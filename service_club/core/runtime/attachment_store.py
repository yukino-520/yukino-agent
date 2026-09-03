"""Session-scoped, durable storage for chat attachments."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from service_club.storage.relational import RelationalBackend, configured_relational_backend


# 作用：表示附件名称、格式、大小、归属或完整性不符合安全约束。
# 参数：无。
class AttachmentError(ValueError):
    pass


# 作用：持久化会话附件及其摘要元数据，并阻断路径穿越和伪造文件类型。
# 参数：无。
class AttachmentStore:
    MAX_UPLOAD_BYTES = 10 * 1024 * 1024
    MAX_TEXT_BYTES = 2 * 1024 * 1024
    MAX_ATTACHMENTS_PER_REQUEST = 4
    ID_PATTERN = re.compile(r"^att-[a-f0-9]{32}$")
    SESSION_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")
    CODE_SUFFIXES = {
        ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
        ".jsx", ".kt", ".php", ".py", ".rb", ".rs", ".sh", ".swift", ".ts",
        ".tsx", ".vue",
    }
    TEXT_SUFFIXES = {
        ".css", ".csv", ".html", ".json", ".log", ".md", ".rst", ".toml",
        ".txt", ".xml", ".yaml", ".yml",
    }
    DOCUMENT_SUFFIXES = {".docx", ".pdf", ".pptx", ".xlsx"}
    IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}

    # 作用：绑定元数据后端与文件根目录，并初始化附件索引表。
    # 参数 db_path：旧版路径参数，运行时不使用。
    # 参数 root：附件文件实际保存的受控根目录。
    # 参数 backend：可选的关系型存储后端；未提供时读取 PostgreSQL 配置。
    def __init__(
        self,
        db_path: str | Path | None = None,
        root: str | Path | None = None,
        *,
        backend: RelationalBackend | None = None,
    ) -> None:
        if backend is None:
            backend = configured_relational_backend()
        if root is None:
            raise ValueError("附件存储目录不能为空。")
        self.backend = backend
        self.db_path = Path(db_path) if db_path is not None else None
        self.root = Path(root).resolve()
        self.workspace_root = self.root.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # 作用：创建附件元数据表和会话内按时间查询的索引。
    # 参数：无。
    def _ensure_schema(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS chat_attachments (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at {self.backend.float_type} NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_attachments_session
                ON chat_attachments(session_id, created_at DESC)
                """
            )

    # 作用：校验附件后原子写盘、计算 SHA-256，并登记会话归属元数据。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 filename：用户提交或系统处理的附件文件名。
    # 参数 mime_type：客户端声明的附件 MIME 类型。
    # 参数 content：附件的原始字节内容。
    def save(
        self,
        *,
        session_id: str,
        filename: str,
        mime_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        self._validate_session(session_id)
        safe_name = self._safe_name(filename)
        kind = self._kind_for(safe_name)
        if not content:
            raise AttachmentError("附件不能为空。")
        if len(content) > self.MAX_UPLOAD_BYTES:
            raise AttachmentError("附件不能超过 10 MiB。")
        if kind in {"code", "text"} and len(content) > self.MAX_TEXT_BYTES:
            raise AttachmentError("代码或文本附件不能超过 2 MiB。")
        self._validate_content(safe_name, kind, content)

        attachment_id = f"att-{uuid.uuid4().hex}"
        session_folder = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        target = (self.root / session_folder / f"{attachment_id}-{safe_name}").resolve()
        if not target.is_relative_to(self.root):
            raise AttachmentError("附件路径无效。")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{attachment_id}.",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        digest = hashlib.sha256(content).hexdigest()
        relative_path = str(target.relative_to(self.workspace_root))
        created_at = time.time()
        try:
            with self.backend.connect(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT INTO chat_attachments(
                        id, session_id, original_name, relative_path,
                        mime_type, kind, size, sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attachment_id,
                        session_id,
                        safe_name,
                        relative_path,
                        self._canonical_mime(safe_name, mime_type),
                        kind,
                        len(content),
                        digest,
                        created_at,
                    ),
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return self.get(session_id, attachment_id) or {}

    # 作用：仅在附件属于指定会话时返回其元数据。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 attachment_id：系统生成的附件唯一标识。
    def get(self, session_id: str, attachment_id: str) -> dict[str, Any] | None:
        self._validate_session(session_id)
        self._validate_id(attachment_id)
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT id, session_id, original_name, relative_path,
                       mime_type, kind, size, sha256, created_at
                FROM chat_attachments
                WHERE id = ? AND session_id = ?
                """,
                (attachment_id, session_id),
            ).fetchone()
        return dict(row) if row is not None else None

    # 作用：批量读取会话附件，并逐个复核文件存在性与 SHA-256 完整性。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 attachment_ids：本次请求需要读取的附件标识列表。
    def get_many(self, session_id: str, attachment_ids: list[str]) -> list[dict[str, Any]]:
        self._validate_session(session_id)
        unique_ids = list(dict.fromkeys(attachment_ids))
        if len(unique_ids) > self.MAX_ATTACHMENTS_PER_REQUEST:
            raise AttachmentError("每次最多发送 4 个附件。")
        attachments: list[dict[str, Any]] = []
        for attachment_id in unique_ids:
            item = self.get(session_id, attachment_id)
            if item is None:
                raise AttachmentError("附件不存在、已删除，或不属于当前会话。")
            path = (self.workspace_root / str(item["relative_path"])).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise AttachmentError(f"附件 {item['original_name']} 的文件已经丢失。")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise AttachmentError(f"附件 {item['original_name']} 完整性校验失败。")
            attachments.append(item)
        return attachments

    # 作用：按上传时间倒序列出会话内的附件元数据。
    # 参数 session_id：当前用户会话的唯一标识。
    def list(self, session_id: str) -> list[dict[str, Any]]:
        self._validate_session(session_id)
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, original_name, relative_path,
                       mime_type, kind, size, sha256, created_at
                FROM chat_attachments
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：删除会话拥有的附件文件及对应数据库记录。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 attachment_id：系统生成的附件唯一标识。
    def delete(self, session_id: str, attachment_id: str) -> bool:
        item = self.get(session_id, attachment_id)
        if item is None:
            return False
        path = (self.workspace_root / str(item["relative_path"])).resolve()
        if path.is_relative_to(self.root):
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM chat_attachments WHERE id = ? AND session_id = ?",
                (attachment_id, session_id),
            )
        return bool(cursor.rowcount)

    # 作用：逐个清理指定会话的全部附件并返回成功数量。
    # 参数 session_id：当前用户会话的唯一标识。
    def delete_session(self, session_id: str) -> int:
        items = self.list(session_id)
        deleted = 0
        for item in items:
            deleted += int(self.delete(session_id, str(item["id"])))
        return deleted

    # 作用：清洗用户文件名为安全的 basename，并验证扩展名受支持。
    # 参数 filename：用户提交或系统处理的附件文件名。
    @classmethod
    # 作用：执行“safe_name”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 filename：调用方传入的filename，用于本次处理。
    def _safe_name(cls, filename: str) -> str:
        name = Path(filename.strip()).name
        name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", name)[:120]
        if not name or name in {".", ".."} or name.startswith("."):
            raise AttachmentError("附件文件名无效。")
        cls._kind_for(name)
        return name

    # 作用：根据扩展名将附件归类为代码、文本、文档或图片。
    # 参数 filename：用户提交或系统处理的附件文件名。
    @classmethod
    # 作用：执行“kind_for”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 filename：调用方传入的filename，用于本次处理。
    def _kind_for(cls, filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix in cls.CODE_SUFFIXES:
            return "code"
        if suffix in cls.TEXT_SUFFIXES:
            return "text"
        if suffix in cls.DOCUMENT_SUFFIXES:
            return "document"
        if suffix in cls.IMAGE_SUFFIXES:
            return "image"
        raise AttachmentError("暂不支持这个附件格式。支持代码、文本、PDF、DOCX、XLSX、PPTX 和常见图片。")

    # 作用：通过编码和文件魔数检查扩展名是否与真实内容匹配。
    # 参数 filename：用户提交或系统处理的附件文件名。
    # 参数 kind：附件分类或后台作业类型。
    # 参数 content：附件的原始字节内容。
    @classmethod
    # 作用：执行“validate_content”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 filename：调用方传入的filename，用于本次处理。
    # 参数 kind：调用方传入的kind，用于本次处理。
    # 参数 content：需要保存、展示或传递的正文内容。
    def _validate_content(cls, filename: str, kind: str, content: bytes) -> None:
        suffix = Path(filename).suffix.lower()
        if kind in {"code", "text"}:
            if b"\x00" in content:
                raise AttachmentError("文本附件包含二进制内容。")
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AttachmentError("代码和文本附件必须使用 UTF-8 编码。") from exc
        elif suffix == ".pdf" and not content.startswith(b"%PDF-"):
            raise AttachmentError("PDF 文件内容与扩展名不匹配。")
        elif suffix in {".docx", ".xlsx", ".pptx"} and not content.startswith(b"PK\x03\x04"):
            raise AttachmentError("Office 文件内容与扩展名不匹配。")
        elif suffix == ".png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AttachmentError("PNG 文件内容与扩展名不匹配。")
        elif suffix in {".jpg", ".jpeg"} and not content.startswith(b"\xff\xd8\xff"):
            raise AttachmentError("JPEG 文件内容与扩展名不匹配。")
        elif suffix == ".gif" and not content.startswith((b"GIF87a", b"GIF89a")):
            raise AttachmentError("GIF 文件内容与扩展名不匹配。")
        elif suffix == ".webp" and not (
            content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP"
        ):
            raise AttachmentError("WebP 文件内容与扩展名不匹配。")

    # 作用：为已验证文件生成可信 MIME 类型，未知文本类型才采用客户端声明。
    # 参数 filename：用户提交或系统处理的附件文件名。
    # 参数 claimed：客户端声明的 MIME 类型。
    @staticmethod
    # 作用：执行“canonical_mime”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 filename：调用方传入的filename，用于本次处理。
    # 参数 claimed：调用方传入的claimed，用于本次处理。
    def _canonical_mime(filename: str, claimed: str) -> str:
        suffix = Path(filename).suffix.lower()
        mapping = {
            ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp",
        }
        return mapping.get(suffix, claimed.strip()[:120] or "text/plain")

    # 作用：校验会话 ID 格式，避免非法键值和路径上下文混入。
    # 参数 session_id：当前用户会话的唯一标识。
    @classmethod
    # 作用：执行“validate_session”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
    def _validate_session(cls, session_id: str) -> None:
        if not cls.SESSION_PATTERN.fullmatch(session_id):
            raise AttachmentError("会话 ID 无效。")

    # 作用：校验附件 ID 是否符合系统生成的固定格式。
    # 参数 attachment_id：系统生成的附件唯一标识。
    @classmethod
    # 作用：执行“validate_id”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 attachment_id：调用方传入的attachment_id，用于本次处理。
    def _validate_id(cls, attachment_id: str) -> None:
        if not cls.ID_PATTERN.fullmatch(attachment_id):
            raise AttachmentError("附件 ID 无效。")
