"""A loopback-only, buildless web workbench. Python standard library only."""

from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import os
import secrets
import sqlite3
from socketserver import TCPServer
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import AgentError
from .personal_store import PersonalStore, checked_model
from .service import CalendarService
from .timeutils import zone

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_BODY = 65536
CALENDAR_WRITES = {"calendar_create_event", "calendar_update_event", "calendar_delete_event", "calendar_set_preferences"}


def fields(values, allowed, required=()):
    if set(values) - set(allowed) or not set(required) <= set(values):
        raise AgentError("validation_error", "参数缺失或包含不支持的字段")
    return values


def task_version(body, required=True):
    if not required and "expected_version" not in body:
        return None
    value = body.get("expected_version")
    if type(value) is not int or value < 1:
        raise AgentError("validation_error", "请提供最新的任务版本")
    return value


class Application:
    def __init__(self, data_dir, engine=None, model_client=None, calendar=None):
        from .assistant import AgentEngine
        from .ollama_client import OllamaClient
        data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir = data_dir
        self.service_instance = secrets.token_hex(16)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.calendar = calendar or CalendarService(str(data_dir / "calendar.sqlite3"))
        self.store = PersonalStore(data_dir / "assistant.sqlite3")
        from .task_planning import TaskPlanner
        self.task_planner = TaskPlanner(self.calendar, self.store)
        self.model_client = model_client or OllamaClient()
        self.engine = engine or AgentEngine(self.calendar, self.store, self.model_client)
        from .knowledge import KnowledgeStore
        self.knowledge = KnowledgeStore(data_dir / "knowledge.sqlite3")
        if engine is None:
            self.engine.knowledge = self.knowledge
        from .api_model import APISettings
        self.api_settings = APISettings(data_dir / "model-api.json")
        from .importing import ImportService
        from .automations import Automations
        from .caldav_sync import CalDAVSync
        from .execution import ExecutionLog
        self.importer = ImportService(self.knowledge)
        self.automations = Automations(data_dir, self.calendar, self.store)
        self.caldav = CalDAVSync(data_dir, self.calendar)
        self.execution = ExecutionLog(data_dir / "agent-runs.sqlite3")
        if engine is None and model_client is None:
            from .nanobot_engine import NanobotEngine
            self.engine = NanobotEngine(self.calendar, self.store, self.model_client, self.knowledge, workspace=data_dir / "nanobot", execution=self.execution)
        from .chat_integrations import ChatIntegrations
        self.chat_integrations = ChatIntegrations(data_dir, self.knowledge, self.importer, self.automations, self.caldav, self.calendar, self.store)
        self.engine.integrations = self.chat_integrations
        from .quality import QualityStore
        self.quality = QualityStore(data_dir, self.store, self.knowledge)
        self.token = secrets.token_urlsafe(32)
        self.action_lock = threading.RLock()
        self.chat_lock = threading.RLock()

    def chat_result(self, result):
        if result.get('message_id'):
            self.quality.record_run(result['message_id'], result.get('run_id'))
        return result

    def close(self):
        self.calendar.close()
        self.store.close()
        self.knowledge.close()
        self.automations.close()

    def apply(self, action_id, confirmation=None):
        with self.action_lock:
            action = self.store.get_action(action_id)
            if action["status"] == "applied":
                return {"action": action, "result": action["result"]}
            if action["status"] != "pending":
                raise AgentError("action_unavailable", "该草案已执行失败或结果待核实，请查看记录后重新发起", {"action": action})
            tool, args = action["tool"], action["arguments"]
            try:
                from .chat_integrations import WRITE_NAMES as CHAT_WRITES
                if tool in CHAT_WRITES:
                    result = self.chat_integrations.apply(tool, args, action_id, confirmation)
                elif tool in CALENDAR_WRITES:
                    result = self.calendar.call(tool, args)
                elif tool == "assistant_add_task":
                    fields(args, {"title", "due_date"}, {"title"})
                    result = {"task": self.store.add_task(source_action_id=action_id, **args)}
                elif tool == "assistant_complete_task":
                    fields(args, {"task_id"}, {"task_id"})
                    result = {"task": self.store.set_task_completed(args["task_id"], True)}
                elif tool == "assistant_save_memory":
                    fields(args, {"content"}, {"content"})
                    result = {"memory": self.store.save_memory(source_action_id=action_id, **args)}
                else:
                    raise AgentError("unsupported_tool", "该操作不在允许的工具范围内")
            except AgentError as exc:
                # Calendar request IDs and local source_action_id also protect retries after a process crash.
                if exc.code == "confirmation_required":
                    raise
                status = "unknown" if exc.code in ("unknown_result", "write_outcome_unknown", "unknown_outcome") else "failed"
                self.store.finish_action(action_id, status, {"error": exc.as_dict()})
                raise
            saved = self.store.finish_action(action_id, "applied", result)
            return {"action": saved, "result": result}

    def knowledge_route(self, method, path, query, body):
        library = self.knowledge
        parts = path.strip("/").split("/")
        group = parts[2] if len(parts) >= 3 else ""
        if group == "projects":
            fields(query, set())
            if len(parts) == 3:
                if method == "GET": return {"projects": library.projects()}
                if method == "POST": return {"project": library.save_project(body)}
            if len(parts) == 4:
                if method == "GET": return {"project": library.project(parts[3])}
                if method == "PATCH": return {"project": library.save_project(body, parts[3])}
                if method == "DELETE":
                    fields(body, {"expected_version"}, {"expected_version"})
                    return library.delete_project(parts[3], body["expected_version"])
        if group == "sources":
            if len(parts) == 3:
                if method == "GET":
                    fields(query, {"query", "project_id", "limit"})
                    args = dict(query)
                    if "limit" in args: args["limit"] = int(args["limit"])
                    return library.search(**args)
                if method == "POST":
                    fields(query, set())
                    return {"source": library.save_source(body)}
            if len(parts) == 4:
                fields(query, set())
                if method == "GET": return {"source": library.source(parts[3])}
                if method == "PATCH": return {"source": library.save_source(body, parts[3])}
                if method == "DELETE":
                    fields(body, {"expected_version"}, {"expected_version"})
                    return library.delete_source(parts[3], body["expected_version"])
        if group == "workstyle" and len(parts) == 3:
            fields(query, set())
            if method == "GET": return {"workstyle": library.get_workstyle()}
            if method == "PUT":
                fields(body, {"content", "expected_version"}, {"content", "expected_version"})
                return {"workstyle": library.set_workstyle(body["content"], body["expected_version"])}
        raise AgentError("not_found", "找不到资料接口")

    def integrations_route(self, method, path, query, body):
        parts = path.strip("/").split("/")
        if path == "/api/runtime" and method == "GET":
            from .nanobot_engine import NanobotEngine
            return dict(NanobotEngine.available(), active_engine="nanobot-sdk" if isinstance(self.engine,NanobotEngine) else "rules-adapter", scheduler_running=bool(self.automations.scheduler), service_pid=os.getpid(), project_root=str(PROJECT_ROOT), data_dir=str(self.data_dir), service_instance=self.service_instance)
        if path == "/api/agent-runs" and method == "GET":
            fields(query, {"session_id"})
            return {"runs": self.execution.list(query.get("session_id"))}
        if path == "/api/imports/article" and method == "POST":
            fields(body, {"url","public_dns"}, {"url"})
            return {"preview": self.importer.article(body["url"],body.get("public_dns",False))}
        if path == "/api/imports/document" and method == "POST":
            fields(body, {"filename","data"}, {"filename","data"})
            return {"preview": self.importer.document(body["filename"],body["data"])}
        if path == "/api/automations":
            if method == "GET": return {"jobs":self.automations.jobs(),"inbox":self.automations.inbox(),"notifications":self.automations.settings()}
            if method == "POST": return {"job":self.automations.save_job(body)}
        if len(parts)==3 and parts[:2]==["api","automations"]:
            if method == "PATCH": return {"job":self.automations.save_job(body,parts[2])}
            if method == "DELETE":
                fields(body,{"expected_version"},{"expected_version"})
                return self.automations.delete_job(parts[2],body["expected_version"])
        if path == "/api/notifications/settings":
            if method == "GET": return {"settings":self.automations.settings()}
            if method == "PUT": return {"settings":self.automations.save_settings(body)}
        if path == "/api/notifications/test" and method == "POST":
            fields(body,set())
            return self.automations.test_notification()
        if len(parts)==4 and parts[:2]==["api","notifications"] and parts[3]=="read" and method=="POST":
            fields(body,set())
            return self.automations.mark_read(parts[2])
        if path == "/api/caldav":
            if method == "GET": return self.caldav.status()
            if method == "PUT": return {"settings":self.caldav.save_settings(body)}
        if path == "/api/caldav/test" and method == "POST":
            fields(body,set())
            return self.caldav.test()
        if path == "/api/caldav/sync" and method == "POST":
            fields(body,{"start","end"},{"start","end"})
            with self.calendar.lock: return self.caldav.sync(body["start"],body["end"])
        if len(parts)==5 and parts[:3]==["api","caldav","operations"] and parts[4]=="reconcile" and method=="POST":
            fields(body,set())
            with self.calendar.lock: return self.caldav.reconcile(parts[3])
        if len(parts)==5 and parts[:3]==["api","caldav","conflicts"] and parts[4]=="resolve" and method=="POST":
            fields(body,{"choice","expected_version"},{"choice","expected_version"})
            with self.calendar.lock:return self.caldav.resolve(parts[3],body["choice"],body["expected_version"])
        raise AgentError("not_found","找不到连接或自动化接口")

    def route(self, method, path, query, body):
        cal, store = self.calendar, self.store
        if path == '/api/quality' or path.startswith('/api/quality/'):
            return self.quality.route(method,path,query,body)
        if path == "/api/chat/attachments" and method == "POST":
            fields(body, {"filename","data","session_id","project_id"}, {"filename","data","session_id"})
            fields(query, set())
            session_id = body["session_id"]
            if not isinstance(session_id,str) or not session_id or len(session_id)>128:
                raise AgentError("validation_error", "对话编号无效")
            self.store.get_session(session_id)
            project_id = self.knowledge.session_project(session_id)
            if body.get("project_id", project_id) != project_id:
                raise AgentError("validation_error", "附件与当前项目不匹配，请开始新对话")
            attachment = self.chat_integrations.upload(body["filename"], body["data"], project_id)
            attachment = self.chat_integrations.bind_attachments([attachment["id"]], session_id, project_id)[0]
            return {"attachment": attachment, "session_id": session_id}
        if path.startswith("/api/chat/previews/") and method == "GET":
            fields(query, {"session_id"}, {"session_id"})
            session_id = query["session_id"]
            project_id = self.knowledge.session_project(session_id)
            return {"preview": self.chat_integrations.get_preview(path.rsplit("/",1)[1],session_id,project_id)}
        if path.startswith(("/api/imports/","/api/automations","/api/notifications/","/api/caldav","/api/agent-runs","/api/runtime")):
            return self.integrations_route(method,path,query,body)
        if path == "/api/model-api":
            fields(query, set())
            if method == "GET": return {"settings": self.api_settings.get()}
            if method == "PUT": return {"settings": self.api_settings.save(body)}
        if method == "POST" and path == "/api/model-api/test":
            fields(query, set())
            fields(body, {"expected_version"}, {"expected_version"})
            config = self.api_settings.snapshot()
            if type(body["expected_version"]) is not int or body["expected_version"] != config["version"]:
                raise AgentError("version_conflict", "配置已变化，请重新加载后测试")
            from .api_model import APIModelClient
            try:
                result = APIModelClient(config).test()
            except AgentError as exc:
                self.api_settings.record_test(config["version"], False, exc.code)
                raise
            return dict(result, settings=self.api_settings.record_test(config["version"], True, "ok"))
        if path.startswith("/api/knowledge/"):
            return self.knowledge_route(method, path, query, body)
        if method == "GET" and path == "/api/bootstrap":
            fields(query, set())
            return {"token": self.token, "status": cal.call("calendar_status", {}), "preferences": cal.call("calendar_get_preferences", {})["preferences"]}
        if path == "/api/preferences":
            fields(query, set())
            if method == "GET":
                return cal.call("calendar_get_preferences", {})
            if method == "PUT":
                return cal.call("calendar_set_preferences", body)
        if method == "GET" and path in ("/api/events", "/api/free-slots", "/api/export"):
            allowed = {"start", "end", "calendar_id"}
            if path == "/api/free-slots":
                allowed |= {"duration_minutes", "limit"}
            fields(query, allowed, {"start", "end"})
            args = dict(query)
            for key in ("duration_minutes", "limit"):
                if key in args:
                    try:
                        args[key] = int(args[key])
                    except ValueError:
                        raise AgentError("validation_error", key + " 必须是整数")
            if path == "/api/free-slots":
                return cal.call("calendar_find_free_slots", args)
            result = cal.call("calendar_list_events", args)
            if path == "/api/export":
                from .ics import calendar_bytes
                return (calendar_bytes(result["events"], result["timezone"]), "text/calendar; charset=utf-8", "attachment; filename=personal-calendar.ics")
            return result
        if method == "POST" and path == "/api/events":
            return cal.call("calendar_create_event", body)
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "events"]:
            if method == "GET":
                return cal.call("calendar_get_event", {"event_id": parts[2]})
            if method in ("PATCH", "DELETE"):
                allowed = {"request_id", "expected_version", "changes", "scope"} if method == "PATCH" else {"request_id", "expected_version", "scope"}
                fields(body, allowed, {"request_id","expected_version","changes"} if method == "PATCH" else {"request_id","expected_version"})
                result = cal.call("calendar_update_event" if method == "PATCH" else "calendar_delete_event", dict(body, event_id=parts[2]))
                if method == "DELETE":
                    store.cancel_event_plans(parts[2])
                return result
        if path == "/api/tasks":
            if method == "GET":
                fields(query, {"deleted"})
                if query.get("deleted", "false") not in ("true", "false"):
                    raise AgentError("validation_error", "deleted 必须为 true 或 false")
                return {"tasks": store.list_tasks(deleted=query.get("deleted") == "true")}
            if method == "POST":
                fields(query, set())
                fields(body, {"title", "due_date"}, {"title"})
                return {"task": store.add_task(**body)}
        if len(parts) >= 3 and parts[:2] == ["api", "tasks"]:
            fields(query, set())
            task_id = parts[2]
            if len(parts) == 3 and method == "GET":
                return {"task": store.get_task(task_id)}
            if len(parts) == 3 and method == "PATCH":
                fields(body, {"title", "due_date", "completed", "expected_version"})
                changes = {key: value for key, value in body.items() if key != "expected_version"}
                return {"task": store.update_task(task_id, changes, task_version(body, required=set(changes) != {"completed"}))}
            if len(parts) == 3 and method == "DELETE":
                fields(body, {"expected_version"})
                return {"task": store.set_task_deleted(task_id, True, task_version(body))}
            if len(parts) == 4 and parts[3] == "restore" and method == "POST":
                fields(body, {"expected_version"})
                return {"task": store.set_task_deleted(task_id, False, task_version(body))}
            if len(parts) == 4 and parts[3] == "schedule":
                if method == "GET":
                    return self.task_planner.reconcile(task_id)
                if method == "POST":
                    return self.task_planner.schedule(task_id, body)
            if len(parts) == 5 and parts[3:] == ["schedule", "retry"] and method == "POST":
                fields(body, set())
                return self.task_planner.retry(task_id)
        if path == "/api/memories":
            if method == "GET":
                return {"memories": store.list_memories()}
            if method == "POST":
                fields(body, {"content"}, {"content"})
                return {"memory": store.save_memory(**body)}
        if len(parts) == 3 and parts[:2] == ["api", "memories"] and method == "DELETE":
            fields(body, set())
            return store.delete_memory(parts[2])
        if method == "POST" and path == "/api/sessions":
            fields(body, {"project_id"})
            project_id = body.get("project_id")
            if project_id is not None: self.knowledge.project(project_id)
            session = store.new_session("新对话")
            self.knowledge.bind_session(session["id"], project_id)
            return {"session": dict(session, project_id=project_id)}
        if method == "GET" and path == "/api/sessions":
            return {"sessions": store.list_sessions()}
        if len(parts) == 3 and parts[:2] == ["api", "sessions"] and method == "GET":
            session = store.get_session(parts[2])
            session.update(messages=store.list_messages(parts[2], 500), actions=store.list_actions(parts[2]), project_id=self.knowledge.session_project(parts[2]))
            session["attachments"] = self.chat_integrations.list_attachments(parts[2], session["project_id"])
            session['feedback'] = [f for f in self.quality.rows('feedback') if f['session_id']==parts[2]]
            for message in session["messages"]:
                message["sources"] = self.knowledge.answer_sources(message["id"])
            return session
        if method == "POST" and path == "/api/chat":
            fields(body, {"message", "session_id", "model", "project_id", "attachment_ids"}, {"message"})
            if "project_id" in body and body["project_id"] is not None:
                self.knowledge.project(body["project_id"])
            attachment_ids = body.get("attachment_ids", [])
            if not isinstance(attachment_ids,list) or len(attachment_ids)>5 or any(not isinstance(x,str) or not x for x in attachment_ids) or len(set(attachment_ids))!=len(attachment_ids):
                raise AgentError("validation_error", "每条消息最多选择5个不同附件")
            model = None
            if body.get("session_id") is not None and not isinstance(body["session_id"], str):
                raise AgentError("validation_error", "session_id 必须是字符串")
            with self.chat_lock:
                config = self.api_settings.snapshot()
                extra = {"project_id": body["project_id"]} if "project_id" in body else {}
                if "attachment_ids" in body: extra["attachment_ids"] = body["attachment_ids"]
                if config["enabled"]:
                    if body.get("model") not in (None, "", config["model"]):
                        raise AgentError("version_conflict", "模型配置已变化，请刷新后发送")
                    from .api_model import APIModelClient
                    previous = self.engine.model_client
                    self.engine.model_client = APIModelClient(config)
                    try:
                        return self.chat_result(self.engine.respond(body["message"], session_id=body.get("session_id"), model=config["model"], model_provider="nanobot" if self.engine.__class__.__name__=="NanobotEngine" else "api", **extra))
                    finally:
                        self.engine.model_client = previous
                model = checked_model(body.get("model", ""))
                return self.chat_result(self.engine.respond(body["message"], session_id=body.get("session_id"), model=model or None, **extra))
        if len(parts) == 4 and parts[:2] == ["api", "actions"] and parts[3] == "apply" and method == "POST":
            fields(body, {"confirm_truncated"})
            if "confirm_truncated" in body and type(body["confirm_truncated"]) is not bool:
                raise AgentError("validation_error", "截选确认无效")
            return self.apply(parts[2], body)
        if method == "GET" and path == "/api/models":
            return self.model_client.list_models()
        if path == "/api/settings":
            if method == "GET":
                return {"settings": store.get_settings()}
            if method == "PUT":
                fields(body, {"model"}, {"model"})
                return {"settings": store.set_settings(body["model"])}
        if method == "GET" and path == "/api/overview":
            fields(query, {"date"})
            prefs = cal.call("calendar_get_preferences", {})["preferences"]
            try:
                day = date.fromisoformat(query["date"]) if "date" in query else datetime.now(zone(prefs["timezone"])).date()
            except (ValueError, TypeError):
                raise AgentError("validation_error", "日期必须为 YYYY-MM-DD")
            result = cal.call("calendar_list_events", {"start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat()})
            tasks, memories = store.list_tasks(), store.list_memories()
            return {"date": day.isoformat(), "timezone": prefs["timezone"], "events": result["events"], "tasks": tasks, "deleted_tasks": store.list_tasks(deleted=True), "memories": memories,
                    "counts": {"events": len(result["events"]), "tasks": sum(not t["completed"] for t in tasks), "memories": len(memories)}}
        raise AgentError("not_found", "找不到该接口")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        # Do not put user text, query strings or tokens into access logs.
        pass

    def send_data(self, status, content, content_type="application/json; charset=utf-8", disposition=None):
        if isinstance(content, dict) and isinstance(content.get("error"), dict):
            content["error"].setdefault("details", {})
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(content)

    def handle_route(self):
        self.connection.settimeout(15)
        app = self.server.application
        try:
            port = self.server.server_address[1]
            host = self.headers.get("Host", "")
            if host not in ("127.0.0.1:%s" % port, "localhost:%s" % port):
                self.send_data(403, {"error": {"code": "forbidden_host", "message": "仅允许本机访问"}})
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + host:
                self.send_data(403, {"error": {"code": "forbidden_origin", "message": "请求来源不匹配"}})
                return
            if self.path == "/api/service/stop":
                stop_token = getattr(self.server, "service_stop_token", "")
                supplied = self.headers.get("X-Agent-Service-Token", "")
                if (self.command != "POST" or origin != "http://" + host or not stop_token
                        or not secrets.compare_digest(supplied, stop_token)):
                    self.send_data(403, {"error": {"code": "forbidden", "message": "无法验证停止请求"}})
                    return
                self.send_data(200, {"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            body = {}
            if self.command in ("POST", "PUT", "PATCH", "DELETE"):
                if origin != "http://" + host or not secrets.compare_digest(self.headers.get("X-Agent-Token", ""), app.token):
                    self.send_data(403, {"error": {"code": "csrf_failed", "message": "请从本地工作台发起操作，或刷新页面后重试"}})
                    return
                if self.headers.get("Transfer-Encoding"):
                    raise AgentError("validation_error", "不支持分块请求")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise AgentError("validation_error", "请求长度无效")
                limit = 12*1024*1024 if urlsplit(self.path).path in ("/api/imports/document", "/api/chat/attachments") else MAX_BODY
                if not 0 <= size <= limit:
                    self.send_data(413, {"error": {"code": "body_too_large", "message": "请求超过此接口的大小限制"}})
                    return
                if size:
                    if self.headers.get_content_type() != "application/json":
                        raise AgentError("validation_error", "请求必须使用 JSON")
                    try:
                        raw = self.rfile.read(size)
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        raise AgentError("validation_error", "请求必须为 UTF-8 JSON 对象")
                    if not isinstance(body, dict):
                        raise AgentError("validation_error", "请求必须为 JSON 对象")
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or len(self.path) > 8192:
                raise AgentError("validation_error", "请求路径无效")
            path = unquote(parsed.path)
            values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30)
            if any(len(v) != 1 for v in values.values()):
                raise AgentError("validation_error", "不允许重复参数")
            query = {k: v[0] for k, v in values.items()}
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
                      "/chat.js": ("chat.js", "text/javascript; charset=utf-8"), "/quality.js": ("quality.js", "text/javascript; charset=utf-8"), "/calendar.js": ("calendar.js", "text/javascript; charset=utf-8"), "/tasks.js": ("tasks.js", "text/javascript; charset=utf-8"), "/knowledge.js": ("knowledge.js", "text/javascript; charset=utf-8"), "/integrations.js": ("integrations.js", "text/javascript; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                      "/brand.css": ("brand.css", "text/css; charset=utf-8"),
                      "/vendor/Geist-variable.woff2": ("vendor/Geist-variable.woff2", "font/woff2"),
                      "/vendor/bulma-1.0.4.min.css": ("vendor/bulma-1.0.4.min.css", "text/css; charset=utf-8")}
            if self.command == "GET" and path in assets:
                name, kind = assets[path]
                self.send_data(200, (PROJECT_ROOT / "assets" / "web" / name).read_bytes(), kind)
                return
            result = app.route(self.command, path, query, body)
            if isinstance(result, tuple):
                self.send_data(200, *result)
            else:
                self.send_data(200, result)
        except AgentError as exc:
            status = 404 if exc.code == "not_found" else 409 if exc.code in ("conflict", "version_conflict", "request_id_conflict", "action_unavailable", "stale_version", "case_needs_review") else 503 if exc.code in ("backend_unavailable", "model_unavailable", "unknown_result", "write_outcome_unknown", "unknown_outcome") else 400
            self.send_data(status, {"error": exc.as_dict()})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except (ValueError, TypeError, OverflowError):
            self.send_data(400, {"error": {"code": "validation_error", "message": "请求参数格式不正确"}})
        except (OSError, sqlite3.Error):
            self.send_data(503, {"error": {"code": "storage_unavailable", "message": "本地数据暂时不可用，请检查目录权限和可用空间"}})
        except Exception:
            self.send_data(500, {"error": {"code": "internal_error", "message": "处理未能完成，请重试或查看本地诊断"}})

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_route


class LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # This service only binds a literal loopback address. A reverse DNS lookup
        # adds no identity/security value and can stall cold macOS startup.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


def make_server(data_dir, host="127.0.0.1", port=0, engine=None, model_client=None, calendar=None):
    if host not in ("127.0.0.1", "localhost"):
        raise AgentError("validation_error", "工作台仅能绑定本机 127.0.0.1")
    server = LoopbackHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    try:
        server.application = Application(data_dir, engine, model_client, calendar)
    except BaseException:
        server.server_close()
        raise
    return server


def run_web(data_dir, port=8787, open_browser=False, calendar=None):
    server = make_server(data_dir, port=port, calendar=calendar)
    server.service_stop_token = os.environ.pop("PERSONAL_AGENT_STOP_TOKEN", "")
    try:
        server.application.automations.start()
    except ImportError:
        print("调度依赖不可用，请用项目 .venv 启动；定时任务未运行。", flush=True)
    if os.environ.get("PERSONAL_AGENT_STARTUP_DIAGNOSTICS") == "1":
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
    url = "http://127.0.0.1:%s" % server.server_address[1]
    print("Daynest 工作台：" + url, flush=True)
    print("数据目录：" + str(Path(data_dir).resolve()), flush=True)
    print("本地工作台；外部同步和通知按已保存配置启用。按 Ctrl+C 停止。", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n工作台已停止。", flush=True)
    finally:
        server.server_close()
        server.application.close()
    return 0
