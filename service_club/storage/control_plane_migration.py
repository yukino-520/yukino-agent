from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from service_club.capabilities.policy import CapabilityPolicyStore
from service_club.capabilities.workflows import WorkflowRunStore
from service_club.core.memory import MemoryManager
from service_club.core.memory.conversations import ConversationStore
from service_club.core.memory.knowledge_graph import KnowledgeGraphStore
from service_club.core.memory.permanent_memory import PermanentMemoryManager
from service_club.core.runtime.agent_request_store import AgentRequestStore
from service_club.core.runtime.agent_task_store import AgentTaskStore
from service_club.core.runtime.attachment_store import AttachmentStore
from service_club.core.runtime.background_jobs import BackgroundJobStore
from service_club.core.runtime.channel_security import InboundChannelSecurity
from service_club.core.runtime.effect_receipts import EffectReceiptStore
from service_club.core.runtime.external_dispatch_store import ExternalDispatchStore
from service_club.core.tooling.operation_store import AgentOperationStore
from service_club.storage.legacy_sqlite import SQLiteRelationalBackend
from service_club.storage.relational import (
    RelationalBackend,
)

_TABLES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "agent_requests",
        (
            "session_id",
            "request_id",
            "request_hash",
            "status",
            "response_json",
            "error",
            "started_at",
            "updated_at",
        ),
        ("session_id", "request_id"),
    ),
    (
        "capability_policy_denials",
        ("capability", "action", "created_at", "updated_at"),
        ("capability", "action"),
    ),
    (
        "capability_temporary_grants",
        (
            "id",
            "session_id",
            "capability",
            "action",
            "status",
            "remaining_uses",
            "expires_at",
            "created_at",
            "updated_at",
            "revoked_at",
        ),
        ("id",),
    ),
    (
        "workflow_runs",
        ("id", "status", "state_json", "error", "created_at", "updated_at"),
        ("id",),
    ),
    (
        "background_jobs",
        (
            "id",
            "kind",
            "session_id",
            "task_id",
            "related_task_id",
            "dedupe_key",
            "payload_json",
            "status",
            "attempts",
            "max_attempts",
            "available_at",
            "lease_token",
            "lease_until",
            "cancel_requested",
            "result_json",
            "error",
            "created_at",
            "updated_at",
            "completed_at",
        ),
        ("id",),
    ),
    (
        "agent_operations",
        (
            "id",
            "session_id",
            "task_id",
            "kind",
            "action",
            "payload_json",
            "payload_hash",
            "summary",
            "status",
            "result_json",
            "error",
            "created_at",
            "expires_at",
            "updated_at",
        ),
        ("id",),
    ),
    (
        "agent_effect_receipts",
        (
            "id",
            "task_id",
            "session_id",
            "action",
            "idempotency_key",
            "status",
            "evidence_json",
            "result_json",
            "error",
            "created_at",
            "updated_at",
        ),
        ("id",),
    ),
    (
        "agent_external_dispatches",
        (
            "id",
            "operation_id",
            "session_id",
            "task_id",
            "capability",
            "action",
            "payload_fingerprint",
            "provider_idempotency_key",
            "status",
            "attempts",
            "receipt_json",
            "error",
            "provider_started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ),
        ("operation_id",),
    ),
    (
        "agent_tasks",
        (
            "id",
            "session_id",
            "request_id",
            "goal",
            "request_json",
            "plan_json",
            "contract_json",
            "budget_json",
            "usage_json",
            "checkpoint_json",
            "checkpoint_revision",
            "run_started_at",
            "status",
            "phase",
            "outcome_json",
            "error",
            "cancel_requested",
            "created_at",
            "updated_at",
        ),
        ("id",),
    ),
    (
        "agent_task_steps",
        (
            "id",
            "task_id",
            "sequence",
            "action",
            "status",
            "idempotency_key",
            "input_json",
            "output_json",
            "error",
            "started_at",
            "completed_at",
            "updated_at",
        ),
        ("id",),
    ),
    (
        "agent_task_events",
        (
            "id",
            "task_id",
            "event",
            "status",
            "phase",
            "step_id",
            "action",
            "detail",
            "payload_json",
            "created_at",
        ),
        ("id",),
    ),
    (
        "inbound_channel_events",
        ("channel", "event_id", "received_at"),
        ("channel", "event_id"),
    ),
    (
        "chat_attachments",
        (
            "id",
            "session_id",
            "original_name",
            "relative_path",
            "mime_type",
            "kind",
            "size",
            "sha256",
            "created_at",
        ),
        ("id",),
    ),
    (
        "conversation_threads",
        (
            "id",
            "chat_mode",
            "character",
            "title",
            "preview",
            "turn_count",
            "created_at",
            "updated_at",
        ),
        ("id",),
    ),
    (
        "conversation_logs",
        (
            "id", "session_id", "character", "chat_mode", "user_message",
            "assistant_reply", "emotion", "tool_results_json", "attachments_json",
            "execution_json", "trace_id", "degraded", "degradation_reason",
            "created_at",
        ),
        ("id",),
    ),
    (
        "memories",
        (
            "id", "session_id", "content", "source", "importance",
            "idempotency_key", "created_at",
        ),
        ("id",),
    ),
    (
        "reminders",
        (
            "id", "session_id", "content", "due_at", "timezone", "recurrence",
            "idempotency_key", "status", "claim_token", "claim_until",
            "delivered_at", "last_delivered_at", "delivery_count", "cancelled_at",
            "updated_at", "created_at",
        ),
        ("id",),
    ),
    (
        "user_profile",
        ("session_id", "summary", "updated_at"),
        ("session_id",),
    ),
    (
        "turn_reflections",
        (
            "id", "session_id", "summary", "emotion", "character", "chat_mode",
            "salience", "created_at",
        ),
        ("id",),
    ),
    (
        "emotion_turns",
        ("id", "session_id", "label", "intensity", "created_at"),
        ("id",),
    ),
    (
        "proactive_nudges",
        (
            "id", "session_id", "content", "reason", "emotion", "due_at",
            "status", "created_at", "delivered_at", "updated_at",
        ),
        ("id",),
    ),
    (
        "agent_route_stats",
        ("agent_id", "turns", "successes", "total_latency_ms", "updated_at"),
        ("agent_id",),
    ),
    (
        "affective_states",
        (
            "session_id", "label", "intensity", "pleasure", "arousal",
            "dominance", "updated_at",
        ),
        ("session_id",),
    ),
    (
        "behavior_instincts",
        (
            "id", "session_id", "rule_key", "content", "confidence",
            "evidence_count", "use_count", "status", "source",
            "last_evidence_hash", "created_at", "last_evidence_at", "last_used_at",
            "updated_at",
        ),
        ("id",),
    ),
    (
        "context_checkpoints",
        (
            "session_id", "summary", "source_hash", "covered_messages",
            "original_tokens", "final_tokens", "updated_at",
        ),
        ("session_id",),
    ),
    (
        "relationship_states",
        (
            "session_id", "turns", "trust_score", "quality_score",
            "positive_feedback", "negative_feedback", "support_moments",
            "rupture_count", "repair_count", "repair_progress", "rupture_state",
            "boundaries_json", "last_gap_seconds", "last_interaction_at", "updated_at",
        ),
        ("session_id",),
    ),
    (
        "relationship_events",
        (
            "id", "session_id", "event_type", "signal_key", "evidence_hash",
            "trust_delta", "quality_delta", "created_at",
        ),
        ("id",),
    ),
    (
        "profile_facts",
        (
            "id", "session_id", "fact_key", "category", "value", "polarity",
            "confidence", "evidence_count", "status", "source", "superseded_by",
            "created_at", "last_evidence_at", "updated_at",
        ),
        ("id",),
    ),
    (
        "profile_fact_evidence",
        ("id", "fact_id", "evidence_hash", "created_at"),
        ("id",),
    ),
    (
        "profile_interaction_stats",
        (
            "session_id", "total_messages", "deep_messages", "total_chars",
            "hour_distribution_json", "first_seen_at", "last_active_at",
        ),
        ("session_id",),
    ),
    (
        "memory_embeddings",
        ("memory_id", "model", "dimensions", "vector_json", "updated_at"),
        ("memory_id", "model"),
    ),
    (
        "response_quality_cases",
        (
            "id", "session_id", "character", "issue_key", "dimension", "severity",
            "output_hash", "repair_action", "created_at",
        ),
        ("id",),
    ),
    (
        "turn_strategy_snapshots",
        (
            "id", "session_id", "character", "response_hash", "strategy_signature",
            "opening_move", "advice_mode", "question_budget", "roleplay_intensity",
            "created_at",
        ),
        ("id",),
    ),
    (
        "interaction_outcomes",
        (
            "id", "session_id", "character", "sentiment", "signal_key",
            "strategy_signature", "response_hash", "evidence_hash", "created_at",
        ),
        ("id",),
    ),
    (
        "agent_self_checkpoints",
        (
            "id", "session_id", "character", "checkpoint_date",
            "relationship_stage", "rupture_state", "trust_band",
            "milestone_keys_json", "adjustment_keys_json", "lesson_keys_json",
            "source_counts_json", "evidence_fingerprint", "created_at", "updated_at",
        ),
        ("id",),
    ),
    (
        "knowledge_entities",
        (
            "id", "session_id", "label", "entity_type", "normalized_label",
            "properties_json", "confidence", "source", "status", "created_at",
            "updated_at",
        ),
        ("id",),
    ),
    (
        "knowledge_relations",
        (
            "id", "session_id", "source_id", "relation_type", "target_id",
            "properties_json", "confidence", "source", "evidence_hash", "status",
            "created_at", "updated_at",
        ),
        ("id",),
    ),
    (
        "permanent_memories",
        (
            "user_id", "key", "category", "value", "source", "timestamp",
            "updated_at",
        ),
        ("user_id", "key"),
    ),
)

