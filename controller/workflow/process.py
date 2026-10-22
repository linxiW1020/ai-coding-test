"""Bounded child processes; all commands use argv, never a shell."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable


class ProcessStopped(RuntimeError):
    pass


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_process(argv: list[str], cwd: Path, output_dir: Path, *,
                timeout: float, stdin: str | None = None,
                tick: Callable[[int], None] | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    monotonic_start = time.monotonic()
    stdout_path, stderr_path = output_dir / "stdout.log", output_dir / "stderr.log"
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, text=True, encoding="utf-8", env=environment, **options)
        try:
            if tick:
                tick(process.pid)
            if stdin is not None:
                process.stdin.write(stdin)
                process.stdin.close()
            while process.poll() is None:
                if time.monotonic() - monotonic_start >= timeout:
                    raise ProcessStopped("process_timeout")
                if tick:
                    tick(process.pid)
                time.sleep(0.2)
        except BaseException:
            stop_process(process)
            raise
    return {"argv": argv, "returncode": process.returncode, "started_at": started,
            "finished_at": time.time(), "stdout": str(stdout_path), "stderr": str(stderr_path)}


def run_codex(codex: str, cwd: Path, output_dir: Path, prompt: str, schema: dict, *,
              sandbox: str = "read-only", timeout: float = 180,
              tick: Callable[[int], None] | None = None, skip_git: bool = False,
              config_overrides: dict | None = None, role: str = "local",
              browser_target: str | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    schema_path, result_path = output_dir / "schema.json", output_dir / "result.json"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    if result_path.exists():
        raise ValueError("A Codex invocation directory must be new; refusing stale output")
    argv = [codex, "--no-daemon", "exec", "--ephemeral", "--json", "--color", "never", "--sandbox", sandbox,
            "--ignore-user-config", "--disable", "plugins", "--disable", "apps",
            "--disable", "multi_agent", "--disable", "hooks", "-c", "mcp_servers={}",
            "-C", str(cwd), "--output-schema", str(schema_path), "-o", str(result_path)]
    if os.name == "nt":
        # Preserve the already-configured Windows backend while omitting all
        # unrelated user plugins/MCPs. This does not disable the sandbox.
        argv.extend(["-c", 'windows.sandbox="elevated"'])
    if role not in ("local", "browser-read", "browser-write"):
        raise ValueError("Unknown Codex role")
    if config_overrides:
        raise ValueError("Role transports are pinned; arbitrary MCP overrides are not permitted")
    overrides = {}
    if role == "local":
        argv.extend(["--disable", "computer_use", "--disable", "browser_use", "--disable", "browser_use_external"])
    else:
        argv.extend(["--disable", "shell_tool", "--disable", "unified_exec"])
        listing = subprocess.run([codex, "mcp", "list", "--json"], capture_output=True,
                                 text=True, encoding="utf-8", timeout=30, check=True)
        source = next(x["transport"] for x in json.loads(listing.stdout) if x["name"] == "cua_repl")
        environment = source.get("env") or {}
        if any(any(word in key.upper() for word in ("PASSWORD", "SECRET", "TOKEN", "API_KEY")) for key in environment):
            raise RuntimeError("Refusing to serialize credential-bearing browser MCP configuration")
        upstream = {"command": source["command"], "args": source.get("args", []), "env": environment}
        if role == "browser-read":
            if not browser_target:
                raise ValueError("Read-only Chrome requires an explicit URL boundary")
            spec = output_dir / "chrome-upstream.json"
            spec.write_text(json.dumps(upstream, ensure_ascii=False, indent=2), encoding="utf-8")
            command = sys.executable
            arguments = [str(Path(__file__).with_name("readonly_chrome_mcp.py")), "--upstream", str(spec),
                         "--repo-url", browser_target]
            environment = {}
        else:
            command, arguments = source["command"], source.get("args", [])
        overrides.setdefault("mcp_servers.cua_repl.command", command)
        overrides.setdefault("mcp_servers.cua_repl.args", arguments)
        for key, value in environment.items():
            overrides.setdefault("mcp_servers.cua_repl.env." + key, value)
    if skip_git:
        argv.append("--skip-git-repo-check")
    for key, value in overrides.items():
        if not key.startswith("mcp_servers.") or not (isinstance(value, (str, bool)) or isinstance(value, list) and all(isinstance(x, str) for x in value)):
            raise ValueError("Only explicit MCP string/bool invocation overrides are supported")
        argv.extend(["-c", key + "=" + json.dumps(value, ensure_ascii=False)])
    argv.append("-")
    (output_dir / "invocation.json").write_text(json.dumps({"role": role, "sandbox": sandbox, "argv": argv},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
    receipt = run_process(argv, cwd, output_dir, timeout=timeout, stdin=prompt, tick=tick)
    if receipt["returncode"] != 0:
        raise RuntimeError(f"Codex exited {receipt['returncode']}; see {output_dir}")
    if not result_path.is_file():
        raise RuntimeError(f"Codex did not return structured output; see {output_dir}")
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise RuntimeError("Codex output is not an object")
    for key in schema.get("required", []):
        if key not in result:
            raise RuntimeError(f"Codex output missing {key}")
    receipt["result"] = result
    usage = []
    for line in Path(receipt["stdout"]).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("type") == "turn.completed" and isinstance(item.get("usage"), dict):
            usage.append(item["usage"])
    receipt["usage"] = usage
    (output_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt
