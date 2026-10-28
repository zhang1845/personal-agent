"""JSON CLI and standalone nanobot configuration export."""

import argparse
import json
import sqlite3
from pathlib import Path
import sys

from .errors import AgentError
from .mcp_server import TOOL_NAMES, serve

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "output" / "runtime" / "calendar.sqlite3"


def build_config(db_path, python_executable=None, bridge_config=None, with_knowledge=False):
    arguments = ["-m", "personal_agent", "--db", str(Path(db_path).expanduser().resolve())]
    if bridge_config:
        arguments.extend(["--bridge-config", str(Path(bridge_config).expanduser().resolve())])
    arguments.append("mcp")
    if with_knowledge: arguments.append("--with-knowledge")
    from .knowledge import KNOWLEDGE_TOOLS
    enabled = sorted(TOOL_NAMES | ({item["name"] for item in KNOWLEDGE_TOOLS} if with_knowledge else set()))
    return {
        "providers": {"ollama": {"apiBase": "http://127.0.0.1:11434/v1"}},
        "modelPresets": {"local-calendar": {"provider": "ollama", "model": "qwen3:8b", "maxTokens": 2048, "contextWindowTokens": 8192, "temperature": 0.1}},
        "agents": {"defaults": {"modelPreset": "local-calendar", "fallbackModels": []}},
        "tools": {"mcpServers": {"personal-calendar": {
            "command": python_executable or sys.executable,
            "args": arguments,
            "env": {"PYTHONPATH": str(PROJECT_ROOT / "code"), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            "enabledTools": enabled,
        }}},
    }


def parser():
    root = argparse.ArgumentParser(description="本地个人日程工具。真实日历和本地模型需另行配置，不自动收费或联网授权。")
    root.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 文件路径，默认保存在项目 output/runtime")
    root.add_argument("--bridge-config", help="可选的外部 MCP 日历桥接配置文件；不自动取得账号授权")
    commands = root.add_subparsers(dest="command", required=True)
    call = commands.add_parser("tool", help="执行结构化日程工具并输出 JSON")
    call.add_argument("name", choices=sorted(TOOL_NAMES))
    call.add_argument("--args", default="{}", help="JSON 对象参数")
    mcp = commands.add_parser("mcp", help="启动 MCP stdio 服务，stdout 仅用于协议")
    mcp.add_argument("--with-knowledge", action="store_true", help="启用同数据目录的只读项目和资料工具")
    web = commands.add_parser("web", help="启动本机个人 Agent 网页工作台")
    web.add_argument("--port", type=int, default=8787, help="本机端口，默认 8787")
    web.add_argument("--data-dir", help="日历、对话、任务、记忆的数据目录，默认 output/runtime")
    web.add_argument("--open", action="store_true", help="启动后在默认浏览器打开")
    export = commands.add_parser("config", help="导出独立 nanobot 本地模型与 MCP 配置，不修改已有配置")
    export.add_argument("--with-knowledge", action="store_true", help="包含只读项目资料工具")
    export.add_argument("--python", default=sys.executable, help="运行 MCP 的 Python 绝对路径")
    snapshot = commands.add_parser("export", help="导出离线 ICS 快照，非持续同步，不发送手机提醒")
    snapshot.add_argument("--start", required=True, help="查询起点 ISO 日期或时间，包含")
    snapshot.add_argument("--end", required=True, help="查询终点 ISO 日期或时间，不包含")
    snapshot.add_argument("--output", required=True, help="新 ICS 文件路径；已存在时拒绝覆盖")
    snapshot.add_argument("--calendar-id", help="日历 ID，省略时使用默认日历")
    return root


def main(argv=None):
    from .platform_utils import configure_stdio
    configure_stdio()
    args = parser().parse_args(argv)
    if args.command == "config":
        print(json.dumps(build_config(args.db, args.python, args.bridge_config, args.with_knowledge), ensure_ascii=False, indent=2))
        return 0
    try:
        from .service import CalendarService
        if args.command == "web":
            if not 0 <= args.port <= 65535:
                raise AgentError("validation_error", "端口必须位于 0 到 65535")
            if args.data_dir:
                args.db = str(Path(args.data_dir).expanduser().resolve() / "calendar.sqlite3")
        backend = None
        if args.bridge_config:
            from .bridge import MCPBridge
            path = Path(args.bridge_config).expanduser().resolve()
            if path.stat().st_size > 1024 * 1024:
                raise AgentError("invalid_config", "连接器配置超过大小上限")
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise AgentError("invalid_config", "连接器配置必须是合法 UTF-8 JSON")
            backend = MCPBridge(config)
        service = CalendarService(str(Path(args.db).expanduser().resolve()), backend=backend)
        try:
            if args.command == "web":
                from .webapp import run_web
                return run_web(Path(args.db).expanduser().resolve().parent, args.port, args.open, service)
            if args.command == "mcp":
                from .knowledge import KnowledgeStore
                knowledge = KnowledgeStore(Path(args.db).expanduser().resolve().parent / "knowledge.sqlite3") if args.with_knowledge else None
                serve(service, knowledge=knowledge)
                return 0
            if args.command == "export":
                from .ics import write_snapshot
                query = {"start": args.start, "end": args.end}
                if args.calendar_id:
                    query["calendar_id"] = args.calendar_id
                result = service.call("calendar_list_events", query)
                exported = write_snapshot(args.output, result["events"], result["timezone"])
                print(json.dumps(exported, ensure_ascii=False, indent=2))
                return 0
            try:
                values = json.loads(args.args)
            except ValueError:
                raise AgentError("invalid_arguments", "--args 必须是合法 JSON 对象")
            if not isinstance(values, dict):
                raise AgentError("invalid_arguments", "--args 必须是 JSON 对象")
            result = service.call(args.name, values)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        finally:
            close = getattr(service, "close", None)
            if close:
                close()
    except AgentError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr if args.command == "mcp" else sys.stdout)
        return 1
    except (ImportError, OSError, sqlite3.Error):
        print(json.dumps({"error": {"code": "startup_failed", "message": "服务未能启动，请检查安装与数据库目录权限。"}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
