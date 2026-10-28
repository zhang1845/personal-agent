import json
import sys
import time
import unittest

from personal_agent.bridge import MCPBridge
from personal_agent.errors import AgentError


class WriteTimeoutTests(unittest.TestCase):
    def test_child_that_stops_reading_cannot_block_a_large_write(self):
        script = """
import json,sys,time
request=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'protocolVersion':'2025-06-18','capabilities':{'tools':{}}}}), flush=True)
time.sleep(5)
"""
        bridge = MCPBridge({"profile_id": "blocked-child", "command": sys.executable,
                            "args": ["-u", "-c", script], "writes_enabled": True,
                            "timeout_seconds": 0.15, "tools": {"create_event": {"name": "create"}}})
        started = time.monotonic()
        try:
            with self.assertRaises(AgentError) as error:
                bridge.create_event({"description": "x" * 400000}, "write-once")
            self.assertEqual("unknown_outcome", error.exception.code)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
