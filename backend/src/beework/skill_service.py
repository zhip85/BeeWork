"""Skills (T-074): built-in platform skills, package storage, the security-check job, mounting and ``load_skill``.

A Skill is never executed: the model first sees only its name and description, and asks for the instructions through the
skill's own load tool when it needs them (CAP-027). Scripts in a package are stored, reported, and left alone.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework import file_store, skill_package
from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS, MountItem
from beework.models.capabilities import Capability, PlatformSkill, Skill
from beework.tools.executor import PROVIDERS, ToolContext, ToolError, ToolOutput, ToolSpec
from beework.worker.queue import enqueue, job_handler

MAX_RESOURCE_CHARS = 64_000
BUILTIN_SKILLS = (
    ("handoff", "转人工", "当用户明确要求人工服务、情绪激动或问题超出可处理范围时，转交人工客服。",
     "---\nname: 转人工\ndescription: 当用户明确要求人工服务、情绪激动或问题超出可处理范围时，转交人工客服。\n---\n\n"
     "# 转人工\n\n1. 先确认用户诉求并简要复述。\n2. 告知用户正在为其转接人工客服，并说明可能的等待时间。\n"
     "3. 整理会话摘要（问题、已尝试的方案、用户情绪）交给坐席。\n4. 转接期间不要再回答业务问题。\n"),
    ("sentiment", "情绪识别", "识别用户情绪（平静 / 不满 / 焦虑 / 愤怒），据此调整回复语气，必要时建议转人工。",
     "---\nname: 情绪识别\ndescription: 识别用户情绪（平静 / 不满 / 焦虑 / 愤怒），据此调整回复语气，必要时建议转人工。\n---\n\n"
     "# 情绪识别\n\n- 从措辞、标点、重复催促判断情绪。\n- 不满或焦虑：先共情、再给出明确的下一步。\n- 愤怒：致歉、避免辩解，建议转人工。\n"),
    ("multilingual", "多语言回复", "使用与用户相同的语言回复；术语保持一致，必要时附原文。",
     "---\nname: 多语言回复\ndescription: 使用与用户相同的语言回复；术语保持一致，必要时附原文。\n---\n\n"
     "# 多语言回复\n\n- 识别用户使用的语言并以同一语言回复。\n- 产品名、型号等专有名词保持原文。\n- 用户混用语言时，以主要语言回复。\n"),
)
# Called when a built-in skill is loaded (CAP-009: “转人工” triggers the handoff hook of T-081): fn(db, ctx) -> None.
LOAD_HOOKS: dict[str, Callable[[Session, ToolContext], None]] = {}


def ensure_platform_skills(db: Session) -> None:
    """The platform's three built-in skills exist (idempotent; platform admins can edit them afterwards)."""
    existing = set(db.scalars(select(PlatformSkill.slug)))
    for slug, name, description, content in BUILTIN_SKILLS:
        if slug not in existing:
            db.add(PlatformSkill(slug=slug, name=name, description=description, content=content, status="active"))
    db.flush()


def sync_builtin_skills(db: Session, tenant_id: UUID) -> None:
    """Every platform skill appears in the tenant's capability list (available by default; the tenant may disable it).
    Rows are created on first sight; nothing is created for platform skills the tenant already has."""
    ensure_platform_skills(db)
    have = set(db.scalars(select(Skill.platform_skill_id).where(Skill.tenant_id == tenant_id, Skill.platform_skill_id.is_not(None))))
    for platform in db.scalars(select(PlatformSkill).order_by(PlatformSkill.slug)):
        if platform.id in have:
            continue
        cap = Capability(tenant_id=tenant_id, kind="skill", name=platform.name[:50], description=platform.description, status="available", created_by="平台")
        db.add(cap)
        db.flush()
        db.add(Skill(capability_id=cap.id, tenant_id=tenant_id, platform_skill_id=platform.id, security_status="passed", checks=[]))
    db.flush()


def platform_of(db: Session, skill: Skill) -> PlatformSkill | None:
    return db.get(PlatformSkill, skill.platform_skill_id) if skill.platform_skill_id else None


