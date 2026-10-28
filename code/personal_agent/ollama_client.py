"""Bounded client for a local Ollama service, without cloud or proxy fallback."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .errors import AgentError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaClient:
    BASE_URL = "http://127.0.0.1:11434"
    MAX_REQUEST = 512 * 1024
    MAX_RESPONSE = 2 * 1024 * 1024
    _MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")

    def __init__(self, timeout=25, opener=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise AgentError("invalid_config", "本地模型超时设置无效")
        self.timeout = timeout
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def _valid_model(cls, name):
        return isinstance(name, str) and bool(cls._MODEL.fullmatch(name)) and "://" not in name and "cloud" not in name.lower()

    def _request(self, path, payload=None, timeout=None):
        if path not in ("/api/tags", "/api/chat"):
            raise AgentError("model_unavailable", "只允许访问本机模型接口")
        data = None
        if payload is not None:
            try:
                data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise AgentError("validation_error", "模型请求包含无效数据")
            if len(data) > self.MAX_REQUEST:
                raise AgentError("validation_error", "模型上下文过大，请开始新的对话或缩短内容")
        request = Request(self.BASE_URL + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(self.MAX_RESPONSE + 1)
            if len(raw) > self.MAX_RESPONSE:
                raise AgentError("model_invalid_response", "本地模型响应超过大小上限")
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict) or result.get("error"):
                raise ValueError()
            return result
        except AgentError:
            raise
        except (HTTPError, URLError, OSError, TimeoutError):
            raise AgentError("model_unavailable", "无法连接本机 Ollama，请先启动本地服务；未切换到云模型") from None
        except (ValueError, UnicodeError, RecursionError):
            raise AgentError("model_invalid_response", "本地模型返回了无法识别的结果") from None

    def list_models(self):
        try:
            result = self._request("/api/tags", timeout=min(2, self.timeout))
            models = result.get("models")
            if not isinstance(models, list) or len(models) > 100:
                raise AgentError("model_invalid_response", "本地模型列表格式无效或超过上限")
            valid = []
            for item in models:
                if not isinstance(item, dict):
                    raise AgentError("model_invalid_response", "本地模型列表格式无效")
                name = item.get("name", item.get("model"))
                if item.get("remote_host") or item.get("remote_model") or not self._valid_model(name):
                    continue
                details = item.get("details", {})
                if isinstance(details, dict) and (details.get("remote_host") or details.get("remote_model")):
                    continue
                size = item.get("size", 0)
                if type(size) is not int or size < 0:
                    size = 0
                valid.append({"name": name, "size": size})
            return {"available": True, "models": valid}
        except AgentError as error:
            return {"available": False, "models": [], "error": error.as_dict()}

    def chat(self, messages, tools, model):
        if not self._valid_model(model):
            raise AgentError("model_unsupported", "请选择已安装的本地模型，不允许云模型名称")
        discovered = self.list_models()
        if not discovered["available"]:
            raise AgentError("model_unavailable", "本机 Ollama 不可用；未调用付费或云模型")
        if model not in {item["name"] for item in discovered["models"]}:
            raise AgentError("model_unsupported", "所选模型未出现在本地可用列表中，不自动下载")
        if not isinstance(messages, list) or not messages or len(messages) > 50 or not isinstance(tools, list):
            raise AgentError("validation_error", "模型消息或工具列表无效")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant", "tool"):
                raise AgentError("validation_error", "模型消息格式无效")
            if not isinstance(message.get("content", ""), str):
                raise AgentError("validation_error", "模型消息必须是文本")
        result = self._request("/api/chat", {"model": model, "messages": messages, "tools": tools,
                                             "stream": False, "options": {"temperature": 0.1, "num_predict": 1200, "num_ctx": 8192}})
        message = result.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content", ""), str):
            raise AgentError("model_invalid_response", "本地模型未返回有效消息")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or len(calls) > 8:
            raise AgentError("model_invalid_response", "模型工具调用数量或格式无效")
        return message
