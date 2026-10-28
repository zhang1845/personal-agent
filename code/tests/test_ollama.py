"""HTTP-boundary tests use a fake opener and never contact user services."""

import json
import unittest
from urllib.error import URLError
from urllib.request import Request

from personal_agent.errors import AgentError
from personal_agent.ollama_client import OllamaClient, _NoRedirect


class FakeResponse:
    def __init__(self, value):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.raw[:limit]


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return FakeResponse(value)


def installed():
    return {"models": [{"name": "qwen-local:8b", "size": 12345}]}


class OllamaTests(unittest.TestCase):
    def test_lists_only_local_models_and_keeps_size(self):
        opener = FakeOpener([{"models": [{"name": "local:8b", "size": 42},
                                         {"name": "remote:cloud", "size": 0},
                                         {"name": "hosted", "remote_host": "https://provider.invalid"},
                                         {"name": "remote-meta", "details": {"remote_model": "hosted"}},
                                         {"name": "https://remote.invalid/model"},
                                         {"name": "another:7b", "size": -2}]}])
        result = OllamaClient(opener=opener).list_models()
        self.assertEqual({"available": True, "models": [{"name": "local:8b", "size": 42}, {"name": "another:7b", "size": 0}]}, result)
        request, timeout = opener.requests[0]
        self.assertEqual("http://127.0.0.1:11434/api/tags", request.full_url)
        self.assertEqual("GET", request.get_method())
        self.assertLessEqual(timeout, 2)

    def test_chat_only_uses_fixed_loopback_endpoint_and_no_stream(self):
        opener = FakeOpener([installed(), {"message": {"role": "assistant", "content": "你好"}}])
        client = OllamaClient(opener=opener)
        result = client.chat([{"role": "user", "content": "你好"}], [], "qwen-local:8b")
        self.assertEqual("你好", result["content"])
        request = opener.requests[1][0]
        self.assertEqual("http://127.0.0.1:11434/api/chat", request.full_url)
        payload = json.loads(request.data)
        self.assertFalse(payload["stream"])
        self.assertEqual(1200, payload["options"]["num_predict"])
        self.assertEqual(["GET", "POST"], [req.get_method() for req, _ in opener.requests])

    def test_cloud_or_remote_url_model_rejected_before_any_request(self):
        opener = FakeOpener([])
        for name in ("gpt-oss:cloud", "QWEN-CLOUD", "http://127.0.0.1/model", "https://provider.invalid/model", "", None):
            with self.subTest(name=name), self.assertRaises(AgentError) as error:
                OllamaClient(opener=opener).chat([{"role": "user", "content": "hi"}], [], name)
            self.assertEqual("model_unsupported", error.exception.code)
        self.assertEqual([], opener.requests)

    def test_unknown_model_not_downloaded(self):
        opener = FakeOpener([installed()])
        with self.assertRaises(AgentError) as error:
            OllamaClient(opener=opener).chat([{"role": "user", "content": "hi"}], [], "not-installed:7b")
        self.assertEqual("model_unsupported", error.exception.code)
        self.assertEqual(1, len(opener.requests))

    def test_list_failure_is_structured_and_does_not_leak_exception(self):
        opener = FakeOpener([URLError("sensitive-provider.invalid SECRET_TOKEN")])
        result = OllamaClient(opener=opener).list_models()
        self.assertFalse(result["available"])
        self.assertEqual([], result["models"])
        self.assertEqual("model_unavailable", result["error"]["code"])
        self.assertNotIn("SECRET_TOKEN", str(result))

    def test_invalid_json_and_invalid_model_list_are_handled(self):
        for response in (b"not-json", b"[]", {"error": "internal details"}, {"models": [None]}):
            with self.subTest(response=response):
                result = OllamaClient(opener=FakeOpener([response])).list_models()
                self.assertFalse(result["available"])
                self.assertEqual("model_invalid_response", result["error"]["code"])
        result = OllamaClient(opener=FakeOpener([{"models": [{"name": "a"}] * 101}])).list_models()
        self.assertFalse(result["available"])

    def test_input_and_output_size_limits(self):
        opener = FakeOpener([installed()])
        client = OllamaClient(opener=opener)
        client.MAX_REQUEST = 20
        with self.assertRaises(AgentError) as error:
            client.chat([{"role": "user", "content": "large"}], [], "qwen-local:8b")
        self.assertEqual("validation_error", error.exception.code)
        self.assertEqual(1, len(opener.requests))
        client = OllamaClient(opener=FakeOpener([b"x" * 32]))
        client.MAX_RESPONSE = 16
        result = client.list_models()
        self.assertFalse(result["available"])
        self.assertEqual("model_invalid_response", result["error"]["code"])

    def test_messages_and_response_tool_calls_are_bounded(self):
        for messages in ([], [{"role": "bad", "content": "hi"}], [{"role": "user", "content": {}}], [{"role": "user", "content": "x"}] * 51):
            with self.subTest(messages=messages), self.assertRaises(AgentError) as error:
                OllamaClient(opener=FakeOpener([installed()])).chat(messages, [], "qwen-local:8b")
            self.assertEqual("validation_error", error.exception.code)
        opener = FakeOpener([installed(), {"message": {"content": "", "tool_calls": [{}] * 9}}])
        with self.assertRaises(AgentError) as error:
            OllamaClient(opener=opener).chat([{"role": "user", "content": "hi"}], [], "qwen-local:8b")
        self.assertEqual("model_invalid_response", error.exception.code)

    def test_redirects_disallowed_and_arbitrary_paths_rejected(self):
        redirect = _NoRedirect()
        self.assertIsNone(redirect.redirect_request(Request("http://127.0.0.1:11434/api/tags"), None, 302, "redirect", {}, "https://remote.invalid"))
        client = OllamaClient(opener=FakeOpener([]))
        with self.assertRaises(AgentError):
            client._request("/api/pull", {"model": "anything"})
        with self.assertRaises(TypeError):
            OllamaClient(base_url="https://remote.invalid")

    def test_timeout_configuration_must_be_bounded(self):
        for timeout in (0, -1, 121, True, "25"):
            with self.subTest(timeout=timeout), self.assertRaises(AgentError):
                OllamaClient(timeout=timeout)


if __name__ == "__main__":
    unittest.main()