def is_usable(db: Session, cap: Capability, skill: Skill) -> bool:
    """CAP-026: a self-built skill needs a passed check; a built-in one needs the platform to keep it active."""
    if cap.status != "available":
        return False
    if skill.platform_skill_id is not None:
        platform = platform_of(db, skill)
        return platform is not None and platform.status == "active"
    return skill.security_status == "passed"


def package_key(tenant_id: UUID, capability_id: UUID, version: int) -> str:
    return f"skills/{tenant_id}/{capability_id}/v{version}"


def store_package(skill: Skill, filename: str, data: bytes, package: skill_package.Package) -> None:
    """Keep the new file, reset the check (a new file is always checked again, CAP-025)."""
    skill.package_version += 1
    key = package_key(skill.tenant_id, skill.capability_id, skill.package_version)
    file_store.save(key, data)
    skill.package_key, skill.filename, skill.size_bytes = key, filename[:200], len(data)
    skill.manifest, skill.has_scripts = package.manifest[:300], package.has_scripts
    skill.security_status, skill.checks, skill.checked_at = "pending", skill_package.pending_checks(), None


def queue_check(db: Session, skill: Skill) -> None:
    enqueue(
        db, "skills.security_check", {"capability_id": str(skill.capability_id), "version": skill.package_version},
        tenant_id=skill.tenant_id, idempotency_key=f"skills.security_check:{skill.capability_id}:{skill.package_version}",
    )


# Called once a check finishes and passed — the API module files the enable request (approval policy).
ON_PASSED: list[Callable[[Session, Capability, Skill], None]] = []


@job_handler("skills.security_check")
def security_check_job(db: Session, payload: dict) -> None:
    """Runs the four checks one after another, committing after each so the page can show N/4 (CAP-023). A newer file
    makes this run obsolete: it stops without touching the newer state."""
    skill = db.get(Skill, UUID(payload["capability_id"]))
    if skill is None or skill.package_version != payload["version"] or skill.package_key is None:
        return
    cap = db.get(Capability, skill.capability_id)
    if cap is None or cap.deleted_at is not None:
        return
    try:
        package = skill_package.load(skill.filename, file_store.read(skill.package_key))
    except (skill_package.PackageError, FileNotFoundError) as exc:
        reason = exc.reason if isinstance(exc, skill_package.PackageError) else "包文件丢失，请重新上传"
        skill.checks = [{"key": "structure", "status": "failed", "issues": [{"path": skill.filename, "reason": reason}]}] + skill_package.pending_checks()[1:]
        skill.security_status, skill.checked_at = "failed", datetime.now(UTC)
        db.commit()
        return
    skill.security_status = "running"
    db.commit()
    results: list[dict] = []
    for key in skill_package.CHECK_KEYS:
        results.append({"key": key, "status": "running", "issues": []})
        skill.checks = results + skill_package.pending_checks()[len(results):]
        db.commit()
        status, issues = skill_package.CHECKS[key](package)
        results[-1] = {"key": key, "status": status, "issues": issues[:50]}
        skill.checks = results + skill_package.pending_checks()[len(results):]
        db.commit()
    failed = any(item["status"] == "failed" for item in results)
    skill.security_status, skill.checked_at = ("failed" if failed else "passed"), datetime.now(UTC)
    if failed and cap.status == "available":
        cap.status = "draft"  # CAP-024: a failing package never stays usable
    db.commit()
    if not failed:
        for hook in ON_PASSED:
            hook(db, cap, skill)
        db.commit()


# ---------------------------------------------------------------------------
# Content, mounting and the load tool.
# ---------------------------------------------------------------------------


def skill_text(db: Session, skill: Skill, resource: str | None = None) -> str:
    """SKILL.md (or one of the package's text resources) — read on demand, never executed."""
    platform = platform_of(db, skill)
    if platform is not None:
        if resource:
            raise ToolError("tool.skill_resource", "内置 Skill 没有附带资源")
        return platform.content
    if not skill.package_key:
        raise ToolError("tool.skill_missing", "Skill 包文件不存在")
    package = skill_package.load(skill.filename, file_store.read(skill.package_key))
    path = resource or package.skill_md_path
    content = package.files.get(path)
    if content is None:
        raise ToolError("tool.skill_resource", f"Skill 中没有资源：{path[:100]}")
    text = skill_package._text(content)  # noqa: SLF001 — the same text test the checks use
    if text is None:
        raise ToolError("tool.skill_resource", "该资源不是文本，无法加载")
    if path.lower().endswith(skill_package.SCRIPT_SUFFIXES):
        text = "（以下为脚本源码，仅供参考，本期不会执行）\n" + text
    return text[:MAX_RESOURCE_CHARS]


