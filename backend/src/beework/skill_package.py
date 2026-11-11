"""Skill packages (T-074, CAP-020–025): safe unpacking and the four automated security checks.

Nothing in a package is ever executed (architecture decision of T-074): scripts are reported, not run. Unpacking happens in
memory with hard limits, so a hostile archive cannot write outside anything or exhaust the disk.
"""

import io
import math
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # CAP-022
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_FILES = 300
MAX_RATIO = 100  # a member that expands more than 100× is treated as a compression bomb
TEXT_LIMIT = 256 * 1024  # text files above this are not scanned line by line (they are still listed)
ARCHIVE_SUFFIXES = (".zip", ".skill")
SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".ps1", ".bat", ".cmd", ".lua")
BINARY_SUFFIXES = (".exe", ".dll", ".so", ".dylib", ".bin", ".msi", ".app", ".jar", ".class", ".com", ".scr")
MAGIC = {b"\x7fELF": "ELF 可执行文件", b"MZ": "Windows 可执行文件", b"\xcf\xfa\xed\xfe": "Mach-O 可执行文件", b"\xfe\xed\xfa\xcf": "Mach-O 可执行文件", b"\xca\xfe\xba\xbe": "Mach-O / Java 可执行文件"}
DEPENDENCY_FILES = ("requirements.txt", "package.json", "pyproject.toml", "Pipfile", "Gemfile", "go.mod", "Cargo.toml")

CHECK_KEYS = ("structure", "executables", "secrets", "dependencies")