_INSERT_ONLY_TABLES = {
    "agent_task_events",
    "inbound_channel_events",
    "chat_attachments",
    "conversation_logs",
    "memories",
    "turn_reflections",
    "emotion_turns",
    "relationship_events",
    "profile_fact_evidence",
    "response_quality_cases",
    "turn_strategy_snapshots",
    "interaction_outcomes",
}
_VERSION_COLUMNS = {
    "profile_interaction_stats": "last_active_at",
}
_IDENTITY_TABLES = (
    ("agent_effect_receipts", "id"),
    ("agent_task_steps", "id"),
    ("agent_task_events", "id"),
    ("conversation_logs", "id"),
    ("memories", "id"),
    ("reminders", "id"),
    ("turn_reflections", "id"),
    ("emotion_turns", "id"),
    ("proactive_nudges", "id"),
    ("behavior_instincts", "id"),
    ("relationship_events", "id"),
    ("profile_facts", "id"),
    ("profile_fact_evidence", "id"),
    ("response_quality_cases", "id"),
    ("turn_strategy_snapshots", "id"),
    ("interaction_outcomes", "id"),
    ("agent_self_checkpoints", "id"),
)


# 作用：把 SQLite 控制面事实幂等迁移到目标关系库，并阻止旧数据覆盖新版本。
# 参数 source_sqlite：控制面迁移使用的 SQLite 源文件。
# 参数 destination：控制面数据要迁入的目标关系存储后端。
# 参数 dry_run：是否只生成迁移报告而不写入目标库。
def migrate_control_plane(
    source_sqlite: str | Path,
    destination: RelationalBackend,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Move permission and workflow facts without downgrading newer rows.

    The migration is additive and idempotent.  A destination row with a newer
    ``updated_at`` wins, so rerunning an old SQLite export cannot roll back a
    live PostgreSQL control plane.
    """

    source_path = Path(source_sqlite).resolve()
    source = SQLiteRelationalBackend(source_path)
    if source.location == destination.location:
        raise ValueError("迁移源和目标不能是同一个数据库。")
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite 迁移源不存在：{source_path}")
    snapshots = {
        table: (
            _read_table(source, table, columns)
            if _sqlite_table_exists(source, table)
            else []
        )
        for table, columns, _keys in _TABLES
    }
    report: dict[str, Any] = {
        "source": str(source_path),
        "destination": destination.location,
        "dry_run": dry_run,
        "tables": {},
        "source_rows": sum(len(rows) for rows in snapshots.values()),
        "applied_rows": 0,
        "newer_destination_rows": 0,
        "verified": False,
    }
    if dry_run:
        report["tables"] = {
            table: {"source_rows": len(rows), "applied_rows": 0}
            for table, rows in snapshots.items()
        }
        return report

    CapabilityPolicyStore(backend=destination)
    WorkflowRunStore(backend=destination)
    AgentRequestStore(backend=destination)
    BackgroundJobStore(backend=destination)
    AgentOperationStore(backend=destination)
    EffectReceiptStore(backend=destination)
    ExternalDispatchStore(backend=destination)
    AgentTaskStore(backend=destination)
    InboundChannelSecurity(backend=destination)
    AttachmentStore(
        backend=destination,
        root=source_path.parent / "workspace" / "attachments",
    )
    destination_memory = MemoryManager(backend=destination)
    destination_memory.init()
    PermanentMemoryManager(
        source_path.parent,
        backend=destination,
        migrate_legacy=False,
    )
    KnowledgeGraphStore(destination)
    ConversationStore(destination_memory, backend=destination)
    for table, columns, keys in _TABLES:
        applied = _upsert_table(
            destination,
            table,
            columns,
            keys,
            snapshots[table],
        )
        report["tables"][table] = {
            "source_rows": len(snapshots[table]),
            "applied_rows": applied,
            "source_fingerprint": _fingerprint(snapshots[table], keys),
        }
        report["applied_rows"] += applied
    with destination.connect(immediate=True) as connection:
        for table, column in _IDENTITY_TABLES:
            destination.reseed_identity(connection, table, column)
    destination_memory.backfill_pgvector()

    verified, newer = _verify(destination, snapshots)
    report["verified"] = verified
    report["newer_destination_rows"] = newer
    if not verified:
        raise RuntimeError("控制面迁移校验失败；目标库缺少记录或包含更旧版本。")
    return report


# 作用：兼容旧表缺失版本列的情况，读取迁移所需列为字典快照。
# 参数 backend：所使用的存储、执行后端或后端标识。
# 参数 table：要查询、迁移或维护的数据表名称。
# 参数 columns：迁移或查询需要处理的列名序列。
def _read_table(
    backend: RelationalBackend,
    table: str,
    columns: tuple[str, ...],
) -> list[dict[str, Any]]:
    selected_columns = list(columns)
    if (
        table == "agent_task_steps"
        and backend.name == "sqlite"
        and not _sqlite_column_exists(backend, table, "updated_at")
    ):
        selected_columns[selected_columns.index("updated_at")] = (
            "COALESCE(completed_at, started_at) AS updated_at"
        )
    legacy_versions = {
        "proactive_nudges": "COALESCE(delivered_at, created_at) AS updated_at",
        "behavior_instincts": (
            "COALESCE(last_used_at, last_evidence_at, created_at) AS updated_at"
        ),
        "profile_facts": "COALESCE(last_evidence_at, created_at) AS updated_at",
    }
    if (
        backend.name == "sqlite"
        and table in legacy_versions
        and not _sqlite_column_exists(backend, table, "updated_at")
    ):
        selected_columns[selected_columns.index("updated_at")] = legacy_versions[table]
    selected = ", ".join(selected_columns)
    with backend.connect() as connection:
        rows = connection.execute(f"SELECT {selected} FROM {table}").fetchall()
    return [{column: row[column] for column in columns} for row in rows]


# 作用：检查 SQLite 迁移源中是否存在指定表。
# 参数 backend：所使用的存储、执行后端或后端标识。
# 参数 table：要查询、迁移或维护的数据表名称。
def _sqlite_table_exists(backend: SQLiteRelationalBackend, table: str) -> bool:
    with backend.connect() as connection:
        row = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table,),
        ).fetchone()
    return row is not None


# 作用：通过 PRAGMA 检查旧版 SQLite 表是否包含指定列。
# 参数 backend：所使用的存储、执行后端或后端标识。
# 参数 table：要查询、迁移或维护的数据表名称。
# 参数 column：要检查或重置的数据库列名。
def _sqlite_column_exists(
    backend: SQLiteRelationalBackend,
    table: str,
    column: str,
) -> bool:
    with backend.connect() as connection:
        return any(
            str(row[1]) == column
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        )


# 作用：按业务键批量插入或仅用更新版本覆盖目标记录。
# 参数 backend：所使用的存储、执行后端或后端标识。
# 参数 table：要查询、迁移或维护的数据表名称。
# 参数 columns：迁移或查询需要处理的列名序列。
# 参数 keys：唯一定位迁移记录的业务键列。
# 参数 rows：要迁移、写入或计算指纹的数据行集合。
def _upsert_table(
    backend: RelationalBackend,
    table: str,
    columns: tuple[str, ...],
    keys: tuple[str, ...],
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in columns)
    selected = ", ".join(columns)
    conflict = ", ".join(keys)
    if table in _INSERT_ONLY_TABLES:
        statement = f"""
            INSERT INTO {table} ({selected}) VALUES ({placeholders})
            ON CONFLICT ({conflict}) DO NOTHING
        """
    else:
        version_column = _VERSION_COLUMNS.get(table, "updated_at")
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in keys
        )
        statement = f"""
            INSERT INTO {table} ({selected}) VALUES ({placeholders})
            ON CONFLICT ({conflict}) DO UPDATE SET {updates}
            WHERE excluded.{version_column} > {table}.{version_column}
        """
    applied = 0
    with backend.connect(immediate=True) as connection:
        for row in rows:
            cursor = connection.execute(
                statement,
                tuple(row[column] for column in columns),
            )
            applied += max(0, int(cursor.rowcount or 0))
    return applied


# 作用：逐表验证目标记录不缺失、不更旧且同版本内容一致。
# 参数 backend：所使用的存储、执行后端或后端标识。
# 参数 source：待读取、迁移、验证或解析的源数据。
def _verify(
    backend: RelationalBackend,
    source: dict[str, list[dict[str, Any]]],
) -> tuple[bool, int]:
    newer = 0
    for table, columns, keys in _TABLES:
        destination_rows = _read_table(backend, table, columns)
        destination_by_key = {
            _key(row, keys): row for row in destination_rows
        }
        for source_row in source[table]:
            destination_row = destination_by_key.get(_key(source_row, keys))
            if destination_row is None:
                return False, newer
            if table in _INSERT_ONLY_TABLES:
                if _fingerprint([source_row], keys) != _fingerprint(
                    [destination_row], keys
                ):
                    return False, newer
                continue
            version_column = _VERSION_COLUMNS.get(table, "updated_at")
            source_updated = float(source_row[version_column])
            destination_updated = float(destination_row[version_column])
            if destination_updated < source_updated:
                return False, newer
            if destination_updated > source_updated:
                newer += 1
                continue
            if _fingerprint([source_row], keys) != _fingerprint(
                [destination_row], keys
            ):
                return False, newer
    return True, newer


# 作用：从数据行提取复合业务键。
# 参数 row：待转换、取键或校验的数据库记录。
# 参数 keys：唯一定位迁移记录的业务键列。
def _key(row: dict[str, Any], keys: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row[key] for key in keys)


# 作用：按业务键稳定排序并计算记录集合的 SHA-256 指纹。
# 参数 rows：要迁移、写入或计算指纹的数据行集合。
# 参数 keys：唯一定位迁移记录的业务键列。
def _fingerprint(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    ordered = sorted(rows, key=lambda row: tuple(str(row[key]) for key in keys))
    payload = json.dumps(
        ordered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