def _rows(db: Session, tenant_id: UUID, ids: list[UUID] | None = None) -> list[tuple[Capability, Skill]]:
    statement = (
        select(Capability, Skill).join(Skill, Skill.capability_id == Capability.id)
        .where(Capability.tenant_id == tenant_id, Capability.kind == "skill", Capability.deleted_at.is_(None))
    )
    if ids is not None:
        if not ids:
            return []
        statement = statement.where(Capability.id.in_(ids))
    return list(db.execute(statement.order_by(func.lower(Capability.name), Capability.id)).tuples())


def resolve_skill_mounts(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, MountItem]:
    return {
        cap.id: MountItem(
            id=cap.id, kind="skill", name=cap.name, state="available" if is_usable(db, cap, skill) else "disabled",
            meta={"builtin": skill.platform_skill_id is not None, "security": skill.security_status},
        )
        for cap, skill in _rows(db, tenant_id, ids)
    }


def list_skill_candidates(db: Session, membership, q: str | None, limit: int) -> list[dict]:
    """CAP-026 / RUL-010: only usable skills — a self-built one that has not passed its check never shows up."""
    sync_builtin_skills(db, membership.tenant_id)
    db.commit()
    items: list[dict] = []
    for cap, skill in _rows(db, membership.tenant_id):
        if not is_usable(db, cap, skill) or (q and q.strip().lower() not in cap.name.lower()):
            continue
        origin = "平台可信 · Built-in" if skill.platform_skill_id else "安全检查通过"
        items.append({"id": cap.id, "kind": "skill", "name": cap.name, "meta": {"summary": origin, "builtin": skill.platform_skill_id is not None}})
        if len(items) >= limit:
            break
    return items


MOUNT_RESOLVERS["skill"] = resolve_skill_mounts
CANDIDATE_LISTERS["skill"] = list_skill_candidates


def tool_name(capability_id: UUID) -> str:
    return f"skill:{capability_id}"


def _spec(cap: Capability) -> ToolSpec:
    return ToolSpec(
        name=tool_name(cap.id), source="skill", capability_id=cap.id,
        parameters={"type": "object", "properties": {"resource": {"type": "string", "maxLength": 300}}, "additionalProperties": False},
        writes=False, description=f"加载 Skill「{cap.name}」的完整说明：{cap.description}"[:300],
    )


def skill_tool_specs(db: Session, tenant_id: UUID, capability_ids: list[UUID]) -> list[ToolSpec]:
    """What the model sees of the mounted skills: name + description, as one load tool per skill (CAP-027)."""
    return [_spec(cap) for cap, skill in _rows(db, tenant_id, capability_ids) if is_usable(db, cap, skill)]


class SkillProvider:
    """``skill:{capability id}`` — loading instructions is a read; nothing is executed."""

    def lookup(self, db: Session, ctx: ToolContext, name: str) -> ToolSpec | None:
        try:
            cap_id = UUID(name.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
        rows = _rows(db, ctx.tenant_id, [cap_id])
        if not rows or not is_usable(db, *rows[0]):
            return None
        return _spec(rows[0][0])

    def execute(self, db: Session, ctx: ToolContext, spec: ToolSpec, args: dict) -> ToolOutput:
        skill = db.get(Skill, spec.capability_id)
        assert skill is not None
        text = skill_text(db, skill, args.get("resource"))
        platform = platform_of(db, skill)
        if platform is not None and platform.slug in LOAD_HOOKS:
            LOAD_HOOKS[platform.slug](db, ctx)
        return ToolOutput(content=text, response_bytes=len(text.encode("utf-8")))


PROVIDERS["skill"] = SkillProvider()
