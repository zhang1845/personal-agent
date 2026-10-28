"""Private, local persistence for the personal workbench (no external services)."""

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import hashlib
from pathlib import Path
import re
import sqlite3
import uuid

from .errors import AgentError


def checked_text(value, field, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AgentError("validation_error", field + " 不能为空或超过长度限制")
    return value.strip()


def checked_model(value):
    if value == "":
        return ""
    value = checked_text(value, "model", 150)
    if not re.fullmatch(r"[A-Za-z0-9_./:-]+", value) or any(x in value.lower() for x in ("cloud", "remote", "://")):
        raise AgentError("validation_error", "仅支持本机已安装的 Ollama 模型，不支持云模型或自定义服务地址")
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


class PersonalStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,role TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,tool TEXT NOT NULL,arguments TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL,result TEXT,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,title TEXT NOT NULL,due_date TEXT,completed INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,source_action_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,content TEXT NOT NULL,created_at TEXT NOT NULL,source_action_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS message_session ON messages(session_id,id);
            """)
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            for name, definition in (("version", "INTEGER NOT NULL DEFAULT 1"), ("deleted_at", "TEXT")):
                if name not in columns:
                    conn.execute("ALTER TABLE tasks ADD COLUMN " + name + " " + definition)
            conn.execute("""CREATE TABLE IF NOT EXISTS task_plans(
                request_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                arguments TEXT NOT NULL,status TEXT NOT NULL,event TEXT,error TEXT,created_at TEXT NOT NULL)""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS task_one_active_plan ON task_plans(task_id)
                WHERE status IN ('pending','unknown','applied')""")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self):
        pass

    def new_session(self, title):
        row = {"id": uuid.uuid4().hex, "title": checked_text(title, "title", 200), "created_at": now(), "updated_at": now()}
        with self.connection() as c:
            c.execute("INSERT INTO sessions VALUES(:id,:title,:created_at,:updated_at)", row)
        return row

    def get_session(self, session_id):
        with self.connection() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise AgentError("not_found", "找不到该对话")
        return dict(row)

    def list_sessions(self):
        with self.connection() as c:
            return [dict(r) for r in c.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 100")]

    def add_message(self, session_id, role, content):
        self.get_session(session_id)
        if role not in ("user", "assistant", "tool"):
            raise AgentError("validation_error", "不支持的消息角色")
        content = checked_text(content, "content", 20000)
        with self.connection() as c:
            cursor = c.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)", (session_id, role, content, now()))
            c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), session_id))
            return cursor.lastrowid

    def list_messages(self, session_id, limit=20):
        self.get_session(session_id)
        limit = min(max(int(limit), 1), 500)
        with self.connection() as c:
            rows = c.execute("SELECT id,role,content,created_at FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    @staticmethod
    def action_dict(row):
        value = dict(row)
        value["arguments"] = json.loads(value["arguments"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return value

    def create_action(self, session_id, tool, arguments, title=None):
        self.get_session(session_id)
        tool = checked_text(tool, "tool", 100)
        if not isinstance(arguments, dict):
            raise AgentError("validation_error", "操作参数必须是对象")
        action_id = uuid.uuid4().hex
        arguments = dict(arguments)
        if tool in ("calendar_create_event", "calendar_update_event", "calendar_delete_event"):
            arguments["request_id"] = action_id
        row = {"id": action_id, "session_id": session_id, "tool": tool,
               "arguments": json.dumps(arguments, ensure_ascii=False), "title": checked_text(title or tool, "title", 300),
               "status": "pending", "result": None, "created_at": now()}
        if len(row["arguments"]) > 20000:
            raise AgentError("validation_error", "操作参数过长")
        with self.connection() as c:
            c.execute("INSERT INTO actions VALUES(:id,:session_id,:tool,:arguments,:title,:status,:result,:created_at)", row)
        return self.action_dict(row)

    def get_action(self, action_id):
        with self.connection() as c:
            row = c.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            raise AgentError("not_found", "找不到该操作草案")
        return self.action_dict(row)

    def list_actions(self, session_id):
        with self.connection() as c:
            return [self.action_dict(r) for r in c.execute("SELECT * FROM actions WHERE session_id=? ORDER BY created_at", (session_id,))]

    def finish_action(self, action_id, status, result):
        with self.connection() as c:
            c.execute("UPDATE actions SET status=?,result=? WHERE id=?", (status, json.dumps(result, ensure_ascii=False), action_id))
        return self.get_action(action_id)

    @staticmethod
    def task_dict(row):
        result = dict(row)
        result.pop("source_action_id", None)
        result["completed"] = bool(result["completed"])
        return result

    @staticmethod
    def checked_due(value):
        if value is not None:
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise AgentError("validation_error", "截止日期必须为 YYYY-MM-DD")
        return value

    @staticmethod
    def check_version(row, expected_version):
        if expected_version is not None:
            if type(expected_version) is not int or expected_version < 1:
                raise AgentError("validation_error", "任务版本必须是正整数")
            if row["version"] != expected_version:
                raise AgentError("version_conflict", "任务已变化，请刷新后重试")

    @staticmethod
    def public_plan(row):
        if not row:
            return None
        return {"request_id": row["request_id"], "status": row["status"],
                "event": json.loads(row["event"]) if row["event"] else None,
                "error": json.loads(row["error"]) if row["error"] else None}

    def list_tasks(self, deleted=False):
        with self.connection() as c:
            rows = c.execute("SELECT * FROM tasks WHERE deleted_at IS " + ("NOT NULL" if deleted else "NULL") + " ORDER BY completed,created_at DESC,id").fetchall()
            plans = {}
            for row in c.execute("SELECT * FROM task_plans ORDER BY created_at,rowid"):
                plans[row["task_id"]] = self.public_plan(row)
            return [dict(self.task_dict(row), schedule=plans.get(row["id"])) for row in rows]

    def get_task(self, task_id):
        with self.connection() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise AgentError("not_found", "找不到该任务")
            plan = c.execute("SELECT * FROM task_plans WHERE task_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (task_id,)).fetchone()
            return dict(self.task_dict(row), schedule=self.public_plan(plan))

    def add_task(self, title, due_date=None, source_action_id=None):
        title = checked_text(title, "title", 300)
        due_date = self.checked_due(due_date)
        with self.connection() as c:
            existing = c.execute("SELECT * FROM tasks WHERE source_action_id=?", (source_action_id,)).fetchone() if source_action_id else None
            if existing:
                return self.task_dict(existing)
            task_id = uuid.uuid4().hex
            c.execute("INSERT INTO tasks(id,title,due_date,completed,created_at,source_action_id) VALUES(?,?,?,0,?,?)", (task_id, title, due_date, now(), source_action_id))
            return self.task_dict(c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    @staticmethod
    def check_plan_idle(c, task_id):
        if c.execute("SELECT 1 FROM task_plans WHERE task_id=? AND status IN ('pending','unknown')", (task_id,)).fetchone():
            raise AgentError("action_unavailable", "任务的日程安排结果待核查，请先核查原安排")

    def update_task(self, task_id, changes, expected_version=None):
        if not isinstance(changes, dict) or not changes or set(changes) - {"title", "due_date", "completed"}:
            raise AgentError("validation_error", "请提供任务标题、截止日期或完成状态")
        changes = dict(changes)
        if "title" in changes:
            changes["title"] = checked_text(changes["title"], "title", 300)
        if "due_date" in changes:
            changes["due_date"] = self.checked_due(changes["due_date"])
        if "completed" in changes and type(changes["completed"]) is not bool:
            raise AgentError("validation_error", "completed 必须是布尔值")
        with self.connection() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=? AND deleted_at IS NULL", (task_id,)).fetchone()
            if not row:
                raise AgentError("not_found", "任务不存在或已移入回收站")
            self.check_version(row, expected_version)
            self.check_plan_idle(c, task_id)
            if any(row[key] != value for key, value in changes.items()):
                assignments = ",".join(key + "=?" for key in changes)
                c.execute("UPDATE tasks SET " + assignments + ",version=version+1 WHERE id=?", (*changes.values(), task_id))
            return self.task_dict(c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def set_task_completed(self, task_id, completed):
        return self.update_task(task_id, {"completed": completed})

    def set_task_deleted(self, task_id, deleted, expected_version=None):
        with self.connection() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise AgentError("not_found", "找不到该任务")
            if bool(row["deleted_at"]) == deleted:
                return self.task_dict(row)
            self.check_version(row, expected_version)
            self.check_plan_idle(c, task_id)
            c.execute("UPDATE tasks SET deleted_at=?,version=version+1 WHERE id=?", (now() if deleted else None, task_id))
            return self.task_dict(c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def get_plan(self, task_id=None, request_id=None):
        with self.connection() as c:
            if request_id is not None:
                row = c.execute("SELECT * FROM task_plans WHERE request_id=?", (request_id,)).fetchone()
            else:
                row = c.execute("SELECT * FROM task_plans WHERE task_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (task_id,)).fetchone()
            return dict(row) if row else None

    def reserve_plan(self, task_id, request_id, payload, timing=None):
        request_id = checked_text(request_id, "request_id", 100)
        fingerprint = hashlib.sha256(json.dumps({"task_id": task_id, "payload": payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.connection() as c:
            old = c.execute("SELECT * FROM task_plans WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise AgentError("request_id_conflict", "同一安排请求不能用于不同任务或参数")
                return dict(old)
            task = c.execute("SELECT * FROM tasks WHERE id=? AND deleted_at IS NULL", (task_id,)).fetchone()
            if not task:
                raise AgentError("not_found", "任务不存在或已移入回收站")
            self.check_version(task, payload["expected_version"])
            if task["completed"]:
                raise AgentError("action_unavailable", "已完成任务不能安排，请先重新打开")
            if len(task["title"]) > 200:
                raise AgentError("validation_error", "任务标题超过200字，请缩短后安排")
            if c.execute("SELECT 1 FROM task_plans WHERE task_id=? AND status IN ('pending','unknown','applied')", (task_id,)).fetchone():
                raise AgentError("action_unavailable", "此任务已有安排或结果待核查，请先查看原日程")
            args = {key: value for key, value in payload.items() if key != "expected_version"}
            args.update(timing or {})
            args.update(title=task["title"], request_id="task-plan:" + request_id, all_day=False)
            c.execute("INSERT INTO task_plans VALUES(?,?,?,?, 'pending',NULL,NULL,?)", (request_id, task_id, fingerprint, json.dumps(args, ensure_ascii=False), now()))
            return dict(c.execute("SELECT * FROM task_plans WHERE request_id=?", (request_id,)).fetchone())

    def cancel_event_plans(self, event_id):
        with self.connection() as c:
            for plan in c.execute("SELECT * FROM task_plans WHERE status='applied'").fetchall():
                if json.loads(plan["event"])["id"] == event_id:
                    c.execute("UPDATE task_plans SET status='canceled' WHERE request_id=?", (plan["request_id"],))

    def finish_plan(self, request_id, status, event=None, error=None):
        with self.connection() as c:
            old = c.execute("SELECT * FROM task_plans WHERE request_id=?", (request_id,)).fetchone()
            if not old:
                raise AgentError("not_found", "找不到原安排")
            if old["status"] == "applied" and status in ("failed", "unknown"):
                return dict(old)
            c.execute("UPDATE task_plans SET status=?,event=?,error=? WHERE request_id=?", (status, json.dumps(event, ensure_ascii=False) if event else old["event"], json.dumps(error, ensure_ascii=False) if error else None, request_id))
            if status == "applied" and old["status"] != "applied":
                c.execute("UPDATE tasks SET version=version+1 WHERE id=?", (old["task_id"],))
            return dict(c.execute("SELECT * FROM task_plans WHERE request_id=?", (request_id,)).fetchone())

    def list_memories(self):
        with self.connection() as c:
            return [{k: r[k] for k in ("id", "content", "created_at")} for r in c.execute("SELECT * FROM memories ORDER BY created_at DESC LIMIT 1000")]

    def save_memory(self, content, source_action_id=None):
        content = checked_text(content, "content", 2000)
        with self.connection() as c:
            existing = c.execute("SELECT * FROM memories WHERE source_action_id=?", (source_action_id,)).fetchone() if source_action_id else None
            if existing:
                return {k: existing[k] for k in ("id", "content", "created_at")}
            row = {"id": uuid.uuid4().hex, "content": content, "created_at": now()}
            c.execute("INSERT INTO memories VALUES(:id,:content,:created_at,:source_action_id)", dict(row, source_action_id=source_action_id))
            return row

    def delete_memory(self, memory_id):
        with self.connection() as c:
            if c.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount != 1:
                raise AgentError("not_found", "找不到该记忆")
        return {"deleted": True, "id": memory_id}

    def get_settings(self):
        with self.connection() as c:
            row = c.execute("SELECT value FROM settings WHERE key='model'").fetchone()
        return {"model": row["value"] if row else ""}

    def set_settings(self, model):
        model = checked_model(model)
        with self.connection() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES('model',?)", (model,))
        return self.get_settings()
