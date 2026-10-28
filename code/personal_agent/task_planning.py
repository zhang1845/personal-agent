"""Task-to-calendar orchestration. Local writes reuse the calendar's durable request IDs."""
import json
import threading

from .errors import AgentError
from .timeutils import interval, zone


class TaskPlanner:
    def __init__(self, calendar, store):
        self.calendar, self.store = calendar, store
        self.lock = threading.RLock()

    def require_local(self):
        if self.calendar.backend is not self.calendar.storage:
            raise AgentError("unsupported_capability", "任务关联排程当前仅支持本地日历；外部日历需单独联调")

    def reconcile(self, task_id):
        self.require_local()
        with self.lock:
            self.store.get_task(task_id)
            plan = self.store.get_plan(task_id=task_id)
            if plan and plan["status"] in ("pending", "unknown"):
                args = json.loads(plan["arguments"])
                with self.calendar.storage.connection() as conn:
                    request = conn.execute("SELECT * FROM requests WHERE request_id=?", (args["request_id"],)).fetchone()
                if request and request["status"] == "done":
                    plan = self.store.finish_plan(plan["request_id"], "applied", json.loads(request["result"])["event"])
            if plan and plan["status"] == "applied":
                try:
                    event = self.calendar.call("calendar_get_event", {"event_id": json.loads(plan["event"])["id"]})["event"]
                    plan = self.store.finish_plan(plan["request_id"], "applied", event)
                except AgentError as exc:
                    if exc.code != "not_found":
                        raise
                    plan = self.store.finish_plan(plan["request_id"], "canceled")
            return {"task": self.store.get_task(task_id), "schedule": self.store.public_plan(plan)}

    def execute(self, plan):
        if plan["status"] == "applied":
            return {"task": self.store.get_task(plan["task_id"]), "schedule": self.store.public_plan(plan), "event": json.loads(plan["event"])}
        if plan["status"] not in ("pending", "unknown"):
            raise AgentError("action_unavailable", "原安排已失败或取消，请重新选择时段并确认")
        try:
            result = self.calendar.call("calendar_create_event", json.loads(plan["arguments"]))
        except AgentError as exc:
            status = "unknown" if exc.code in ("unknown_outcome", "unknown_result", "write_outcome_unknown", "storage_unavailable") else "failed"
            self.store.finish_plan(plan["request_id"], status, error=exc.as_dict())
            raise
        saved = self.store.finish_plan(plan["request_id"], "applied", result["event"])
        return {"task": self.store.get_task(plan["task_id"]), "schedule": self.store.public_plan(saved), "event": result["event"]}

    def schedule(self, task_id, payload):
        self.require_local()
        allowed = {"request_id", "expected_version", "start", "end", "location", "description"}
        if set(payload) - allowed or not {"request_id", "expected_version", "start", "end"} <= set(payload):
            raise AgentError("validation_error", "安排需要任务版本和明确起止时间")
        if type(payload["expected_version"]) is not int or payload["expected_version"] < 1:
            raise AgentError("validation_error", "任务版本必须是正整数")
        if any(not isinstance(payload[key], str) or "T" not in payload[key] for key in ("start", "end")):
            raise AgentError("validation_error", "任务排程需要明确的日期和时刻")
        preferences = self.calendar.call("calendar_get_preferences", {})["preferences"]
        start, end = interval(payload["start"], payload["end"], zone(preferences["timezone"]))
        request_id = payload["request_id"]
        args = dict(payload)
        del args["request_id"]
        with self.lock:
            # Reconcile a canceled linked event before allowing a fresh request.
            self.reconcile(task_id)
            plan = self.store.reserve_plan(task_id, request_id, args, {"start": start.isoformat(), "end": end.isoformat()})
            return self.execute(plan)

    def retry(self, task_id):
        self.require_local()
        with self.lock:
            current = self.reconcile(task_id)
            plan = self.store.get_plan(task_id=task_id)
            if not plan:
                raise AgentError("not_found", "没有待核查的原安排")
            # Explicit continuation of exactly the saved request, never a fresh request.
            return self.execute(plan) if plan["status"] in ("pending", "unknown", "applied") else current