class PackageError(Exception):
    """The upload is not a usable Skill package (CAP-022); ``reason`` is shown to the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Package:
    files: dict[str, bytes]
    skill_md_path: str
    name: str
    description: str
    has_scripts: bool = False
    manifest: list[dict] = field(default_factory=list)

    @property
    def skill_md(self) -> str:
        return self.files[self.skill_md_path].decode("utf-8", errors="replace")


def parse_frontmatter(text: str) -> dict:
    """The YAML block between the leading ``---`` lines of SKILL.md (an empty dict when there is none)."""
    stripped = text.lstrip("﻿")
    if not stripped.startswith("---"):
        return {}
    parts = stripped.split("\n")
    for index in range(1, len(parts)):
        if parts[index].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(parts[1:index]))
            except yaml.YAMLError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _safe_name(raw: str) -> str:
    name = raw.replace("\\", "/")
    path = PurePosixPath(name)
    if name.startswith("/") or any(part in ("..", "") for part in path.parts) or ":" in path.parts[0]:
        raise PackageError(f"包内路径不安全：{raw[:120]}")
    return str(path)


def unpack(filename: str, data: bytes) -> dict[str, bytes]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise PackageError("包大小超过 20 MB")
    lower = filename.lower()
    if lower.endswith(".md"):
        return {"SKILL.md": data}
    if not lower.endswith(ARCHIVE_SUFFIXES):
        raise PackageError("只接受 .zip、.skill 或 .md 文件")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackageError("不是有效的 zip 包") from exc
    files: dict[str, bytes] = {}
    total = 0
    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_FILES:
            raise PackageError(f"包内文件超过 {MAX_FILES} 个")
        for info in members:
            name = _safe_name(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PackageError(f"包内不允许符号链接：{name[:120]}")
            if info.file_size > MAX_UNPACKED_BYTES or (info.compress_size and info.file_size / info.compress_size > MAX_RATIO):
                raise PackageError(f"疑似压缩炸弹：{name[:120]}")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise PackageError("解压后总大小超过 50 MB")
            with archive.open(info) as member:
                content = member.read(info.file_size + 1)
            if len(content) > info.file_size:  # the header lied about the size
                raise PackageError(f"疑似压缩炸弹：{name[:120]}")
            files[name] = content
    return files


def load(filename: str, data: bytes) -> Package:
    """Unpack and read SKILL.md: at the root, or at the root of a single top-level folder (CAP-021)."""
    files = unpack(filename, data)
    candidates = [path for path in files if PurePosixPath(path).name == "SKILL.md" and len(PurePosixPath(path).parts) <= 2]
    root_level = [path for path in candidates if len(PurePosixPath(path).parts) == 1]
    tops = {PurePosixPath(path).parts[0] for path in files}
    if root_level:
        skill_md = root_level[0]
    elif len(tops) == 1 and candidates:
        skill_md = candidates[0]
    else:
        raise PackageError("包内缺少根目录的 SKILL.md")
    meta = parse_frontmatter(files[skill_md].decode("utf-8", errors="replace"))
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not name.strip() or not isinstance(description, str) or not description.strip():
        raise PackageError("SKILL.md 需要包含 name 与 description 的 frontmatter")
    manifest = [{"path": path, "size": len(content)} for path, content in sorted(files.items())]
    has_scripts = any(path.lower().endswith(SCRIPT_SUFFIXES) or content[:2] == b"#!" for path, content in files.items())
    return Package(files, skill_md, name.strip()[:50], description.strip()[:500], has_scripts, manifest)


# ---------------------------------------------------------------------------
# The four checks (CAP-023). Each returns (status, issues); status is passed | warning | failed.
# ---------------------------------------------------------------------------

Issue = dict[str, str]


def _text(content: bytes) -> str | None:
    if len(content) > TEXT_LIMIT or b"\x00" in content[:4096]:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def check_structure(package: Package) -> tuple[str, list[Issue]]:
    issues: list[Issue] = []
    meta = parse_frontmatter(package.skill_md)
    if len(str(meta.get("name", ""))) > 64:
        issues.append({"path": package.skill_md_path, "reason": "name 超过 64 个字符"})
    if len(str(meta.get("description", ""))) > 1024:
        issues.append({"path": package.skill_md_path, "reason": "description 超过 1024 个字符"})
    for path in package.files:
        if any(part.startswith(".") and part not in (".", "..") for part in PurePosixPath(path).parts) and not path.endswith(".gitkeep"):
            issues.append({"path": path, "reason": "包含隐藏文件或目录"})
    return ("failed" if issues else "passed"), issues


def check_executables(package: Package) -> tuple[str, list[Issue]]:
    """Binaries fail; scripts are allowed but reported — they are never executed in this release."""
    failed: list[Issue] = []
    notes: list[Issue] = []
    for path, content in package.files.items():
        lower = path.lower()
        magic = next((label for prefix, label in MAGIC.items() if content.startswith(prefix)), None)
        if lower.endswith(BINARY_SUFFIXES) or magic:
            failed.append({"path": path, "reason": f"包含可执行文件（{magic or '二进制'}）"})
        elif lower.endswith(SCRIPT_SUFFIXES) or content[:2] == b"#!":
            notes.append({"path": path, "reason": "含脚本，本期不执行"})
    if failed:
        return "failed", failed + notes
    return ("warning" if notes else "passed"), notes


SECRET_PATTERNS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS 访问密钥"),
    (re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}"), "AWS 私密访问密钥"),
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), "私钥"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "GitHub 令牌"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack 令牌"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "API 密钥（sk- 前缀）"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API 密钥"),
]
ASSIGNMENT = re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\b\s*[:=]\s*['\"]?([A-Za-z0-9/+_=.-]{16,})")


def _entropy(value: str) -> float:
    counts = {ch: value.count(ch) for ch in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def check_secrets(package: Package) -> tuple[str, list[Issue]]:
    issues: list[Issue] = []
    for path, content in package.files.items():
        text = _text(content)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern, label in SECRET_PATTERNS:
                if pattern.search(line):
                    issues.append({"path": f"{path}:{number}", "reason": f"疑似{label}"})
            match = ASSIGNMENT.search(line)
            if match and _entropy(match.group(1)) >= 3.5 and not any(i["path"] == f"{path}:{number}" for i in issues):
                issues.append({"path": f"{path}:{number}", "reason": "疑似硬编码凭证（高熵字符串）"})
            if len(issues) >= 50:
                return "failed", issues
    return ("failed" if issues else "passed"), issues


URL = re.compile(r"\b(?:https?|ftp)://([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::\d+)?[^\s)\"'>]*")
PRIVATE_HOST = re.compile(r"^(?:localhost|127\.|10\.|192\.168\.|169\.254\.|172\.(?:1[6-9]|2\d|3[01])\.|0\.0\.0\.0|\[?::1\]?|metadata\.google\.internal)", re.I)


def check_dependencies(package: Package) -> tuple[str, list[Issue]]:
    """Internal addresses fail (a Skill must never point the model at the network inside); dependency manifests, plain
    http and the external hosts it mentions are reported for the reviewer."""
    failed: list[Issue] = []
    notes: list[Issue] = []
    hosts: set[str] = set()
    for path, content in package.files.items():
        if PurePosixPath(path).name in DEPENDENCY_FILES:
            notes.append({"path": path, "reason": "声明了外部依赖（本期不安装、不执行）"})
        text = _text(content)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in URL.finditer(line):
                host = match.group(1).lower()
                if PRIVATE_HOST.match(host):
                    failed.append({"path": f"{path}:{number}", "reason": f"引用内网或本机地址：{host}"})
                elif match.group(0).lower().startswith("http://"):
                    notes.append({"path": f"{path}:{number}", "reason": f"使用非加密地址：{host}"})
                hosts.add(host)
    external = sorted(h for h in hosts if not PRIVATE_HOST.match(h))
    if external:
        notes.append({"path": "（全部文件）", "reason": "引用的外部域名：" + "、".join(external[:20])})
    if failed:
        return "failed", failed + notes
    return ("warning" if notes else "passed"), notes


CHECKS = {
    "structure": check_structure,
    "executables": check_executables,
    "secrets": check_secrets,
    "dependencies": check_dependencies,
}


def pending_checks() -> list[dict]:
    return [{"key": key, "status": "pending", "issues": []} for key in CHECK_KEYS]
