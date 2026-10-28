"""Optional stdio MCP adapter with an explicit normalized calendar contract.

This is a transport adapter, not an OAuth implementation or a vendor connector.
The configured child process owns credentials and provider-specific pagination.
No command is started until a backend operation is requested.
"""

import copy
import json
import os
import select
import subprocess
import threading
import time
from datetime import datetime, timedelta

from .errors import AgentError
from .platform_utils import IS_WINDOWS
from .pipe_transport import ThreadedPipes


class _ProtocolError(Exception):
    pass


class MCPBridge:
    kind = "external_mcp"
    _PROTOCOLS = ("2025-06-18", "2024-11-05")
    _OPERATIONS = {
        "list_calendars", "list_events", "get_event", "create_event",
        "update_event", "delete_event",
    }
    _PAGINATION_KEYS = (
        "nextCursor", "next_cursor", "nextPageToken", "next_page_token",
        "@odata.nextLink", "has_more", "truncated",
    )

    def __init__(self, config):
        if not isinstance(config, dict):
            raise AgentError("invalid_config", "外部日历配置必须是 JSON 对象")
        self.config = copy.deepcopy(config)
        profile_id = config.get("profile_id")
        if (
            not isinstance(profile_id, str) or not profile_id.strip()
            or profile_id != profile_id.strip() or len(profile_id) > 128
        ):
            raise AgentError("invalid_config", "需显式指定非空且不含首尾空白的 profile_id")
        # Stable account-and-connector identity; credentials are never an identity.
        # The service binds this to one SQLite DB. Account changes require a new ID/DB.
        self.profile_id = profile_id
        command = config.get("command")
        args = config.get("args", [])
        env = config.get("env", {})
        if not isinstance(command, str) or not command or "\x00" in command:
            raise AgentError("invalid_config", "需显式指定外部 MCP 可执行文件")
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            raise AgentError("invalid_config", "外部 MCP args 必须是字符串数组")
        if not isinstance(env, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or "=" in k or "\x00" in k + v
            for k, v in env.items()
        ):
            raise AgentError("invalid_config", "外部 MCP env 必须是字符串映射")
        tools = config.get("tools", {})
        if not isinstance(tools, dict) or set(tools) - self._OPERATIONS:
            raise AgentError("invalid_config", "外部 MCP 工具映射包含不支持的操作")
        for operation, mapping in tools.items():
            if not isinstance(mapping, dict) or not isinstance(mapping.get("name"), str) or not mapping["name"]:
                raise AgentError("invalid_config", "每个工具映射需要明确的 name")
            for field in ("argument_map", "constants", "event_fields", "calendar_fields"):
                if field in mapping and not isinstance(mapping[field], dict):
                    raise AgentError("invalid_config", "工具字段映射必须是对象")
            for field in ("response_path", "complete_path"):
                if field in mapping and not isinstance(mapping[field], str):
                    raise AgentError("invalid_config", "结果路径必须是点分字符串")
            for field in ("event_fields", "calendar_fields"):
                self._validate_field_map(mapping.get(field, {}))
        self._validate_field_map(config.get("event_fields", {}))
        self.timeout = self._number(config.get("timeout_seconds", 10), 0.05, 120)
        self.max_bytes = int(self._number(config.get("max_response_bytes", 1048576), 1024, 4194304))
        self.max_events = int(self._number(config.get("max_events", 200), 1, 1000))
        self.max_days = int(self._number(config.get("max_days", 31), 1, 366))
        self.capabilities = {
            "remote": True,
            "writes_enabled": config.get("writes_enabled") is True,
            "supports_reminders": config.get("supports_reminders") is True,
            "supports_recurrence": False,
        }
        self._process = None
        self._pipes = None
        self._threaded_pipes = IS_WINDOWS
        self._buffer = b""
        self._counter = 0
        self._initialized = False
        self._lock = threading.RLock()

    @staticmethod
    def _validate_field_map(mapping):
        if not isinstance(mapping, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in mapping.items()
        ):
            raise AgentError("invalid_config", "结果字段映射必须是非空字符串路径")

    @staticmethod
    def _number(value, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise AgentError("invalid_config", "连接器数值配置超出允许范围")
        return value

    @staticmethod
    def _path(value, path):
        if path in (None, ""):
            return value
        current = value
        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                raise _ProtocolError("Missing mapped field")
            current = current[key]
        return current

    def _write(self, message, deadline=None):
        data = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(data) > self.max_bytes:
            raise _ProtocolError("Request is too large")
        deadline = deadline or time.monotonic() + self.timeout
        if self._pipes is not None:
            self._pipes.write(data, deadline)
            return
        view = memoryview(data)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP request timeout")
            _, ready, _ = select.select([], [self._process.stdin], [], remaining)
            if not ready:
                raise TimeoutError("MCP request timeout")
            try:
                count = os.write(self._process.stdin.fileno(), view)
            except BlockingIOError:
                continue
            if not count:
                raise _ProtocolError("MCP request pipe closed")
            view = view[count:]

    def _read(self, deadline):
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if not line:
                    raise _ProtocolError("Empty protocol line")
                try:
                    message = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeError):
                    raise _ProtocolError("Non JSON protocol output") from None
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise _ProtocolError("Invalid JSON RPC response")
                return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP response timeout")
            if self._pipes is not None:
                data = self._pipes.read(deadline)
            else:
                ready, _, _ = select.select([self._process.stdout], [], [], remaining)
                if not ready:
                    raise TimeoutError("MCP response timeout")
                data = os.read(self._process.stdout.fileno(), 65536)
            if not data:
                raise _ProtocolError("MCP process disconnected")
            self._buffer += data
            if len(self._buffer) > self.max_bytes:
                raise _ProtocolError("MCP response too large")

    def _rpc(self, method, params):
        self._counter += 1
        request_id = self._counter
        deadline = time.monotonic() + self.timeout
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, deadline)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("MCP response timeout")
            message = self._read(deadline)
            if "method" in message:
                if "id" in message:
                    self._write({"jsonrpc": "2.0", "id": message["id"], "error": {
                        "code": -32601, "message": "Client method not supported",
                    }}, deadline)
                # Notifications are legal between a request and its response.
                continue
            if message.get("id") != request_id:
                raise _ProtocolError("Unexpected response id")
            if "error" in message or "result" not in message:
                raise _ProtocolError("MCP request failed")
            return message["result"]

    def _ensure_started(self):
        if self._initialized and self._process and self._process.poll() is None:
            return
        self.close()
        try:
            # Inherit only runtime settings, never all ambient API credentials.
            env = {k: os.environ[k] for k in (
                "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TMPDIR",
                "TEMP", "TMP", "LANG", "LC_ALL", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            ) if k in os.environ}
            env.update(self.config.get("env", {}))
            env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            self._process = subprocess.Popen(
                [self.config["command"]] + self.config.get("args", []),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
            if self._threaded_pipes:
                self._pipes = ThreadedPipes(self._process)
            else:
                os.set_blocking(self._process.stdin.fileno(), False)
            result = self._rpc("initialize", {
                "protocolVersion": self._PROTOCOLS[0], "capabilities": {},
                "clientInfo": {"name": "personal-agent-calendar-bridge", "version": "0.1.0"},
            })
            if not isinstance(result, dict) or result.get("protocolVersion") not in self._PROTOCOLS:
                raise _ProtocolError("Unsupported negotiated protocol")
            capabilities = result.get("capabilities", {})
            if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
                raise _ProtocolError("Remote server does not offer tools")
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._initialized = True
        except (OSError, ValueError, _ProtocolError, TimeoutError):
            self.close()
            raise AgentError("backend_unavailable", "外部日历 MCP 初始化失败，未发送日历操作") from None

    def _invoke(self, operation, arguments, write=False):
        mapping = self.config.get("tools", {}).get(operation)
        if not mapping:
            raise AgentError("unsupported_operation", "连接器未配置此操作", {"operation": operation})
        if write and self.config.get("writes_enabled") is not True:
            raise AgentError("writes_disabled", "外部日历写入尚未明确启用")
        argument_map = mapping.get("argument_map", {})
        params = dict(mapping.get("constants", {}))
        for key, value in arguments.items():
            destination = argument_map.get(key, key)
            if destination is None:
                continue
            if not isinstance(destination, str) or not destination:
                raise AgentError("invalid_config", "参数映射目标必须是非空字符串")
            params[destination] = value
        with self._lock:
            self._ensure_started()
            try:
                result = self._rpc("tools/call", {"name": mapping["name"], "arguments": params})
                if not isinstance(result, dict) or result.get("isError"):
                    raise _ProtocolError("Tool failed")
                data = result.get("structuredContent")
                if data is None:
                    content = result.get("content", [])
                    text = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"]
                    if len(text) != 1 or not isinstance(text[0], str):
                        raise _ProtocolError("Expected one JSON text result")
                    data = json.loads(text[0])
                if not isinstance(data, dict):
                    raise _ProtocolError("Tool payload must be a JSON object")
                if data.get("error") or data.get("ok") is False or data.get("code") not in (None, 0, "0"):
                    raise _ProtocolError("Provider reports a failure")
                return data, mapping
            except (OSError, TypeError, ValueError, _ProtocolError, TimeoutError):
                self.close()
                code = "unknown_outcome" if write else "backend_unavailable"
                message = "外部写入结果未知，请核查真实日历，禁止自动重发" if write else "外部日历查询失败，不能判断为空闲"
                raise AgentError(code, message, {"operation": operation}) from None

    @staticmethod
    def _datetime(value):
        if not isinstance(value, str):
            raise _ProtocolError("Event time must be ISO text")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise _ProtocolError("Invalid event timestamp") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise _ProtocolError("Event timestamp needs an explicit offset")
        return parsed

    def _event(self, value, mapping):
        if not isinstance(value, dict):
            raise _ProtocolError("Event is not an object")
        fields = mapping.get("event_fields", self.config.get("event_fields", {}))
        required = ("id", "title", "start", "end", "calendar_id", "version", "all_day", "recurring")
        event = {key: self._path(value, fields.get(key, key)) for key in required}
        for key in ("id", "title", "calendar_id", "version"):
            if not isinstance(event[key], str) or not event[key]:
                raise _ProtocolError("Event identity is incomplete")
        if type(event["all_day"]) is not bool or type(event["recurring"]) is not bool:
            raise _ProtocolError("Event type is ambiguous")
        start, end = self._datetime(event["start"]), self._datetime(event["end"])
        if end <= start:
            raise _ProtocolError("Invalid event interval")
        event["start"], event["end"] = start.isoformat(), end.isoformat()
        for key in ("location", "description"):
            try:
                event[key] = self._path(value, fields.get(key, key))
            except _ProtocolError:
                event[key] = ""
            if not isinstance(event[key], str):
                raise _ProtocolError("Invalid event text")
        try:
            reminder = self._path(value, fields.get("reminder_minutes", "reminder_minutes"))
        except _ProtocolError:
            reminder = None
        if reminder is not None and (type(reminder) is not int or not 0 <= reminder <= 40320):
            raise _ProtocolError("Invalid reminder")
        event["reminder_minutes"] = reminder
        return event

    def _parse(self, operation, data, mapping, default_path, write=False):
        try:
            value = self._path(data, mapping.get("response_path", default_path))
            if operation in ("list_calendars", "list_events"):
                if self._path(data, mapping.get("complete_path", "complete")) is not True:
                    raise _ProtocolError("Incomplete list")
                envelope = data
                ancestors = [data]
                for segment in mapping.get("response_path", default_path).split("."):
                    if isinstance(envelope, dict) and segment in envelope:
                        envelope = envelope[segment]
                        if isinstance(envelope, dict):
                            ancestors.append(envelope)
                for ancestor in ancestors:
                    if any(ancestor.get(key) for key in self._PAGINATION_KEYS):
                        raise _ProtocolError("Unconsumed page or truncated data")
                if not isinstance(value, list):
                    raise _ProtocolError("List response must be an array")
                maximum = self.max_events if operation == "list_events" else 50
                if len(value) > maximum:
                    raise _ProtocolError("List result exceeds bound")
            if operation == "list_calendars":
                calendars = []
                fields = mapping.get("calendar_fields", {})
                for raw in value:
                    calendar = {key: self._path(raw, fields.get(key, key)) for key in ("id", "name", "writable")}
                    if any(not isinstance(calendar[k], str) or not calendar[k] for k in ("id", "name")):
                        raise _ProtocolError("Invalid calendar identity")
                    if type(calendar["writable"]) is not bool:
                        raise _ProtocolError("Unknown calendar write access")
                    calendars.append(calendar)
                return calendars
            if operation == "list_events":
                events = [self._event(raw, mapping) for raw in value]
                if len({event["id"] for event in events}) != len(events):
                    raise _ProtocolError("Event occurrence identities are not unique")
                return events
            if operation == "delete_event":
                if not isinstance(value, dict) or value.get("deleted") is not True or not isinstance(value.get("event_id"), str):
                    raise _ProtocolError("Deletion was not verified")
                return value
            return self._event(value, mapping)
        except (_ProtocolError, TypeError, ValueError):
            code = "unknown_outcome" if write else "backend_unavailable"
            message = "外部写入响应无法核实，禁止自动重发" if write else "外部日历响应不完整，不能判断为空闲"
            raise AgentError(code, message, {"operation": operation}) from None

    def list_calendars(self):
        data, mapping = self._invoke("list_calendars", {})
        return self._parse("list_calendars", data, mapping, "calendars")

    def list_events(self, start, end, calendar_id):
        try:
            a, b = self._datetime(start), self._datetime(end)
            if b <= a or b - a > timedelta(days=self.max_days):
                raise _ProtocolError("Range exceeds bound")
        except _ProtocolError:
            raise AgentError("invalid_range", "日程查询需使用含偏移时间，且跨度不可超过连接器上限") from None
        data, mapping = self._invoke("list_events", {"start": start, "end": end, "calendar_id": calendar_id})
        events = self._parse("list_events", data, mapping, "events")
        for event in events:
            if event["calendar_id"] != calendar_id or self._datetime(event["end"]) <= a or self._datetime(event["start"]) >= b:
                raise AgentError("backend_unavailable", "连接器返回不属于所选日历或时间范围的事件")
        return events

    def get_event(self, event_id):
        data, mapping = self._invoke("get_event", {"event_id": event_id})
        event = self._parse("get_event", data, mapping, "event")
        if event["id"] != event_id:
            raise AgentError("backend_unavailable", "连接器返回了不同日程，拒绝继续操作")
        return event

    def _write_event(self, operation, arguments, event_id=None):
        if arguments.get("recurring") or arguments.get("recurrence"):
            raise AgentError("unsupported_recurrence", "第一版不向外部日历写入重复系列")
        if arguments.get("reminder_minutes") is not None and not self.capabilities["supports_reminders"]:
            raise AgentError("unsupported_reminder", "此连接器未声明原生提醒能力")
        data, mapping = self._invoke(operation, arguments, write=True)
        event = self._parse(operation, data, mapping, "event", write=True)
        if event_id is not None and event["id"] != event_id:
            raise AgentError("unknown_outcome", "外部写入返回不同日程，需人工核查")
        return event

    def create_event(self, eventdict, request_id):
        if not isinstance(eventdict, dict):
            raise AgentError("invalid_arguments", "事件参数必须是对象")
        arguments = dict(eventdict)
        arguments["request_id"] = request_id
        return self._write_event("create_event", arguments)

    def update_event(self, event_id, eventdict, expected_version, request_id):
        if not isinstance(eventdict, dict):
            raise AgentError("invalid_arguments", "事件参数必须是对象")
        arguments = dict(eventdict)
        arguments.update(event_id=event_id, expected_version=expected_version, request_id=request_id)
        return self._write_event("update_event", arguments, event_id)

    def delete_event(self, event_id, expected_version, request_id):
        data, mapping = self._invoke("delete_event", {
            "event_id": event_id, "expected_version": expected_version, "request_id": request_id,
        }, write=True)
        deleted = self._parse("delete_event", data, mapping, "", write=True)
        if deleted["event_id"] != event_id:
            raise AgentError("unknown_outcome", "外部删除响应的日程不匹配，需人工核查")
        return deleted

    def close(self):
        with self._lock:
            process = self._process
            pipes = self._pipes
            self._pipes = None
            if pipes is not None:
                pipes.stop()
            self._process = None
            self._initialized = False
            self._buffer = b""
            if process is None:
                return
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
            finally:
                if pipes is not None:
                    pipes.join()
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
