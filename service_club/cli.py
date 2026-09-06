"""Command-line entrypoints for AGI Yukino's native Service Club runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from service_club.core.agent import ServiceClubCore
from service_club.core.runtime.doctor import ServiceClubDoctor
from service_club.core.types import ChatMessage, ChatRequest
from service_club.storage.control_plane_migration import migrate_control_plane
from service_club.storage.relational import PostgresRelationalBackend, RelationalBackendUnavailable

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# 作用：从版本文件读取发布版本，缺失时返回开发标识。
# 参数：无。
def _version() -> str:
    try:
        return (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"


# 作用：构建服务、聊天、诊断及存储运维命令的参数解析器。
# 参数：无。
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agi-yukino",
        description="AGI Yukino — 原生侍奉部多角色情感陪伴 Agent",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    subcommands = parser.add_subparsers(dest="command")

    serve = subcommands.add_parser("serve", help="启动 Web 活动室")
    serve.add_argument("--host", default=os.getenv("YUKINO_HOST", "127.0.0.1"))
    serve.add_argument(
        "--port", type=int, default=int(os.getenv("YUKINO_PORT", "8082"))
    )
    serve.add_argument("--reload", action="store_true", help="开发模式热重载")

    chat = subcommands.add_parser("chat", help="进入终端私聊")
    chat.add_argument(
        "--character",
        choices=("yukino", "yui", "hachiman", "iroha", "shizuka"),
        default="yukino",
    )
    chat.add_argument("--club", action="store_true", help="启用侍奉部群像模式")
    chat.add_argument("--session", default="cli")

    doctor = subcommands.add_parser("doctor", help="检查原生运行时")
    doctor.add_argument("--json", action="store_true", dest="json_output")

    usage = subcommands.add_parser("usage", help="查看模型用量")
    usage.add_argument("--days", type=int, default=1)
    usage.add_argument("--json", action="store_true", dest="json_output")

    storage = subcommands.add_parser("storage", help="检查或迁移持久存储")
    storage_commands = storage.add_subparsers(dest="storage_command")
    storage_status = storage_commands.add_parser("status", help="检查 PostgreSQL、Elasticsearch 与 Kafka")
    storage_status.add_argument("--json", action="store_true", dest="json_output")
    migrate = storage_commands.add_parser(
        "migrate-control-plane",
        help="迁移 Agent 控制面账本到 YUKINO_POSTGRES_DSN",
    )
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--confirmed", action="store_true")
    migrate.add_argument("--source-sqlite", required=True, type=Path)
    migrate.add_argument("--json", action="store_true", dest="json_output")
    vector_status = storage_commands.add_parser(
        "vector-status", help="检查 Milvus / pgvector 检索后端"
    )
    vector_status.add_argument("--json", action="store_true", dest="json_output")
    vector_rebuild = storage_commands.add_parser(
        "rebuild-vectors", help="从关系事实库重建 Milvus 索引"
    )
    vector_rebuild.add_argument("--drop-existing", action="store_true")
    vector_rebuild.add_argument("--confirmed", action="store_true")
    vector_rebuild.add_argument("--json", action="store_true", dest="json_output")
    search_status = storage_commands.add_parser(
        "search-status", help="检查 Elasticsearch 记忆与文档索引"
    )
    search_status.add_argument("--json", action="store_true", dest="json_output")
    search_rebuild = storage_commands.add_parser(
        "rebuild-search", help="从 PostgreSQL 重建 Elasticsearch 记忆与文档索引"
    )
    search_rebuild.add_argument("--json", action="store_true", dest="json_output")
    graph_status = storage_commands.add_parser(
        "graph-status", help="检查关系图谱和 Neo4j 投影"
    )
    graph_status.add_argument("--json", action="store_true", dest="json_output")
    graph_rebuild = storage_commands.add_parser(
        "rebuild-graph", help="从关系图谱事实重建 Neo4j 投影"
    )
    graph_rebuild.add_argument("--clear-existing", action="store_true")
    graph_rebuild.add_argument("--confirmed", action="store_true")
    graph_rebuild.add_argument("--json", action="store_true", dest="json_output")
    return parser


# 作用：通过 Uvicorn 启动 FastAPI Web 服务。
# 参数 host：监听服务或建立网络连接的主机名。
# 参数 port：监听或连接使用的网络端口。
# 参数 reload：是否启用开发模式热重载。
def _serve(host: str, port: int, reload: bool) -> int:
    import uvicorn

    uvicorn.run(
        "service_club.web.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
    return 0


# 作用：运行保留短期对话历史的终端私聊或群像会话。
# 参数 character：终端会话选用的角色标识。
# 参数 club：是否启用多角色群像会话模式。
# 参数 session_id：隔离会话数据、权限和任务的会话标识。
def _chat(character: str, club: bool, session_id: str) -> int:
    core = ServiceClubCore()
    core.start_data_plane()
    history: list[ChatMessage] = []
    mode = "club" if club else "solo"
    print("\n放学后的侍奉部活动室。输入 /exit 离开。\n")
    try:
        while True:
            try:
                text = input("你 › ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if text in {"/exit", "/quit", "退出"}:
                break
            if not text:
                continue
            history.append(ChatMessage(role="user", content=text))
            reply = core.process(
                ChatRequest(
                    messages=history[-24:],
                    chat_mode=mode,
                    character=character,
                    session_id=session_id,
                )
            )
            history.append(ChatMessage(role="assistant", content=reply.content))
            print(f"{reply.character} › {reply.content}\n")
    finally:
        core.stop_data_plane()
    return 0


# 作用：执行运行时自检，并按文本或 JSON 输出检查结果。
# 参数 json_output：是否以机器可读 JSON 格式输出报告。
def _doctor(json_output: bool) -> int:
    report = ServiceClubDoctor(ServiceClubCore()).run()
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        marker = "OK" if report.get("ok") else "CHECK"
        print(f"AGI Yukino doctor: {marker}")
        for name, result in report.get("checks", {}).items():
            status = "✓" if result.get("ok", True) else "!"
            print(f"  {status} {name}")
    return 0 if report.get("ok") else 1


# 作用：汇总指定天数内的模型调用量、Token 和估算费用。
# 参数 days：用量统计向前覆盖的天数。
# 参数 json_output：是否以机器可读 JSON 格式输出报告。
def _usage(days: int, json_output: bool) -> int:
    report = ServiceClubCore().usage.summary(days=max(1, days))
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"最近 {report['days']} 天：{report['calls']} 次模型调用")
        print(f"输入 {report['input_tokens']} tokens · 输出 {report['output_tokens']} tokens")
        print(f"估算费用 {report['estimated_cost']} {report['currency']}")
    return 0


# 作用：检查生产存储和事件流状态。
# 参数 json_output：是否以机器可读 JSON 格式输出报告。
def _storage_status(json_output: bool) -> int:
    core = ServiceClubCore()
    report = {
        "ok": core.memory.backend.name == "postgresql",
        "backend": core.memory.backend.name,
        "location": core.memory.backend.location,
        "search": core.search_index.status(probe=True),
        "events": core.event_stream.status(probe=True),
    }
    report["ok"] = bool(report["ok"] and report["search"].get("ok"))
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        marker = "OK" if report.get("ok") else "CHECK"
        print(f"AGI Yukino storage: {marker}")
        print(
            f"  backend={report.get('backend')} "
            f"search={report['search'].get('backend')} "
            f"events={report['events'].get('backend')}"
        )
    return 0 if report.get("ok") else 1


# 作用：经显式确认后把控制面账本迁移到 PostgreSQL。
# 参数 dry_run：是否只生成迁移报告而不写入目标库。
# 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
# 参数 json_output：是否以机器可读 JSON 格式输出报告。
def _migrate_control_plane(
    *,
    source_sqlite: Path,
    dry_run: bool,
    confirmed: bool,
    json_output: bool,
) -> int:
    dsn = os.getenv("YUKINO_POSTGRES_DSN", "").strip()
    if not dsn:
        print("缺少 YUKINO_POSTGRES_DSN；不会修改任何数据库。")
        return 2
    if not dry_run and not confirmed:
        print("真实迁移需要 --confirmed；可先使用 --dry-run。")
        return 2
    try:
        report = migrate_control_plane(
            source_sqlite,
            PostgresRelationalBackend(dsn),
            dry_run=dry_run,
        )
    except RelationalBackendUnavailable as exc:
        print(str(exc))
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"控制面迁移失败（{type(exc).__name__}）。请检查数据库连通性和迁移日志。")
        return 1
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        action = "预检" if dry_run else "迁移"
        marker = "OK" if dry_run or report.get("verified") else "CHECK"
        print(f"控制面{action}: {marker}")
        print(
            f"  source_rows={report['source_rows']} "
            f"applied_rows={report['applied_rows']}"
        )
    return 0 if dry_run or report.get("verified") else 1


# 作用：统一打印向量或图存储适配器状态并返回退出码。
# 参数 report：需要格式化输出的存储适配器报告。
# 参数 label：展示报告所用标签或知识图谱实体名称。
# 参数 json_output：是否以机器可读 JSON 格式输出报告。
def _print_storage_adapter(report: dict, *, label: str, json_output: bool) -> int:
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        marker = "OK" if report.get("ok", True) else "CHECK"
        print(f"AGI Yukino {label}: {marker}")
        print(
            f"  backend={report.get('backend', report.get('fact_backend', 'unknown'))} "
            f"enabled={report.get('enabled', True)}"
        )
    return 0 if report.get("ok", True) else 1


# 作用：探测当前向量索引后端，未启用时说明关系库事实源。
# 参数 json_output：是否以机器可读 JSON 格式输出报告。
def _vector_status(json_output: bool) -> int:
    core = ServiceClubCore()
    report = (
        core.vector_store.status(probe=True)
        if core.vector_store is not None
        else {
            "ok": True,
            "enabled": False,
            "backend": core.memory.backend.name,
            "connection_state": "disabled",
            "source_of_truth": "relational",
        }
    )
    return _print_storage_adapter(report, label="vector", json_output=json_output)


def _search_status(json_output: bool) -> int:
    core = ServiceClubCore()
    return _print_storage_adapter(
        core.search_index.status(probe=True), label="search", json_output=json_output
    )


def _rebuild_search(json_output: bool) -> int:
    core = ServiceClubCore()
    memories = core.search_index.bulk_rebuild_memories(
        core.memory.iter_memories(), replace=True
    )
    documents = core.search_index.bulk_rebuild_documents(
        core.knowledge_base.iter_chunks(), replace=True
    )
    report = {"ok": True, "backend": "elasticsearch", "memories": memories, "document_chunks": documents}
    return _print_storage_adapter(report, label="search rebuild", json_output=json_output)


# 作用：按确认策略从关系事实库重建派生向量索引。
# 参数 drop_existing：重建向量索引前是否删除现有集合。
# 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
# 参数 json_output：是否以机器可读 JSON 格式输出报告。
def _rebuild_vectors(
    *, drop_existing: bool, confirmed: bool, json_output: bool
) -> int:
    if drop_existing and not confirmed:
        print("删除现有 Milvus Collection 需要 --confirmed。")
        return 2
    core = ServiceClubCore()
    report = core.memory.rebuild_vector_index(drop_existing=drop_existing)
    document_report = core.knowledge_base.rebuild_vector_index()
    report["document_chunks"] = document_report.get("indexed", 0)
    report["document_failed"] = document_report.get("failed", 0)
    report["document_vectors"] = document_report
    report["ok"] = bool(not report.get("enabled") or report.get("failed", 0) == 0)
    report["ok"] = bool(report["ok"] and document_report.get("failed", 0) == 0)
    return _print_storage_adapter(report, label="vector rebuild", json_output=json_output)


# 作用：探测知识图谱事实库及 Neo4j 投影状态。
# 参数 json_output：是否以机器可读 JSON 格式输出报告。
def _graph_status(json_output: bool) -> int:
    report = ServiceClubCore().capabilities.knowledge_graph.status(probe=True)
    return _print_storage_adapter(report, label="graph", json_output=json_output)


# 作用：按确认策略从图谱事实重建 Neo4j 派生投影。
# 参数 clear_existing：重建图投影前是否先清空现有派生数据。
# 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
# 参数 json_output：是否以机器可读 JSON 格式输出报告。
def _rebuild_graph(
    *, clear_existing: bool, confirmed: bool, json_output: bool
) -> int:
    if clear_existing and not confirmed:
        print("清空现有 Neo4j 投影需要 --confirmed。")
        return 2
    core = ServiceClubCore()
    report = core.capabilities.knowledge_graph.rebuild_projection(
        clear_existing=clear_existing
    )
    report["ok"] = bool(not report.get("enabled") or report.get("failed", 0) == 0)
    return _print_storage_adapter(report, label="graph rebuild", json_output=json_output)


# 作用：解析命令行参数并分派到对应运行或运维入口。
# 参数 argv：待解析的命令行参数；为空时读取进程参数。
def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args.host, args.port, args.reload)
    if args.command == "chat":
        return _chat(args.character, args.club, args.session)
    if args.command == "doctor":
        return _doctor(args.json_output)
    if args.command == "usage":
        return _usage(args.days, args.json_output)
    if args.command == "storage":
        if args.storage_command == "status":
            return _storage_status(args.json_output)
        if args.storage_command == "migrate-control-plane":
            return _migrate_control_plane(
                source_sqlite=args.source_sqlite,
                dry_run=args.dry_run,
                confirmed=args.confirmed,
                json_output=args.json_output,
            )
        if args.storage_command == "vector-status":
            return _vector_status(args.json_output)
        if args.storage_command == "rebuild-vectors":
            return _rebuild_vectors(
                drop_existing=args.drop_existing,
                confirmed=args.confirmed,
                json_output=args.json_output,
            )
        if args.storage_command == "search-status":
            return _search_status(args.json_output)
        if args.storage_command == "rebuild-search":
            return _rebuild_search(args.json_output)
        if args.storage_command == "graph-status":
            return _graph_status(args.json_output)
        if args.storage_command == "rebuild-graph":
            return _rebuild_graph(
                clear_existing=args.clear_existing,
                confirmed=args.confirmed,
                json_output=args.json_output,
            )
        parser.parse_args(["storage", "--help"])
    parser.print_help()
    return 0
