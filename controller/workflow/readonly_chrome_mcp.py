"""A narrow JSONL MCP proxy for repository-scoped, read-only Chrome calls.

This process has no HTTP client or credential store.  It starts the configured
CUA stdio server and permits only the literal templates below; it does not
evaluate JavaScript to decide whether a call is safe.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any
from urllib.parse import urlsplit


_JSON_STRING = r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"'
_CREATE = re.compile(
    r'\s*var\s+wfTab\s*=\s*await\s+cua\.createBrowserTab\(\s*"chrome"\s*,\s*'
    r'(?P<url>' + _JSON_STRING + r')\s*,\s*\{\s*"sessionName"\s*:\s*'
    r'(?P<session>' + _JSON_STRING + r')\s*\}\s*\)\s*;\s*'
)
_AX = re.compile(r'\s*await\s+wfTab\.getAXState\(\s*(?:\{\s*"disableDiffing"\s*:\s*true\s*\})?\s*\)\s*;\s*')
_SCREENSHOT = re.compile(r'\s*await\s+wfTab\.getScreenshot\(\s*\)\s*;\s*')
_GOTO = re.compile(
    r'\s*await\s+wfTab\.goto\(\s*(?P<url>' + _JSON_STRING + r')\s*\)\s*;\s*'
    r'await\s+wfTab\.getAXState\(\s*\)\s*;\s*'
)
_CLOSE = re.compile(r'\s*await\s+wfTab\.close\(\s*\)\s*;\s*')
_UPSTREAM_ID_PREFIX = "readonly-upstream:"

TEMPLATES = '''READ-ONLY PROXY: only these exact JavaScript templates are accepted.
The first js call, and the first after close/reset, must be template 1 only.
Use double-quoted JSON string literals for URL and sessionName.
1. var wfTab = await cua.createBrowserTab("chrome", "HTTPS_REPOSITORY_URL", {"sessionName":"Read-only workflow"});
2. await wfTab.getAXState();
   await wfTab.getAXState({"disableDiffing":true});
3. await wfTab.getScreenshot();
4. await wfTab.goto("HTTPS_URL_IN_SAME_REPOSITORY"); await wfTab.getAXState();
5. await wfTab.close();
No click, fill, evaluate, shell, nodeRepl.write, other variables, comments,
extra statements, or navigation outside the configured repository are allowed.
js_reset accepts an empty argument object and resets this proxy session.
'''


def _parts(url: Any) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(url, str) or not url:
        return None
    # Browsers normalize dot segments, backslashes and encoded delimiters.
    # Reject them rather than comparing an ambiguous pre-normalized path.
    if any(ord(char) <= 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or "\\" in url or "%" in parsed.netloc):
            return None
        _ = parsed.port
    except ValueError:
        return None
    if "%" in parsed.path or not parsed.path.startswith("/"):
        return None
    path = parsed.path.rstrip("/")
    segments = tuple(path[1:].split("/"))
    if any(segment in ("", ".", "..") for segment in segments):
        return None
    return parsed.netloc.casefold(), segments


def _repo_parts(repo_url: Any) -> tuple[str, tuple[str, ...]] | None:
    parts = _parts(repo_url)
    if parts is None or len(parts[1]) != 2:
        return None
    parsed = urlsplit(repo_url)
    if parsed.query or parsed.fragment:
        return None
    return parts[0], tuple(segment.casefold() for segment in parts[1])


def _allowed_url(url: Any, repo_url: str) -> bool:
    repository, target = _repo_parts(repo_url), _parts(url)
    return bool(repository and target and target[0] == repository[0]
                and len(target[1]) >= 2
                and tuple(segment.casefold() for segment in target[1][:2]) == repository[1])


def _operation(code: Any, repo_url: str) -> str | None:
    if not isinstance(code, str) or _repo_parts(repo_url) is None:
        return None
    for pattern, operation in ((_CREATE, "create"), (_GOTO, "goto")):
        match = pattern.fullmatch(code)
        if match:
            try:
                url = json.loads(match.group("url"))
                if operation == "create":
                    session_name = json.loads(match.group("session"))
                    if (not isinstance(session_name, str) or not session_name
                            or any(0xD800 <= ord(char) <= 0xDFFF for char in session_name)):
                        return None
            except (ValueError, TypeError):
                return None
            return operation if _allowed_url(url, repo_url) else None
    for pattern, operation in ((_AX, "read"), (_SCREENSHOT, "read"), (_CLOSE, "close")):
        if pattern.fullmatch(code):
            return operation
    return None


def allowed_code(code: str, repo_url: str) -> bool:
    """Validate syntax and URL scope only; SessionGuard enforces call order."""
    return _operation(code, repo_url) is not None


class SessionGuard:
    """Per-upstream state. Rejected calls never advance the first-call rule."""

    def __init__(self, repo_url: str):
        if _repo_parts(repo_url) is None:
            raise ValueError("repo-url must identify one HTTPS repository")
        self.repo_url = repo_url
        self.has_tab = False

    def accept(self, params: Any) -> tuple[bool, str]:
        if not isinstance(params, dict) or params.get("name") not in ("js", "js_reset"):
            return False, "Only js and js_reset tools are available"
        if any(key not in ("name", "arguments", "_meta") for key in params):
            return False, "Unsupported tool call fields"
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return False, "Tool arguments must be an object"
        if params["name"] == "js_reset":
            if arguments:
                return False, "js_reset requires empty arguments"
            self.has_tab = False
            return True, ""
        if any(key not in ("code", "title", "timeout_ms") for key in arguments):
            return False, "Unsupported js arguments"
        if "title" in arguments and not isinstance(arguments["title"], str):
            return False, "title must be a string"
        if ("timeout_ms" in arguments
                and (type(arguments["timeout_ms"]) is not int or arguments["timeout_ms"] <= 0)):
            return False, "timeout_ms must be a positive integer"
        operation = _operation(arguments.get("code"), self.repo_url)
        if operation is None:
            return False, "JavaScript does not match an allowed read-only template and repository URL"
        if not self.has_tab and operation != "create":
            return False, "The first js call must create wfTab in Chrome using template 1 only"
        if self.has_tab and operation == "create":
            return False, "Close or reset the current tab before creating another"
        if operation == "create":
            self.has_tab = True
        elif operation == "close":
            self.has_tab = False
        return True, ""


def _filtered_tools(message: dict) -> dict:
    result = message.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return message
    tools = []
    for tool in result["tools"]:
        if not isinstance(tool, dict) or tool.get("name") not in ("js", "js_reset"):
            continue
        description = tool.get("description", "")
        if not isinstance(description, str):
            description = ""
        tools.append({**tool, "description": description + "\n\n" + TEMPLATES})
    return {**message, "result": {"tools": tools}}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _load_upstream(path: Path) -> tuple[list[str], dict[str, str]]:
    spec = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(spec, dict) or set(spec) - {"command", "args", "env"}:
        raise ValueError("invalid upstream configuration")
    command, arguments, environment = spec.get("command"), spec.get("args", []), spec.get("env", {})
    if (not isinstance(command, str) or not command or not isinstance(arguments, list)
            or not all(isinstance(value, str) for value in arguments)
            or not isinstance(environment, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items())):
        raise ValueError("invalid upstream configuration")
    return [command, *arguments], {**os.environ, **environment}


def serve(upstream: subprocess.Popen, repo_url: str) -> None:
    guard = SessionGuard(repo_url)
    stopped = threading.Event()
    client_lock, upstream_lock, pending_lock = threading.Lock(), threading.Lock(), threading.Lock()
    pending: dict[str, str] = {}
    # Approval questions belong to the official upstream and the real client.
    # A separate ID namespace prevents an elicitation from looking like the
    # response to an unrelated client request with the same upstream ID.
    upstream_requests: dict[str, Any] = {}
    upstream_sequence = 0

    def key(request_id: Any) -> str:
        return json.dumps(request_id, ensure_ascii=True)

    def write(stream: Any, message: dict, lock: threading.Lock) -> None:
        with lock:
            stream.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n")
            stream.flush()

    def to_client(message: dict) -> None:
        write(sys.stdout, message, client_lock)

    def to_upstream(message: dict) -> None:
        write(upstream.stdin, message, upstream_lock)

    def read_client() -> None:
        try:
            for line in sys.stdin:
                try:
                    message = json.loads(line)
                except ValueError:
                    to_client(_error(None, -32700, "Invalid JSON"))
                    continue
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    to_client(_error(None, -32600, "Invalid JSON-RPC request"))
                    continue
                method, request_id = message.get("method"), message.get("id")
                if "method" not in message:
                    # Never manufacture an approval answer: only relay the
                    # actual client's result/error for an outstanding question.
                    if (not isinstance(request_id, str)
                            or ("result" in message) == ("error" in message)):
                        continue
                    with pending_lock:
                        known = request_id in upstream_requests
                        original_id = upstream_requests.pop(request_id, None)
                    if known:
                        to_upstream({**message, "id": original_id})
                    continue
                if not isinstance(method, str) or ("id" in message and type(request_id) not in (str, int, type(None))):
                    to_client(_error(None, -32600, "Invalid JSON-RPC method or id"))
                    continue
                if "id" not in message:
                    if method.startswith("notifications/"):
                        to_upstream(message)
                    continue
                if isinstance(request_id, str) and request_id.startswith(_UPSTREAM_ID_PREFIX):
                    to_client(_error(request_id, -32600, "Request id uses the reserved upstream namespace"))
                    continue
                if method not in ("initialize", "ping", "tools/list", "tools/call"):
                    to_client(_error(request_id, -32601, "Method unavailable in read-only Chrome proxy"))
                    continue
                with pending_lock:
                    request_key = key(request_id)
                    if request_key in pending:
                        to_client(_error(request_id, -32600, "Duplicate in-flight request id"))
                        continue
                    if method == "tools/call":
                        accepted, reason = guard.accept(message.get("params"))
                        if not accepted:
                            to_client(_error(request_id, -32602, reason))
                            continue
                    pending[request_key] = method
                to_upstream(message)
        except (OSError, ValueError):
            pass
        finally:
            stopped.set()

    def read_upstream() -> None:
        nonlocal upstream_sequence
        try:
            for line in upstream.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    # stdout is protocol-only; do not relay arbitrary log lines.
                    continue
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    continue
                if "method" in message:
                    if "id" in message:
                        if (message["method"] == "elicitation/create"
                                and type(message["id"]) in (str, int, type(None))):
                            with pending_lock:
                                upstream_sequence += 1
                                rewritten_id = _UPSTREAM_ID_PREFIX + str(upstream_sequence)
                                upstream_requests[rewritten_id] = message["id"]
                            to_client({**message, "id": rewritten_id})
                        else:
                            to_upstream(_error(message["id"], -32601, "Client request unavailable in read-only proxy"))
                    else:
                        to_client(message)
                    continue
                if "id" not in message:
                    continue
                with pending_lock:
                    method = pending.pop(key(message["id"]), None)
                if method is not None:
                    to_client(_filtered_tools(message) if method == "tools/list" else message)
        except (OSError, ValueError):
            pass
        finally:
            stopped.set()

    readers = [threading.Thread(target=read_client, daemon=True), threading.Thread(target=read_upstream, daemon=True)]
    for reader in readers:
        reader.start()
    stopped.wait()


def main(argv: list[str] | None = None) -> int:
    # MCP stdio is UTF-8 even when Windows/PYTHONIOENCODING selects a legacy
    # console codec. Configure before reading any request: surrogateescaped
    # UTF-8 labels otherwise look like invalid JavaScript/URL characters.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict", newline=None)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict", newline="\n",
                               line_buffering=True, write_through=True)
    parser = argparse.ArgumentParser(description="Repository-scoped read-only Chrome MCP proxy")
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--repo-url", required=True)
    args = parser.parse_args(argv)
    try:
        SessionGuard(args.repo_url)
        command, environment = _load_upstream(args.upstream)
        upstream = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=None, text=True, encoding="utf-8", env=environment,
                                    bufsize=1, shell=False)
    except (OSError, ValueError, TypeError):
        print("Read-only Chrome proxy: invalid repository/upstream configuration or server unavailable", file=sys.stderr)
        return 2
    try:
        serve(upstream, args.repo_url)
    except KeyboardInterrupt:
        pass
    finally:
        if upstream.stdin is not None:
            try:
                upstream.stdin.close()
            except OSError:
                pass
        if upstream.poll() is None:
            upstream.terminate()
            try:
                upstream.wait(timeout=5)
            except subprocess.TimeoutExpired:
                upstream.kill()
                upstream.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
