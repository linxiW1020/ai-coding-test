"""Real stdio transport against a local fake server; no Chrome or network."""

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest


REPO = "https://github.com/example/pilot"
PROXY = Path(__file__).resolve().parents[1] / "workflow" / "readonly_chrome_mcp.py"
FAKE_SERVER = r'''
import json
from pathlib import Path
import sys

sys.stdin.reconfigure(encoding="utf-8", errors="strict")
sys.stdout.reconfigure(encoding="utf-8", errors="strict", line_buffering=True)
log = Path(sys.argv[1])
awaiting = {}
for line in sys.stdin:
    request = json.loads(line)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request, ensure_ascii=False) + "\n")
    if "id" not in request:
        continue
    if "method" not in request:
        original = awaiting.pop(request["id"], None)
        if original is None:
            continue
        result = {"content": [{"type": "text", "text": "approval result received"}], "isError": True,
                  "structuredContent": {"received_response_id": request["id"],
                                        "received_action": request.get("result", {}).get("action"),
                                        "received_error": request.get("error")}}
        print(json.dumps({"jsonrpc": "2.0", "id": original, "result": result}, ensure_ascii=False), flush=True)
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-cua", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "js", "description": "JavaScript", "inputSchema": {"type": "object"}}, {"name": "js_reset", "description": "Reset", "inputSchema": {"type": "object"}}, {"name": "exec_command", "description": "must be hidden", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        arguments = request["params"].get("arguments", {})
        code = arguments.get("code", "")
        title = arguments.get("title")
        if title in ("fake-elicitation", "fake-sampling"):
            # Intentionally use the same ID as the in-flight client request.
            awaiting[request["id"]] = request["id"]
            server_method = "elicitation/create" if title == "fake-elicitation" else "sampling/createMessage"
            params = {"message": "Request actual user approval; do not auto-approve", "requestedSchema": {"type": "object", "properties": {}}}
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "method": server_method, "params": params}), flush=True)
            continue
        result = {"content": [{"type": "text", "text": "工具返回中文 🧪"}], "structuredContent": {"received_code": code}}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}, ensure_ascii=False), flush=True)
'''


class ReadOnlyChromeTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-readonly-stdio-")
        self.root = Path(self.temporary.name)
        self.log = self.root / "received.jsonl"
        spec = self.root / "upstream.json"
        spec.write_text(json.dumps({"command": sys.executable,
                                    "args": ["-u", "-c", FAKE_SERVER, str(self.log)],
                                    "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}},
                                   ensure_ascii=False), encoding="utf-8")
        self.stderr_path = self.root / "proxy-stderr.log"
        self.stderr = self.stderr_path.open("wb")
        environment = {**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "gbk:surrogateescape"}
        # Binary client I/O ensures the test really sends UTF-8 bytes while
        # the proxy starts with an explicitly incompatible default codec.
        self.process = subprocess.Popen([sys.executable, str(PROXY), "--upstream", str(spec), "--repo-url", REPO],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, env=environment, shell=False)
        self.responses = queue.Queue()

        def collect():
            try:
                for raw in self.process.stdout:
                    self.responses.put(json.loads(raw.decode("utf-8", errors="strict")))
            except (OSError, ValueError) as exc:
                self.responses.put(exc)
            finally:
                self.responses.put(None)

        self.reader = threading.Thread(target=collect, daemon=True)
        self.reader.start()

    def tearDown(self):
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.stderr.close()
        self.temporary.cleanup()

    def send(self, request):
        self.process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def receive(self):
        try:
            response = self.responses.get(timeout=8)
        except queue.Empty:
            self.fail("Proxy response timed out: " + self.stderr_path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(response, dict):
            self.fail("Proxy exited or emitted non-UTF-8 JSON: " + repr(response)
                      + "; stderr=" + self.stderr_path.read_text(encoding="utf-8", errors="replace"))
        return response

    def request(self, request_id, method, params=None):
        request = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self.send(request)
        response = self.receive()
        self.assertEqual(response.get("id"), request_id)
        return response

    def call(self, request_id, code):
        return self.request(request_id, "tools/call", {"name": "js", "arguments": {"code": code, "title": "只读验证 🧪"}})

    def test_utf8_chinese_and_emoji_survive_legacy_parent_encoding(self):
        initialized = self.request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "只读客户端 🧪", "version": "1"}})
        self.assertIn("result", initialized)
        inventory = self.request(2, "tools/list")
        self.assertEqual([tool["name"] for tool in inventory["result"]["tools"]], ["js", "js_reset"])
        code = 'var wfTab = await cua.createBrowserTab("chrome", "' + REPO + '", {"sessionName":"🔎 工作流只读 🧪"});'
        opened = self.call(3, code)
        self.assertNotIn("error", opened)
        self.assertEqual(opened["result"]["structuredContent"]["received_code"], code)
        self.assertEqual(opened["result"]["content"][0]["text"], "工具返回中文 🧪")
        observed = self.call(4, "await wfTab.getAXState();")
        self.assertNotIn("error", observed)
        logged = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(logged[0]["params"]["clientInfo"]["name"], "只读客户端 🧪")
        self.assertEqual(logged[2]["params"]["arguments"]["code"], code)

    def test_click_is_rejected_and_never_sent_to_fake_upstream(self):
        opened = self.call(10, 'var wfTab = await cua.createBrowserTab("chrome", "' + REPO + '", {"sessionName":"只读 🧪"});')
        self.assertIn("result", opened)
        blocked = self.call(11, 'await wfTab.click({"role":"button","name":"Merge"});')
        self.assertEqual(blocked["error"]["code"], -32602)
        # A subsequent allowed request also flushes preceding upstream work.
        self.assertIn("result", self.call(12, "await wfTab.getAXState();"))
        logged = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([request["id"] for request in logged], [10, 12])
        self.assertNotIn("click", self.log.read_text(encoding="utf-8"))

    def test_elicitation_is_forwarded_and_real_client_decline_restores_original_id(self):
        code = 'var wfTab = await cua.createBrowserTab("chrome", "' + REPO + '", {"sessionName":"需要用户授权 🧪"});'
        self.send({"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                   "params": {"name": "js", "arguments": {"code": code, "title": "fake-elicitation"}}})
        question = self.receive()
        self.assertEqual(question["method"], "elicitation/create")
        self.assertTrue(question["id"].startswith("readonly-upstream:"))
        self.assertNotEqual(question["id"], 20)
        self.assertIn("actual user approval", question["params"]["message"])
        # The fake client deliberately declines. The proxy must neither answer
        # first nor change that decision into an acceptance.
        self.send({"jsonrpc": "2.0", "id": question["id"], "result": {"action": "decline"}})
        completed = self.receive()
        self.assertEqual(completed["id"], 20)
        observed = completed["result"]["structuredContent"]
        self.assertEqual(observed["received_response_id"], 20)
        self.assertEqual(observed["received_action"], "decline")
        logged = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(logged), 2)
        self.assertEqual(logged[1], {"jsonrpc": "2.0", "id": 20, "result": {"action": "decline"}})
        self.assertFalse(any(record.get("result", {}).get("action") == "accept" for record in logged))

    def test_sampling_request_remains_rejected_without_reaching_client(self):
        code = 'var wfTab = await cua.createBrowserTab("chrome", "' + REPO + '", {"sessionName":"只读"});'
        response = self.request(30, "tools/call", {"name": "js", "arguments": {"code": code, "title": "fake-sampling"}})
        self.assertNotIn("method", response)
        self.assertEqual(response["result"]["structuredContent"]["received_error"]["code"], -32601)
        logged = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(logged[1]["error"]["code"], -32601)
        self.assertNotIn("result", logged[1])

    def test_client_approval_error_is_relayed_without_automatic_answer(self):
        code = 'var wfTab = await cua.createBrowserTab("chrome", "' + REPO + '", {"sessionName":"只读"});'
        self.send({"jsonrpc": "2.0", "id": 25, "method": "tools/call",
                   "params": {"name": "js", "arguments": {"code": code, "title": "fake-elicitation"}}})
        question = self.receive()
        self.assertEqual(question["method"], "elicitation/create")
        denied = {"code": -32000, "message": "Approval denied by client policy"}
        self.send({"jsonrpc": "2.0", "id": question["id"], "error": denied})
        completed = self.receive()
        self.assertEqual(completed["id"], 25)
        self.assertEqual(completed["result"]["structuredContent"]["received_error"], denied)
        logged = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(logged[1], {"jsonrpc": "2.0", "id": 25, "error": denied})

    def test_client_cannot_collide_with_reserved_upstream_namespace(self):
        denied = self.request("readonly-upstream:1", "ping")
        self.assertEqual(denied["error"]["code"], -32600)
        self.assertIn("result", self.request(40, "ping"))
        logged = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([request["id"] for request in logged], [40])


if __name__ == "__main__":
    unittest.main()
