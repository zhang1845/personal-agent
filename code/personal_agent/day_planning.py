"""Select existing tasks in chat and review one calendar draft per available slot."""
from datetime import datetime, time, timedelta
import json
import re

from .errors import AgentError
from .timeutils import localize

TOOL = "assistant_schedule_task"
MAX_CHOICES = 50
_START = re.compile(
    r"^(?:请)?(?:帮我|给我|为我)?(?:规划|计划|安排一下)(?:一下)?今天(?:上午|下午|的待办|的任务|的计划|的安排)?(?:[。！!？?]?$|[：:，,])"
    r"|(?:待办|任务|件事).{0,120}(?:排进|排到|安排在|安排到|排在)今天"
    r"|(?:排进|排到|规划|安排)今天(?:上午|下午)?(?:的|要做的|这些|这几项)*(?:待办|任务)"
)
_CANCEL = {"取消规划", "退出规划", "不规划了", "取消排程"}


def reply(message, actions=None, clarify=False):
    return {"reply": message, "actions": actions or [], "tools": [], "needs_clarification": clarify}


def store_choices(store, session, state):
    with store.connection() as conn:
        if state is None:
            conn.execute("DELETE FROM day_plan_choices WHERE session_id=?", (session,))
        else:
            conn.execute("INSERT OR REPLACE INTO day_plan_choices VALUES(?,?)", (session, json.dumps(state, ensure_ascii=False)))


def eligible(task):
    schedule = task.get("schedule") or {}
    return not task["completed"] and not task.get("deleted_at") and schedule.get("status") not in ("pending", "unknown", "applied")


def choose_tasks(engine, session, message):
    if engine.calendar.backend is not engine.calendar.storage:
        return reply("待办排程目前仅支持 Daynest 本地日历。")
    current, tz = engine._now()
    tasks = sorted((task for task in engine.store.list_tasks() if eligible(task)),
                   key=lambda task: (task.get("due_date") or "9999", task["created_at"], task["id"]))
    mentioned = [task for task in tasks if task["title"] in message]
    pool = mentioned or tasks
    options = [{key: task[key] for key in ("id", "title", "version")} for task in pool[:MAX_CHOICES]]
    if not options:
        store_choices(engine.store, session, None)
        return reply("暂无可以排程的待办。可以先添加待办，再说“帮我规划今天”。")
    period = "下午" if "下午" in message else "上午" if "上午" in message else ""
    state = {"date": current.date().isoformat(), "timezone": str(tz), "period": period, "tasks": options, "selected": {}}
    # A request may already name tasks and estimates: “帮我规划今天：写邮件30分钟，评审半小时”.
    # Consume the entire list; a partially matched item must never disappear from the plan.
    parts = re.split(r"规划今天(?:上午|下午)?(?:的待办|的任务)?[：:，,]", message, maxsplit=1)
    if mentioned and len(parts) == 2 and parts[1].strip():
        try:
            selected = parse_selection(parts[1].strip(), state)
        except (AgentError, ValueError, OverflowError):
            selected = None
        if selected:
            store_choices(engine.store, session, state)
            return finish_selection(engine, session, state, selected, current, tz)
    store_choices(engine.store, session, state)
    lines = [f"今天{period}想安排哪些待办？请回复编号和各自所需时长。"]
    lines += [f"{i}. {task['title']}" for i, task in enumerate(options, 1)]
    if len(pool) > MAX_CHOICES:
        lines.append(f"已列出前 {MAX_CHOICES} 项。也可以在规划请求中写明待办名称。")
    lines.append("例如“1 30分钟，2 45分钟”。")
    return reply("\n".join(lines), clarify=True)


def task_index(label, tasks):
    match = re.fullmatch(r"(?:第|编号)?(\d+)(?:项|号)?", label)
    if match:
        index = int(match.group(1))
        if 1 <= index <= len(tasks):
            return str(index)
    same_title = [str(i) for i, task in enumerate(tasks, 1) if task["title"] == label]
    if len(same_title) == 1:
        return same_title[0]
    if same_title:
        raise AgentError("validation_error", "有同名待办，请用列表中的编号选择。")
    return None


def parse_selection(message, state):
    """Return exact selections, or None so an unrelated conversation can continue."""
    from .assistant import _DURATION, _duration_minutes
    selected = dict(state["selected"])
    raw = re.sub(r"^(?:选(?:择)?|安排|排程)\s*", "", message).strip(" 。")
    shared = re.fullmatch(r"(?:都|各|每项|每个)(?:需要|用)?\s*(.+)", raw)
    duration_only = _DURATION.fullmatch(shared[1] if shared else raw)
    if duration_only:
        targets = [key for key, value in selected.items() if value is None]
        if not targets and len(state["tasks"]) == 1:
            targets = ["1"]
        if targets and (shared or len(targets) == 1):
            return {**selected, **{key: _duration_minutes(duration_only) for key in targets}}
        if shared or selected:
            raise AgentError("validation_error", "请说明各项时长对应的待办编号。")
        return None
    picked = {}
    for part in re.split(r"[,，、;；\n]+", raw):
        part = part.strip()
        match = _DURATION.search(part)
        if match and match.end() != len(part):
            return None
        label = (part[:match.start()] if match else part).strip(" ：:，")
        key = task_index(label, state["tasks"])
        if key is None:
            if re.fullmatch(r"(?:第|编号)?\d{1,3}(?:项|号)?",label) or picked:
                raise AgentError("validation_error", "请按待办列表中的编号和时长填写，例如“1 30分钟，2 45分钟”。")
            return None
        if key in picked:
            raise AgentError("validation_error","一个待办出现了两次，请为每项给出一个时长。")
        picked[key] = _duration_minutes(match) if match else None
    return {**selected, **picked}


