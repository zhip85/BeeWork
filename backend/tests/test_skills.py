"""T-074: Skills — package loading, the four security checks, the worker job, built-in skills and the load tool."""

import io
import shutil
import stat
import tempfile
import unittest
import zipfile
from uuid import UUID, uuid4

from sqlalchemy import select

import test_assistants
from beework import skill_package, skill_service
from beework.approvals import ensure_policies
from beework.config import settings
from beework.models import Membership, Tenant, User
from beework.models.approvals import ApprovalRequest
from beework.models.capabilities import Capability, PlatformSkill, Skill
from beework.tools.executor import ToolContext, invoke_tool
from beework.worker.runner import run_once

SKILL_MD = "---\nname: 退货助手\ndescription: 处理退货与退款问题的步骤\n---\n\n# 退货助手\n\n先确认订单号，再说明退货规则。\n"


def zip_of(files: dict[str, bytes | str], *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content if isinstance(content, bytes) else content.encode())
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


class PackageTests(unittest.TestCase):
    def test_a_single_skill_md_is_a_package(self) -> None:
        package = skill_package.load("SKILL.md", SKILL_MD.encode())
        self.assertEqual((package.name, package.description, package.skill_md_path, package.has_scripts), ("退货助手", "处理退货与退款问题的步骤", "SKILL.md", False))

    def test_zip_with_skill_md_at_the_root_or_in_one_top_folder(self) -> None:
        self.assertEqual(skill_package.load("a.zip", zip_of({"SKILL.md": SKILL_MD, "docs/rules.md": "# rules"})).skill_md_path, "SKILL.md")
        nested = skill_package.load("a.skill", zip_of({"refund/SKILL.md": SKILL_MD, "refund/run.py": "print(1)"}))
        self.assertEqual((nested.skill_md_path, nested.has_scripts), ("refund/SKILL.md", True))
        self.assertEqual([m["path"] for m in nested.manifest], ["refund/SKILL.md", "refund/run.py"])

    def test_packages_that_are_not_usable_are_rejected_with_a_reason(self) -> None:
        cases = {
            "缺少根目录的 SKILL.md": ("a.zip", zip_of({"README.md": "x"})),
            "frontmatter": ("SKILL.md", b"# no frontmatter"),
            "不是有效的 zip": ("a.zip", b"not a zip"),
            "只接受": ("a.tar.gz", b"x"),
            "路径不安全": ("a.zip", zip_of({"SKILL.md": SKILL_MD, "../evil.md": "x"})),
            "符号链接": ("a.zip", zip_of({"SKILL.md": SKILL_MD}, symlink="link")),
            "压缩炸弹": ("a.zip", zip_of({"SKILL.md": SKILL_MD, "big.txt": b"0" * (5 * 1024 * 1024)})),
            "20 MB": ("SKILL.md", b"x" * (skill_package.MAX_UPLOAD_BYTES + 1)),
        }
        for expected, (filename, data) in cases.items():
            with self.assertRaises(skill_package.PackageError, msg=expected) as caught:
                skill_package.load(filename, data)
            self.assertIn(expected, caught.exception.reason)
        two_tops = zip_of({"a/SKILL.md": SKILL_MD, "b/x.md": "x"})
        with self.assertRaises(skill_package.PackageError):
            skill_package.load("a.zip", two_tops)

    def test_too_many_files(self) -> None:
        files = {"SKILL.md": SKILL_MD, **{f"f{i}.md": "x" for i in range(skill_package.MAX_FILES)}}
        with self.assertRaises(skill_package.PackageError):
            skill_package.load("a.zip", zip_of(files))

    def run_check(self, key: str, files: dict[str, bytes | str]) -> tuple[str, list[dict]]:
        return skill_package.CHECKS[key](skill_package.load("a.zip", zip_of({"SKILL.md": SKILL_MD, **files})))

    def test_structure_check(self) -> None:
        self.assertEqual(self.run_check("structure", {})[0], "passed")
        status, issues = self.run_check("structure", {".env": "x"})
        self.assertEqual((status, issues[0]["path"]), ("failed", ".env"))

    def test_scripts_are_reported_and_binaries_fail(self) -> None:
        self.assertEqual(self.run_check("executables", {"notes.md": "x"}), ("passed", []))
        status, issues = self.run_check("executables", {"run.sh": "#!/bin/sh\necho hi"})
        self.assertEqual((status, issues[0]["reason"]), ("warning", "含脚本，本期不执行"))
        for name, content in (("tool.bin", b"\x7fELFxxxx"), ("setup.exe", b"MZ\x90\x00"), ("lib.so", b"x")):
            self.assertEqual(self.run_check("executables", {name: content})[0], "failed", name)

    def test_secrets_are_found_with_file_and_line(self) -> None:
        status, issues = self.run_check("secrets", {"config/aws.md": "region: x\naws_key: AKIAIOSFODNN7EXAMPLE\n"})
        self.assertEqual((status, issues[0]["path"]), ("failed", "config/aws.md:2"))
        self.assertIn("AWS", issues[0]["reason"])
        for content in ("-----BEGIN RSA PRIVATE KEY-----", "token = ghp_" + "a" * 36, "api_key = 'Zx9Qw3Er7Ty1Ui5Op2As8Df'"):
            self.assertEqual(self.run_check("secrets", {"x.md": content})[0], "failed", content)
        self.assertEqual(self.run_check("secrets", {"x.md": "password = example"})[0], "passed")  # low entropy placeholder

    def test_dependencies_and_internal_addresses(self) -> None:
        self.assertEqual(self.run_check("dependencies", {})[0], "passed")
        status, issues = self.run_check("dependencies", {"requirements.txt": "requests\n", "x.md": "see https://docs.example.com/a"})
        self.assertEqual(status, "warning")
        self.assertIn("docs.example.com", issues[-1]["reason"])
        for url in ("http://localhost:8080/x", "http://169.254.169.254/latest", "https://10.0.0.8/a", "http://192.168.1.2"):
            self.assertEqual(self.run_check("dependencies", {"x.md": url})[0], "failed", url)


class SkillTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        self.storage = tempfile.mkdtemp(prefix="beework-skills-")
        self._storage_setting, settings.file_storage_dir = settings.file_storage_dir, self.storage
        self.addCleanup(lambda: (shutil.rmtree(self.storage, ignore_errors=True), setattr(settings, "file_storage_dir", self._storage_setting)))
        with self.session_factory.begin() as db:
            account = User(email="admin2@example.com", display_name="Admin Two", password_hash=test_assistants.hash_password(test_assistants.PASSWORD))
            platform = User(email="platform@example.com", display_name="Platform", password_hash=test_assistants.hash_password(test_assistants.PASSWORD), is_platform_admin=True)
            db.add_all([account, platform])
            db.flush()
            db.add(Membership(tenant_id=UUID(self.ids["tenant"]), user_id=account.id, organization_id=UUID(self.ids["root"]), role="tenant_admin"))
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = False
        self.admin = self.login("admin@example.com")

    # -- helpers ------------------------------------------------------------------

    def upload(self, data: bytes, filename: str = "SKILL.md", headers=None, **query):
        params = "&".join(f"{k}={v}" for k, v in {"filename": filename, **query}.items())
        return self.client.post(f"/api/v1/capabilities/skills?{params}", content=data, headers={**(headers or self.admin), "Content-Type": "application/octet-stream"})

    def drain(self) -> None:
        run_once("test-worker", self.session_factory, schedule=False)

    def get(self, capability_id: str, headers=None) -> dict:
        response = self.client.get(f"/api/v1/capabilities/{capability_id}", headers=headers or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def post(self, path: str, headers=None, body=None):
        return self.client.post(f"/api/v1/capabilities/{path}", json=body, headers=headers or self.admin)

    def ready_skill(self, data: bytes = SKILL_MD.encode(), filename: str = "SKILL.md", **query) -> str:
        cap = self.upload(data, filename, **query).json()
        self.drain()
        self.assertEqual(self.get(cap["id"])["status"], "available")
        return cap["id"]

    def builtins(self) -> dict[str, dict]:
        items = self.client.get("/api/v1/capabilities?kind=skill", headers=self.admin).json()
        return {item["name"]: item for item in items if item["skill"]["builtin"]}

    def candidates(self) -> list[str]:
        return [c["name"] for c in self.client.get("/api/v1/capabilities/candidates?kind=skill", headers=self.admin).json()["items"]]

    # -- upload and checks ----------------------------------------------------------

    def test_upload_fills_name_and_description_from_skill_md_and_queues_the_checks(self) -> None:
        response = self.upload(SKILL_MD.encode())
        self.assertEqual(response.status_code, 201, response.text)
        cap = response.json()
        self.assertEqual((cap["name"], cap["description"], cap["status"], cap["kind"]), ("退货助手", "处理退货与退款问题的步骤", "draft", "skill"))
        self.assertEqual((cap["skill"]["security_status"], [c["status"] for c in cap["skill"]["checks"]], cap["meta"]), ("pending", ["pending"] * 4, "待安全检查 · SKILL.md"))
        self.assertNotIn("退货助手", self.candidates())  # CAP-026: not mountable before the checks pass
        self.drain()
        after = self.get(cap["id"])
        self.assertEqual((after["status"], after["skill"]["security_status"], [c["status"] for c in after["skill"]["checks"]]), ("available", "passed", ["passed"] * 4))
        self.assertEqual(after["meta"], "安全检查通过 · SKILL.md")
        self.assertIn("退货助手", self.candidates())

    def test_given_name_and_description_win_over_the_frontmatter(self) -> None:
        cap = self.upload(SKILL_MD.encode(), name="自定义名", description="自定义描述").json()
        self.assertEqual((cap["name"], cap["description"]), ("自定义名", "自定义描述"))

    def test_scripts_only_warn_and_the_skill_still_passes(self) -> None:
        cap_id = self.ready_skill(zip_of({"SKILL.md": SKILL_MD, "tools/run.py": "print('hi')"}), "pkg.zip")
        checks = {c["key"]: c for c in self.get(cap_id)["skill"]["checks"]}
        self.assertEqual(checks["executables"]["status"], "warning")
        self.assertEqual(checks["executables"]["issues"][0], {"path": "tools/run.py", "reason": "含脚本，本期不执行"})
        self.assertTrue(self.get(cap_id)["skill"]["has_scripts"])

    def test_a_package_with_a_secret_fails_and_stays_a_draft(self) -> None:
        cap = self.upload(zip_of({"SKILL.md": SKILL_MD, "notes.md": "key AKIAIOSFODNN7EXAMPLE"}), "pkg.zip").json()
        self.drain()
        after = self.get(cap["id"])
        self.assertEqual((after["status"], after["skill"]["security_status"]), ("draft", "failed"))
        secrets = next(c for c in after["skill"]["checks"] if c["key"] == "secrets")
        self.assertEqual((secrets["status"], secrets["issues"][0]["path"]), ("failed", "notes.md:1"))
        self.assertNotIn("退货助手", self.candidates())
        blocked = self.post(f"{cap['id']}/enable")
        self.assertEqual((blocked.status_code, blocked.json()["detail"]), (409, "此 Skill 需要先通过安全检查才能启用。"))

    def test_invalid_packages_are_rejected_at_upload(self) -> None:
        for data, filename in ((b"# no frontmatter", "SKILL.md"), (zip_of({"x.md": "x"}), "a.zip"), (b"junk", "a.zip"), (b"x", "a.exe")):
            response = self.upload(data, filename)
            self.assertEqual((response.status_code, response.json()["code"]), (422, "capability.invalid_package"), filename)
        with self.session_factory() as db:
            self.assertEqual(db.scalars(select(Capability).where(Capability.created_by != "平台")).all(), [])

    def test_an_oversized_upload_is_refused_before_reading_it(self) -> None:
        response = self.upload(b"x" * (skill_package.MAX_UPLOAD_BYTES + 1))
        self.assertEqual((response.status_code, response.json()["code"]), (413, "capability.package_too_large"))

    def test_names_are_unique_among_skills(self) -> None:
        self.assertEqual(self.upload(SKILL_MD.encode()).status_code, 201)
        self.assertEqual(self.upload(SKILL_MD.encode()).json()["code"], "capability.name_taken")

    def test_a_new_file_is_checked_again_and_an_old_run_does_not_overwrite_it(self) -> None:
        cap_id = self.ready_skill()
        replaced = self.client.post(f"/api/v1/capabilities/skills/{cap_id}/package?filename=pkg.zip", content=zip_of({"SKILL.md": SKILL_MD, "k.md": "AKIAIOSFODNN7EXAMPLE"}), headers=self.admin)
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual((replaced.json()["status"], replaced.json()["skill"]["security_status"], replaced.json()["skill"]["filename"]), ("draft", "pending", "pkg.zip"))
        with self.session_factory() as db:  # an obsolete run (previous version) leaves the new state alone
            skill_service.security_check_job(db, {"capability_id": cap_id, "version": 1})
        self.assertEqual(self.get(cap_id)["skill"]["security_status"], "pending")
        self.drain()
        self.assertEqual((self.get(cap_id)["skill"]["security_status"], self.get(cap_id)["status"]), ("failed", "draft"))

    def test_renaming_keeps_the_check_result(self) -> None:
        cap_id = self.ready_skill()
        current = self.get(cap_id)
        renamed = self.client.put(f"/api/v1/capabilities/skills/{cap_id}", json={"name": "新名称", "description": "d", "row_version": current["row_version"]}, headers=self.admin)
        self.assertEqual((renamed.status_code, renamed.json()["status"], renamed.json()["skill"]["security_status"]), (200, "available", "passed"))
        stale = self.client.put(f"/api/v1/capabilities/skills/{cap_id}", json={"name": "X", "row_version": current["row_version"]}, headers=self.admin)
        self.assertEqual(stale.status_code, 409)

    def test_recheck_runs_again_on_the_same_file(self) -> None:
        cap_id = self.ready_skill()
        rechecked = self.post(f"skills/{cap_id}/recheck").json()
        self.assertEqual(rechecked["skill"]["security_status"], "pending")
        self.assertEqual(self.post(f"skills/{cap_id}/recheck").json()["code"], "capability.check_running")
        self.drain()
        self.assertEqual(self.get(cap_id)["skill"]["security_status"], "passed")

    # -- approval ------------------------------------------------------------------

    def test_with_the_approval_policy_on_a_passed_skill_waits_for_approval(self) -> None:
        with self.session_factory.begin() as db:
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = True
        cap = self.upload(SKILL_MD.encode()).json()
        self.drain()
        after = self.get(cap["id"])
        self.assertEqual((after["status"], after["pending_approval"]), ("draft", True))
        with self.session_factory() as db:
            request = db.scalar(select(ApprovalRequest).where(ApprovalRequest.object_id == UUID(cap["id"])))
        self.assertEqual(self.client.post(f"/api/v1/approvals/{request.id}/approve", json={}, headers=self.login("admin2@example.com")).status_code, 200)
        self.drain()
        self.admin = self.login("admin@example.com")
        self.assertEqual(self.get(cap["id"])["status"], "available")

    def test_without_another_approver_the_reason_is_kept_on_the_skill(self) -> None:
        with self.session_factory.begin() as db:
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = True
            for membership in db.scalars(select(Membership).join(User).where(User.email == "admin2@example.com")):
                membership.status = "suspended"
        cap = self.upload(SKILL_MD.encode()).json()
        self.drain()
        after = self.get(cap["id"])
        self.assertEqual((after["status"], after["skill"]["security_status"]), ("draft", "passed"))
        self.assertIn("没有其他可审批的人", after["skill"]["enable_warning"])

    # -- built-in skills ---------------------------------------------------------------

    def test_the_platform_skills_appear_for_every_tenant_and_are_available(self) -> None:
        built = self.builtins()
        self.assertEqual(sorted(built), ["多语言回复", "情绪识别", "转人工"])
        handoff = built["转人工"]
        self.assertEqual((handoff["status"], handoff["meta"], handoff["can_edit"], handoff["can_delete"]), ("available", "平台可信 · Built-in", False, False))
        self.assertEqual(len(self.builtins()), 3)  # listing again creates nothing new
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(len([i for i in self.client.get("/api/v1/capabilities?kind=skill", headers=stranger).json() if i["skill"]["builtin"]]), 3)

    def test_built_ins_cannot_be_changed_or_deleted_but_can_be_disabled_and_copied(self) -> None:
        cap_id = self.builtins()["转人工"]["id"]
        self.assertEqual(self.client.delete(f"/api/v1/capabilities/{cap_id}", headers=self.admin).json()["code"], "capability.builtin")
        self.assertEqual(self.client.put(f"/api/v1/capabilities/skills/{cap_id}", json={"name": "x", "row_version": 1}, headers=self.admin).json()["code"], "capability.builtin")
        self.assertEqual(self.client.post(f"/api/v1/capabilities/skills/{cap_id}/package?filename=SKILL.md", content=SKILL_MD.encode(), headers=self.admin).json()["code"], "capability.builtin")
        self.assertEqual(self.post(f"{cap_id}/disable").json()["status"], "disabled")
        self.assertNotIn("转人工", self.candidates())
        with self.session_factory.begin() as db:
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = True  # built-ins are platform-trusted: no approval
        self.assertEqual(self.post(f"{cap_id}/enable").json()["status"], "available")
        copy = self.post(f"{cap_id}/copy").json()
        self.assertEqual((copy["name"], copy["status"], copy["skill"]["builtin"], copy["skill"]["security_status"]), ("转人工（副本）", "draft", False, "pending"))
        with self.session_factory.begin() as db:
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = False
        self.drain()
        self.assertEqual(self.get(copy["id"])["status"], "available")  # the copy went through the checks

    def test_the_platform_maintains_built_ins_and_can_switch_one_off_everywhere(self) -> None:
        cap_id = self.builtins()["情绪识别"]["id"]
        platform = self.login("platform@example.com")
        rows = self.client.get("/api/v1/platform/skills", headers=platform).json()
        row = next(r for r in rows if r["slug"] == "sentiment")
        content = "---\nname: 情绪识别 v2\ndescription: 新说明\n---\n\n新内容"
        updated = self.client.put(f"/api/v1/platform/skills/{row['id']}", json={"name": "情绪识别 v2", "description": "新说明", "content": content, "status": "disabled"}, headers=platform)
        self.assertEqual(updated.status_code, 200, updated.text)
        bad = self.client.put(f"/api/v1/platform/skills/{row['id']}", json={"name": "x", "content": "no frontmatter"}, headers=platform)
        self.assertEqual(bad.status_code, 422)
        self.admin = self.login("admin@example.com")
        data = self.get(cap_id)
        self.assertEqual((data["name"], data["skill"]["platform_active"]), ("情绪识别 v2", False))
        self.assertNotIn("情绪识别 v2", self.candidates())
        self.assertEqual(self.client.get("/api/v1/platform/skills", headers=self.admin).status_code, 403)

    # -- runtime -----------------------------------------------------------------------

    def test_the_model_sees_name_and_description_and_loads_the_instructions_on_demand(self) -> None:
        cap_id = self.ready_skill(zip_of({"SKILL.md": SKILL_MD, "docs/rules.md": "# 规则", "run.sh": "echo hi", "img.png": b"\x89PNG\x00\x00"}), "pkg.zip")
        tenant = UUID(self.ids["tenant"])
        name = skill_service.tool_name(UUID(cap_id))
        with self.session_factory() as db:
            specs = skill_service.skill_tool_specs(db, tenant, [UUID(cap_id)])
            self.assertEqual([(s.name, s.writes) for s in specs], [(name, False)])
            self.assertIn("退货助手", specs[0].description)
            ctx = ToolContext(tenant_id=tenant, allowed=frozenset({name}), run_id=uuid4())
            self.assertIn("先确认订单号", invoke_tool(db, ctx, name, {}).content)
            self.assertEqual(invoke_tool(db, ctx, name, {"resource": "docs/rules.md"}).content, "# 规则")
            self.assertTrue(invoke_tool(db, ctx, name, {"resource": "run.sh"}).content.startswith("（以下为脚本源码"))
            self.assertEqual(invoke_tool(db, ctx, name, {"resource": "img.png"}).code, "tool.skill_resource")
            self.assertEqual(invoke_tool(db, ctx, name, {"resource": "missing.md"}).code, "tool.skill_resource")
            self.assertEqual(invoke_tool(db, ctx, name, {"other": 1}).code, "tool.invalid_arguments")

    def test_built_in_skills_load_from_the_platform_and_fire_their_hook(self) -> None:
        cap_id = self.builtins()["转人工"]["id"]
        fired: list[str] = []
        skill_service.LOAD_HOOKS["handoff"] = lambda db, ctx: fired.append(str(ctx.run_id))
        self.addCleanup(skill_service.LOAD_HOOKS.pop, "handoff", None)
        name = skill_service.tool_name(UUID(cap_id))
        with self.session_factory() as db:
            run_id = uuid4()
            result = invoke_tool(db, ToolContext(tenant_id=UUID(self.ids["tenant"]), allowed=frozenset({name}), run_id=run_id), name, {})
        self.assertIn("转交人工", result.content)
        self.assertEqual(fired, [str(run_id)])

    def test_an_unusable_skill_cannot_be_loaded_or_mounted(self) -> None:
        cap = self.upload(zip_of({"SKILL.md": SKILL_MD, "k.md": "AKIAIOSFODNN7EXAMPLE"}), "pkg.zip").json()
        self.drain()
        name = skill_service.tool_name(UUID(cap["id"]))
        with self.session_factory() as db:
            ctx = ToolContext(tenant_id=UUID(self.ids["tenant"]), allowed=frozenset({name}), run_id=uuid4())
            self.assertEqual(invoke_tool(db, ctx, name, {}).code, "tool.not_found")
            items = skill_service.resolve_skill_mounts(db, UUID(self.ids["tenant"]), [UUID(cap["id"])])
            self.assertEqual(items[UUID(cap["id"])].state, "disabled")
            other = ToolContext(tenant_id=UUID(self.ids["other"]), allowed=frozenset({name}), run_id=uuid4())
            self.assertEqual(invoke_tool(db, other, name, {}).code, "tool.not_found")

    def test_mounting_a_skill_on_an_agent(self) -> None:
        cap_id = self.ready_skill()
        assistant = self.create(self.admin, "Uses skill").json()["id"]
        current = self.client.get(f"/api/v1/assistants/{assistant}", headers=self.admin).json()
        body = {"name": "Uses skill", "description": "d", "category": "sales", "row_version": current["row_version"], "config": {**current["config"], "mounts": {**current["config"]["mounts"], "skill": [cap_id]}}}
        self.assertEqual(self.client.put(f"/api/v1/assistants/{assistant}", json=body, headers=self.admin).status_code, 200)
        refs = self.client.get(f"/api/v1/capabilities/{cap_id}/references", headers=self.admin).json()
        self.assertEqual(refs["total"], 1)

    # -- entitlement, permissions, isolation ---------------------------------------------------

    def test_a_plan_without_skills_is_locked(self) -> None:
        from beework.models.plans import TenantSubscription

        with self.session_factory.begin() as db:
            db.get(Tenant, UUID(self.ids["tenant"])).plan = "standard"
            for row in db.scalars(select(TenantSubscription).where(TenantSubscription.tenant_id == UUID(self.ids["tenant"]))):
                db.delete(row)
        locked = self.upload(SKILL_MD.encode())
        self.assertEqual((locked.status_code, locked.json()["code"]), (403, "plan.feature_locked"))

    def test_roles_without_permission_and_other_tenants(self) -> None:
        cap_id = self.ready_skill()
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.get(f"/api/v1/capabilities/{cap_id}", headers=viewer).status_code, 200)
        self.assertEqual(self.upload(SKILL_MD.encode(), headers=viewer).status_code, 403)
        self.assertEqual(self.client.post(f"/api/v1/capabilities/skills/{cap_id}/recheck", headers=viewer).status_code, 403)
        stranger = self.login("other@example.com", self.ids["other"])
        for method, path in (("get", f"/api/v1/capabilities/{cap_id}"), ("post", f"/api/v1/capabilities/skills/{cap_id}/recheck"), ("post", f"/api/v1/capabilities/{cap_id}/copy"), ("delete", f"/api/v1/capabilities/{cap_id}")):
            self.assertEqual(getattr(self.client, method)(path, headers=stranger).status_code, 404, path)
        self.assertEqual(self.client.post(f"/api/v1/capabilities/skills/{cap_id}/package?filename=SKILL.md", content=SKILL_MD.encode(), headers=stranger).status_code, 404)

    def test_packages_are_stored_per_tenant(self) -> None:
        cap_id = self.ready_skill()
        self.builtins()  # built-in skills are created the first time the list is opened
        with self.session_factory() as db:
            skill = db.get(Skill, UUID(cap_id))
            self.assertTrue(skill.package_key.startswith(f"skills/{self.ids['tenant']}/{cap_id}/"))
            self.assertEqual(len(db.scalars(select(PlatformSkill)).all()), 3)


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(SkillTests, _name, None)  # inherited fixtures only; the assistant tests run in their own module


if __name__ == "__main__":
    unittest.main()
