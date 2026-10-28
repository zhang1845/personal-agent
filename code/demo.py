"""Produce an isolated, repeatable local demonstration without any cloud account."""

import json
from pathlib import Path
import tempfile

from personal_agent.errors import AgentError
from personal_agent.ics import write_snapshot
from personal_agent.service import CalendarService


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "output"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="calendar-demo-", dir=str(output)) as tmp:
        service = CalendarService(Path(tmp) / "demo.sqlite3")
        create = {"request_id": "demo-create-once", "title": "演示 个人 Agent 方案评审",
                  "start": "2026-10-27T15:00:00+08:00", "end": "2026-10-27T16:00:00+08:00",
                  "location": "本地开发样例"}
        steps = [{"step": "status", "result": service.call("calendar_status", {})}]
        event = service.call("calendar_create_event", create)["event"]
        steps.append({"step": "create", "result": event})
        steps.append({"step": "retry_same_request", "result": service.call("calendar_create_event", create)})
        try:
            service.call("calendar_create_event", dict(create, request_id="demo-conflict", title="这条应被拦截"))
        except AgentError as error:
            steps.append({"step": "conflict_rejected", "result": error.as_dict()})
        event = service.call("calendar_update_event", {
            "request_id": "demo-move-once", "event_id": event["id"], "expected_version": event["version"],
            "changes": {"start": "2026-10-27T16:00:00+08:00", "end": "2026-10-27T17:00:00+08:00"},
        })["event"]
        steps.append({"step": "update", "result": event})
        steps.append({"step": "free_slots", "result": service.call("calendar_find_free_slots", {
            "start": "2026-10-27", "end": "2026-10-28", "duration_minutes": 60,
        })})
        export_path = output / "demo-calendar.ics"
        # Avoid overwriting an existing review artifact when the demo is rerun.
        if not export_path.exists():
            steps.append({"step": "snapshot", "result": write_snapshot(export_path, [event])})
        else:
            steps.append({"step": "snapshot", "result": {"notice": "已有 demo-calendar.ics 保留原文件；本轮未覆盖"}})
        steps.append({"step": "delete", "result": service.call("calendar_delete_event", {
            "request_id": "demo-delete-once", "event_id": event["id"], "expected_version": event["version"],
        })})
        service.close()
    result = {"mode": "isolated_local_demo", "real_calendar_accessed": False,
              "phone_notification_sent": False, "steps": steps}
    path = output / "demo-result.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"demo_result": str(path), "snapshot": str(export_path),
                      "real_calendar_accessed": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
