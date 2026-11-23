"""Select Daynest tools and conversation evidence for a single model turn."""

import re


# These are routing hints, not permission checks. Validation and confirmation
# remain in the business tools, even when several groups are selected together.
_DOMAINS = {
    "calendar": r"日程|日历|安排|空闲|有空|有没(?:有)?(?:空|时间)|空档|有空档|找时间|时段|改期|改到|会议|开会|评审会|约会|工作时间|工作时段|时区|冲突|行程|calendar|schedule|meeting|free slot",
    "tasks": r"待办|待做|任务|推进计划|事项|to-?do|\btask\b",
    "knowledge": r"资料|材料|笔记|知识库|原文|出处|引用|项目(?:背景|进展|决策|情况)|这个项目|文献|knowledge|notes?\b",
    "imports": r"https?://|附件|链接|网页|文件|导入|归档|上传|pdf|docx|attachment|import",
    "reminders": r"提醒|闹钟|通知|remind|notification",
    "sync": r"同步|caldav|sync",
    "memory": r"记住|记忆|记得我|我的偏好|我的习惯|我(?:之前|平时)喜欢|工作方式|memory|my preferences",
}

_TOOLS = {
    "tasks": {"assistant_list_tasks", "assistant_add_task", "assistant_complete_task"},
    "imports": {"assistant_preview_link", "assistant_list_attachments", "assistant_read_attachment", "assistant_save_import"},
    "reminders": {"assistant_list_reminders", "assistant_create_reminder", "assistant_update_reminder", "assistant_delete_reminder"},
    "sync": {"assistant_sync_calendar", "assistant_calendar_sync_status"},
    "memory": {"assistant_list_memories", "assistant_save_memory"},
}

_REFERENCE = re.compile(r"刚才|上述|上(?:一条|一轮|面)|前面|继续|上次提到|那(?:份|场|条|件|个|篇|天)|这(?:份|场|条|几项)|^(?:请)?(?:改成|改到|提前到|推迟到)|第二个|最后一个|^再(?:说|查|把|帮|改|给|解释)|^换(?:个|成)|^然后|^其(?:中|余)")
_QUESTION = re.compile(r"[?？]|请(?:补充|告诉|说明|提供)|还需|缺少|多久|多长|几点|哪个|哪天")
_DURATION_OR_DATE = re.compile(r"^(?:定在|就|只要|从|改为|预计|大概|时长|名称[叫为是]?|标题[叫为是]?)?\s*(?:\d|[零〇一二两三四五六七八九十半]|明天|今天|后天|上午|下午|中午|晚上|周[一二三四五六日天]|下周|本周)")
_NEW_REQUEST = re.compile(r"请|帮我|查询|是什么|有没有|是否|解释|总结|搜索|创建|保存|列出|查看|查一下|分析")


_LIBRARY_LOOKUP = re.compile(
    r"(?:根据|结合|参考|基于|按照|查|搜索|检索|读|分析|整理出|总结|提取|梳理|使用|用|从|依据).{0,16}(?:资料|材料|笔记|文献|知识库|原文|项目背景)"
    r"|(?:资料|材料|笔记|知识库|项目背景).{0,8}(?:中|里|提到|指出|显示|说|要求|说明|怎么|如何)"
    r"|出处|引用|according to|based on|from (?:my |the )?notes", re.I
)


def _domains(message):
    domains = {key for key, pattern in _DOMAINS.items() if re.search(pattern, message, re.IGNORECASE)}
    # “安排资料评审会” names a meeting topic. “根据资料安排评审会”
    # explicitly asks the assistant to consult the library.
    if domains.intersection({"calendar", "tasks", "reminders"}) and not _LIBRARY_LOOKUP.search(message):
        domains.discard("knowledge")
    return domains


def _depends_on_previous(message, preceding_reply):
    if _REFERENCE.search(message):
        return True
    # Fragmentary replies such as “一个半小时” need the preceding question.
    if not _QUESTION.search(preceding_reply) or len(message) > 60:
        return False
    if _NEW_REQUEST.search(message) or re.search(r"[?？]", message):
        return False
    return bool(_DURATION_OR_DATE.match(message) or not _domains(message))


def _related_history(message, history):
    """Walk back over the contiguous turns that the new request refers to."""
    selected = []
    current = message
    for end in range(len(history), 1, -2):
        user, reply = history[end - 2:end]
        if user["role"] != "user" or reply["role"] != "assistant":
            break
        if not _depends_on_previous(current, reply["content"]):
            break
        # On a calendar-only turn, a reference to a meeting can carry forward
        # its fields. A previous document answer does not supply those fields.
        if _domains(current) == {"calendar"}:
            prior_domains = _domains(user["content"])
            if prior_domains and "calendar" not in prior_domains and not _LIBRARY_LOOKUP.search(current):
                break
        selected[0:0] = [{"role": item["role"], "content": item["content"]} for item in (user, reply)]
        current = user["content"]
    return selected


def select_context(message, history, available_tools, attachment_ids=()):
    history = _related_history(message, history)
    domains = _domains(message)
    for item in history:
        if item["role"] == "user":
            domains.update(_domains(item["content"]))
    if attachment_ids:
        domains.add("imports")
    # An unfamiliar expression can still use the bounded workbench tools.
    # Do not prefetch a user's documents or memories just because intent is unclear.
    names = set(available_tools) if not domains else set()
    for domain in domains:
        names.update(_TOOLS.get(domain, ()))
    if "calendar" in domains:
        calendars = {name for name in available_tools if name.startswith("calendar_")}
        calendar_intent = " ".join([item["content"] for item in history if item["role"] == "user"] + [message])
        # Exclude mutation schemas only for unambiguous queries. Edits, proposals,
        # or follow-ups to a prior write retain the normal confirmation tools.
        query = re.search(r"查询|查看|查(?:看|找|一下|明天|今天|周)|读取|列出|有没有|有空吗|哪些|find|list|check", calendar_intent, re.I)
        change = re.search(r"创建|建一个|新增|添加|预约|安排|改|调|新建|订|定在|帮我排|排进|取消|移|删除|推迟|提前|设|草案|生成|保存|然后|并且|同时|create|schedule|book|cancel|reschedule", calendar_intent, re.I)
        if query and not change:
            calendars.difference_update({"calendar_create_event", "calendar_update_event", "calendar_delete_event", "calendar_set_preferences"})
        names.update(calendars)
    if "knowledge" in domains:
        names.update(name for name in available_tools if name.startswith("knowledge_"))
    if "reminders" in domains:
        names.update(("calendar_get_preferences", "calendar_list_events"))
    if "sync" in domains:
        names.add("calendar_get_preferences")
    return domains, history, names