def suggest(engine, session, state, selected, current, tz):
    day = current.date()
    start_hour = 12 if state["period"] == "下午" else 0
    end_day = day if state["period"] == "上午" else day + timedelta(days=1)
    end_hour = 12 if state["period"] == "上午" else 0
    lower = localize(datetime.combine(day, time(start_hour)), tz)
    # Leave enough time for the user to review the first draft and align to a five-minute boundary.
    start = max(lower, current.replace(second=0, microsecond=0) + timedelta(minutes=5 - current.minute % 5))
    end = localize(datetime.combine(end_day, time(end_hour)), tz)
    with engine.calendar.lock:
        prefs = engine.calendar.call("calendar_get_preferences", {})["preferences"]
        gaps = list(engine.calendar.working_gaps(start.isoformat(), end.isoformat(), prefs)) if start < end else []
    buffer = timedelta(minutes=prefs["buffer_minutes"])
    drafts, skipped = [], []
    for key, minutes in selected.items():
        task = state["tasks"][int(key) - 1]
        live = engine.store.get_task(task["id"])
        if live["version"] != task["version"] or not eligible(live):
            skipped.append(f"{task['title']}（任务已变更或已有安排）")
            continue
        duration = timedelta(minutes=minutes)
        for left, right in gaps:
            if left + duration <= right:
                args = dict(task_id=task["id"], title=task["title"], expected_version=task["version"],
                            start=left.astimezone(tz).isoformat(), end=(left + duration).astimezone(tz).isoformat(),
                            timezone=str(tz))
                drafts.append(args)
                blocked_start, blocked_end = left - buffer, left + duration + buffer
                gaps = [(a, b) for x, y in gaps
                        for a, b in ((x, min(y, blocked_start)), (max(x, blocked_end), y)) if a < b]
                break
        else:
            skipped.append(f"{task['title']}（工作时段内没有连续 {minutes} 分钟）")
    with engine.store.connection() as conn:
        actions = [engine.store.create_action(session, TOOL, args, title="为待办安排时间", connection=conn) for args in drafts]
        conn.execute("DELETE FROM day_plan_choices WHERE session_id=?", (session,))
    text = f"已找到 {len(actions)} 个候选时段，请逐项确认想加入日历的安排。" if actions else "没有可确认的候选时段。"
    if skipped:
        text += "\n未排入日历：" + "、".join(skipped) + "。"
    return reply(text, actions)


def handle(engine, message, session):
    from .chat_rules import denies_writes
    starting = _START.search(message)
    if starting and not denies_writes(message) and not re.search(r"不想|不要|不必|别规划|明天|后天", message):
        return choose_tasks(engine, session, message)
    # Most chat requests do not involve a pending plan.
    with engine.store.connection() as conn:
        row = conn.execute("SELECT state FROM day_plan_choices WHERE session_id=?", (session,)).fetchone()
    if not row:
        return None
    if message.strip(" 。") in _CANCEL:
        store_choices(engine.store, session, None)
        return reply("已结束这次规划。")
    state = json.loads(row["state"])
    try:
        selected = parse_selection(message, state)
    except (ValueError, OverflowError):
        return reply("请填写有效的整数分钟或小时数，例如“1 30分钟”。", clarify=True)
    if not selected:
        store_choices(engine.store, session, None)
        return None
    current, tz = engine._now()
    if current.date().isoformat() != state["date"] or str(tz) != state["timezone"]:
        store_choices(engine.store, session, None)
        return reply("日期或时区已变化，请重新发起今天的规划。", clarify=True)
    return finish_selection(engine, session, state, selected, current, tz)


def finish_selection(engine, session, state, selected, current, tz):
    if any(minutes is not None and not 5 <= minutes <= 1440 for minutes in selected.values()):
        return reply("每项待办的时长需为 5 到 1440 分钟，请重新填写。", clarify=True)
    missing = [key for key, value in selected.items() if value is None]
    if missing:
        state["selected"] = selected
        store_choices(engine.store, session, state)
        names = [f"{key}. {state['tasks'][int(key) - 1]['title']}" for key in missing]
        return reply("请补充这些待办的时长：" + "、".join(names) + "。例如“" + missing[0] + " 30分钟”。", clarify=True)
    return suggest(engine, session, state, selected, current, tz)


def confirm(planner, action_id, args, current):
    """A replay reconciles the original request even if its time slot has since passed."""
    payload = {key: args[key] for key in ("expected_version", "start", "end")}
    payload["request_id"] = action_id
    with planner.lock, planner.calendar.lock:
        if planner.store.get_plan(request_id=action_id) is None:
            start, end = datetime.fromisoformat(args["start"]), datetime.fromisoformat(args["end"])
            prefs = planner.calendar.call("calendar_get_preferences", {})["preferences"]
            if current >= start or prefs["timezone"] != args["timezone"]:
                raise AgentError("action_unavailable", "时段已开始或时区已变化，请重新规划。")
            minutes = int((end - start).total_seconds() // 60)
            free = planner.calendar.call("calendar_find_free_slots", {"start":args["start"], "end":args["end"],
                                         "duration_minutes":minutes, "limit":1})
            if not free["slots"]:
                raise AgentError("conflict", "这个时段已不可用，请重新规划。")
        return planner.schedule(args["task_id"], payload)
