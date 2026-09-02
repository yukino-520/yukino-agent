from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


# 作用：表示 Agent 文件访问违反路径、格式或大小约束。
# 参数：无。
class AgentFileError(ValueError):
    pass


# 作用：在显式授权目录内提供受控的项目文件读写能力。
# 参数：无。
class AgentFileAccess:
    """Read project files and write only inside explicitly authorized roots."""

    MAX_READ_BYTES = 2 * 1024 * 1024
    MAX_WRITE_BYTES = 2 * 1024 * 1024
    BLOCKED_NAMES = {
        ".env",
        ".env.local",
        ".env.production",
        "runtime_config.json",
        "credentials.json",
        "secrets.json",
    }
    BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

    # 作用：初始化读写根目录，并过滤过宽或无效的额外授权路径。
    # 参数 workspace_root：Agent 默认可写工作区的根目录。
    # 参数 project_root：允许读取或执行项目资源的根目录。
    # 参数 allowed_write_roots：除默认工作区外显式获准写入的目录列表。
    def __init__(
        self,
        workspace_root: str | Path,
        project_root: str | Path,
        *,
        allowed_write_roots: list[str | Path] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.project_root = Path(project_root).expanduser().resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        configured = allowed_write_roots if allowed_write_roots is not None else self._from_env()
        self.write_roots = self._dedupe_roots(
            [self.workspace_root, *self._safe_configured_roots(configured)]
        )
        self.read_roots = self._dedupe_roots([self.project_root, *self.write_roots])

    # 作用：从环境变量读取额外写入目录及桌面授权开关。
    # 参数：无。
    @classmethod
    # 作用：从环境变量读取并规范化允许访问的文件根目录。
    def _from_env(cls) -> list[Path]:
        values: list[str] = []
        try:
            parsed = json.loads(os.getenv("YUKINO_ALLOWED_WRITE_DIRS", "[]"))
            if isinstance(parsed, list):
                values.extend(str(item) for item in parsed if isinstance(item, str))
        except json.JSONDecodeError:
            pass
        if os.getenv("YUKINO_ALLOW_DESKTOP", "").lower() in {"1", "true", "yes"}:
            values.append(str(Path.home() / "Desktop"))
        return [Path(item).expanduser() for item in values if item.strip()]

    # 作用：解析并去重目录列表，保留稳定的授权顺序。
    # 参数 values：待规范化、写入或合并的配置值集合。
    @staticmethod
    # 作用：执行“dedupe_roots”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 values：调用方传入的values，用于本次处理。
    def _dedupe_roots(values: list[str | Path]) -> tuple[Path, ...]:
        roots: list[Path] = []
        for raw in values:
            path = Path(raw).expanduser().resolve()
            if path not in roots:
                roots.append(path)
        return tuple(roots)

    # 作用：剔除主目录、文件系统根目录等危险或不存在的写入范围。
    # 参数 values：待规范化、写入或合并的配置值集合。
    def _safe_configured_roots(self, values: list[str | Path]) -> list[Path]:
        home = Path.home().resolve()
        broad_roots = {Path("/").resolve(), home, home.parent}
        roots: list[Path] = []
        for raw in values:
            path = Path(raw).expanduser().resolve()
            if path in broad_roots or not path.is_dir():
                continue
            roots.append(path)
        return roots

    # 作用：返回当前文件访问根目录及桌面授权状态。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        desktop = (Path.home() / "Desktop").resolve()
        return {
            "workspace_root": str(self.workspace_root),
            "project_root": str(self.project_root),
            "write_roots": [str(item) for item in self.write_roots],
            "desktop_path": str(desktop),
            "desktop_allowed": desktop in self.write_roots,
        }

    # 作用：将用户路径解析为受授权根目录约束的绝对路径。
    # 参数 raw_path：用户提供的文件或目录路径。
    # 参数 write：本次路径解析是否用于写入操作。
    # 参数 must_exist：解析路径时是否要求目标已经存在。
    def resolve(
        self,
        raw_path: str = ".",
        *,
        write: bool = False,
        must_exist: bool = False,
    ) -> Path:
        value = raw_path.strip() or "."
        expanded = Path(value).expanduser()
        candidate = (
            expanded.resolve()
            if expanded.is_absolute()
            else (self.workspace_root / expanded).resolve()
        )
        roots = self.write_roots if write else self.read_roots
        if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
            kind = "写入" if write else "读取"
            raise AgentFileError(f"该路径不在 Agent 的已授权{kind}目录中。")
        self._guard_secret_path(candidate)
        if must_exist and not candidate.exists():
            raise AgentFileError("文件或目录不存在。")
        return candidate

    # 作用：列出授权目录中的非敏感文件和子目录。
    # 参数 raw_path：用户提供的文件或目录路径。
    def list(self, raw_path: str = ".") -> list[dict[str, Any]]:
        directory = self.resolve(raw_path, must_exist=True)
        if not directory.is_dir():
            raise AgentFileError("目标不是目录。")
        result: list[dict[str, Any]] = []
        for item in sorted(directory.iterdir(), key=lambda value: (not value.is_dir(), value.name.lower())):
            try:
                self._guard_secret_path(item)
            except AgentFileError:
                continue
            result.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "kind": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0,
                }
            )
            if len(result) >= 500:
                break
        return result

    # 作用：读取大小受限的 UTF-8 文本文件。
    # 参数 raw_path：用户提供的文件或目录路径。
    def read(self, raw_path: str) -> dict[str, Any]:
        source = self.resolve(raw_path, must_exist=True)
        if not source.is_file():
            raise AgentFileError("目标不是文件。")
        if source.stat().st_size > self.MAX_READ_BYTES:
            raise AgentFileError("文件超过 2 MiB 读取限制。")
        try:
            content = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise AgentFileError("当前 Agent 文件工具只读取 UTF-8 文本。") from exc
        return {"path": str(source), "content": content, "size": source.stat().st_size}

    # 作用：计算授权文件的大小和 SHA-256 指纹以供结果验收。
    # 参数 raw_path：用户提供的文件或目录路径。
    def fingerprint(self, raw_path: str) -> dict[str, Any]:
        source = self.resolve(raw_path, must_exist=True)
        if not source.is_file():
            raise AgentFileError("目标不是文件。")
        size = source.stat().st_size
        if size > self.MAX_READ_BYTES:
            raise AgentFileError("文件超过 2 MiB 指纹校验限制。")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return {"path": str(source), "size": size, "sha256": digest}

    # 作用：通过临时文件原子写入内容，并校验最终落盘结果。
    # 参数 raw_path：用户提供的文件或目录路径。
    # 参数 content：待写入文件的文本内容。
    # 参数 overwrite：目标已存在时是否允许覆盖写入。
    def write(self, raw_path: str, content: str, *, overwrite: bool = False) -> dict[str, Any]:
        encoded = content.encode("utf-8")
        if len(encoded) > self.MAX_WRITE_BYTES:
            raise AgentFileError("写入内容超过 2 MiB 限制。")
        target = self.resolve(raw_path, write=True)
        if target.exists() and not overwrite:
            raise AgentFileError("文件已经存在，需要确认后才能覆盖。")
        target.parent.mkdir(parents=True, exist_ok=True)
        previous_mode = target.stat().st_mode if target.exists() else None
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            if previous_mode is not None:
                os.chmod(temporary_path, previous_mode)
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        persisted = target.read_bytes()
        if persisted != encoded:
            raise AgentFileError("文件已落盘，但内容校验失败。")
        digest = hashlib.sha256(persisted).hexdigest()
        return {
            "path": str(target),
            "size": len(encoded),
            "overwritten": previous_mode is not None,
            "sha256": digest,
            "verified": True,
        }

    # 作用：在授权范围内搜索文本，并返回命中的文件和行号。
    # 参数 query：能力、文件、网页或图谱检索使用的查询文本。
    # 参数 raw_path：用户提供的文件或目录路径。
    def search(self, query: str, raw_path: str = ".") -> list[dict[str, Any]]:
        if not query.strip():
            raise AgentFileError("搜索词不能为空。")
        root = self.resolve(raw_path, must_exist=True)
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        paths = [root] if root.is_file() else root.rglob("*")
        results: list[dict[str, Any]] = []
        for source in paths:
            if not source.is_file():
                continue
            try:
                self._guard_secret_path(source)
                if source.stat().st_size > self.MAX_READ_BYTES:
                    continue
                lines = source.read_text(encoding="utf-8").splitlines()
            except (AgentFileError, OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                if pattern.search(line):
                    results.append(
                        {"path": str(source), "line": line_number, "text": line[:300]}
                    )
                    if len(results) >= 200:
                        return results
        return results

    # 作用：拒绝访问凭据文件、私钥文件和 Git 内部数据。
    # 参数 path：目标文件、数据库或状态存储路径。
    def _guard_secret_path(self, path: Path) -> None:
        if path.name.lower() in self.BLOCKED_NAMES or path.suffix.lower() in self.BLOCKED_SUFFIXES:
            raise AgentFileError("安全策略禁止 Agent 访问密钥或凭据文件。")
        if ".git" in path.parts:
            raise AgentFileError("安全策略禁止 Agent 访问 Git 内部数据。")
