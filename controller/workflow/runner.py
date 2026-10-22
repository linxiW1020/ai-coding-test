"""One bounded task attempt for the Codex-only pilot.

SQLite owns local progress. Git and Chrome observations own remote facts.
No deployment, background scheduler, forced push, or automatic PID cleanup is
implemented here. Every command uses the controller's argv-based process API.
"""
from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatchcase
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

from .policy import _PROTECTED_PATTERNS, evaluate_merge
from .process import ProcessStopped, run_codex, run_process
from .state import LeaseLostError, StateStore


IMPLEMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"status": {"type": "string", "enum": ["done", "blocked"]},
                   "summary": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["status", "summary", "reason"],
}
PATCH_IMPLEMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "summary": {"type": "string"}, "reason": {"type": "string"},
        "edits": {"type": "array", "maxItems": 32, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"},
                           "expected_sha256": {"type": "string"},
                           "content": {"type": "string"}},
            "required": ["path", "expected_sha256", "content"],
        }},
    },
    "required": ["status", "summary", "reason", "edits"],
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"verdict": {"type": "string", "enum": ["pass", "fail", "blocked"]},
                   "blocking_findings": {"type": "integer", "minimum": 0},
                   "findings": {"type": "array", "items": {"type": "string"}},
                   "reason": {"type": "string"}},
    "required": ["verdict", "blocking_findings", "findings", "reason"],
}
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_DEFAULT_LIMITS = {"max_iterations": 3, "max_model_calls": 8,
                   "max_seconds": 1200, "command_timeout": 180,
                   "no_progress_limit": 2, "max_ci_polls": 3,
                   "ci_poll_interval_seconds": 15}


class _Blocked(RuntimeError):
    pass


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _repo(value: object) -> tuple[str, str, str] | None:
    if not _text(value):
        return None
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
    else:
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "ssh") or not parsed.hostname:
            return None
        host, path = parsed.hostname, parsed.path.lstrip("/")
    parts = path.rstrip("/").split("/")
    if len(parts) != 2 or any(not part or part in (".", "..") for part in parts):
        return None
    return host.casefold(), parts[0].casefold(), parts[1].removesuffix(".git").casefold()


def _timestamp(value: object) -> float | None:
    try:
        if type(value) in (int, float):
            result = float(value)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            result = parsed.timestamp()
        else:
            return None
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError, OSError):
        return None


def _valid_path(path: object, *, pattern: bool = False) -> bool:
    return (_text(path) and path == path.strip() and not path.startswith("/")
            and not any(char in path for char in ("\\", ":", "\x00"))
            and not any(part in ("", ".", "..") for part in path.split("/"))
            and (pattern or not any(char in path for char in "*?[")))


def _status_paths(raw: str) -> list[str]:
    """Parse porcelain v1 -z, including both ends of a rename/copy."""
    tokens = raw.split("\x00")
    result: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4 or token[2] != " ":
            raise _Blocked("malformed_git_status")
        result.append(token[3:])
        if "R" in token[:2] or "C" in token[:2]:
            if index >= len(tokens) or not tokens[index]:
                raise _Blocked("malformed_git_rename")
            result.append(tokens[index])
            index += 1
    return sorted(set(result))


class _Runner:
    def __init__(self, config: dict, store: StateStore, task: dict,
                 browser: object, codex_run: object, check_run: object) -> None:
        self.config, self.store, self.task = config, store, task
        self.browser, self.codex_run, self.check_run = browser, codex_run, check_run
        self.task_id, self.owner = task["id"], task["owner"]
        self.ctx = dict(task["context"])
        self.phase = task["phase"]
        self.payload = task["payload"]
        self.repo_path = Path(config.get("repo_path", "")).resolve()
        self.runtime = Path(config.get("runtime_dir", ".workflow-runtime")).resolve()
        self.limits = {**_DEFAULT_LIMITS, **config.get("limits", {})}
        self.started_mono = time.monotonic()
        self.last_heartbeat = -math.inf
        self.remaining_at_start = math.inf
        token = hashlib.sha256(self.task_id.encode("utf-8")).hexdigest()[:16]
        self.worktree = self.runtime / "worktrees" / token
        self.output_root = self.runtime / "evidence" / token

    def now(self) -> float:
        return max(float(self.store.clock()), float(self.task.get("updated_at", 0)))

    def save(self, phase: str | None = None) -> None:
        if phase is not None:
            self.phase = phase
        self.task = self.store.checkpoint(self.task_id, self.owner, self.phase, self.ctx)

    def tick(self, pid: int | None = None) -> None:
        current = self.store.get(self.task_id)
        if current["pause_requested"]:
            raise ProcessStopped("pause_requested")
        now = self.now()
        if (now >= self.ctx.get("deadline", math.inf)
                or time.monotonic() - self.started_mono >= self.remaining_at_start):
            raise ProcessStopped("time_budget_exhausted")
        if pid and self.ctx.get("active_process", {}).get("pid") != pid:
            self.ctx["active_process"]["pid"] = pid
            self.save()
        if time.monotonic() - self.last_heartbeat >= 2:
            self.task = self.store.heartbeat(self.task_id, self.owner, lease_seconds=60)
            self.last_heartbeat = time.monotonic()

    def timeout(self) -> float:
        left = min(self.ctx["deadline"] - self.now(),
                   self.remaining_at_start - (time.monotonic() - self.started_mono))
        if left <= 0:
            raise ProcessStopped("time_budget_exhausted")
        return min(float(self.limits["command_timeout"]), left)

    def invoke(self, kind: str, callback: object, *args: object,
               directory: Path | None = None, **kwargs: object) -> dict:
        self.tick()
        self.ctx["command_sequence"] = self.ctx.get("command_sequence", 0) + 1
        directory = directory or self.output_root / f"{self.ctx['command_sequence']:04d}-{kind}"
        self.ctx["active_process"] = {"pid": None, "owner": self.owner,
                                      "kind": kind, "output_dir": str(directory),
                                      "started_at": self.now()}
        self.save()
        try:
            return callback(*args, directory, timeout=self.timeout(), tick=self.tick, **kwargs)
        finally:
            # run_process/run_codex stop and join their own child on exceptions.
            # A hard-killed controller cannot reach here; its PID record stays.
            self.ctx.pop("active_process", None)
            self.save()

    @staticmethod
    def stdout(receipt: dict) -> str:
        if "stdout_text" in receipt:  # Narrow injection point for deterministic tests.
            return receipt["stdout_text"]
        return Path(receipt["stdout"]).read_text(encoding="utf-8", errors="strict")

    def git(self, args: list[str], *, cwd: Path | None = None,
            optional: bool = False) -> tuple[int, str]:
        receipt = self.invoke("git", self.check_run, ["git", *args], cwd or self.repo_path)
        code, output = receipt.get("returncode"), self.stdout(receipt)
        if type(code) is not int:
            raise _Blocked("git_result_unknown")
        if code != 0 and not optional:
            raise _Blocked(f"git_command_failed:{args[0]}:exit={code}")
        return code, output.rstrip("\r\n")

    def head(self, cwd: Path | None = None) -> str:
        code, sha = self.git(["rev-parse", "--verify", "HEAD"], cwd=cwd, optional=True)
        if code or not _SHA.fullmatch(sha):
            raise _Blocked("bootstrap_required" if cwd is None else "worktree_head_unknown")
        return sha

    def grant(self, action: str) -> None:
        grants = self.config.get("grants", {})
        date = _timestamp(grants.get("approved_at"))
        task_ids = grants.get("task_ids")
        if (grants.get(action) is not True or not _text(grants.get("approved_by"))
                or date is None or date > self.now()
                or not isinstance(task_ids, list) or not task_ids or not all(map(_text, task_ids))
                or self.task_id not in task_ids
                or _repo(grants.get("repo_url")) != _repo(self.config.get("repo_url"))):
            raise _Blocked(f"authorization_required:{action}")

    def validate(self) -> None:
        if not self.repo_path.is_dir() or not _repo(self.config.get("repo_url")):
            raise _Blocked("invalid_repository_config")
        if not _text(self.config.get("base_branch")):
            raise _Blocked("base_branch_required")
        for key, value in self.limits.items():
            if key == "max_ci_polls" and type(value) is int and value >= 0:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise _Blocked(f"invalid_limit:{key}")
        for key in ("max_iterations", "max_model_calls", "no_progress_limit", "max_ci_polls"):
            if type(self.limits[key]) is not int:
                raise _Blocked(f"invalid_limit:{key}")
        if self.limits["ci_poll_interval_seconds"] > 60:
            raise _Blocked("invalid_limit:ci_poll_interval_seconds")
        if not _text(self.payload.get("goal")):
            raise _Blocked("goal_required")
        acceptance = self.payload.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance or not all(map(_text, acceptance)):
            raise _Blocked("acceptance_required")
        allowed = self.payload.get("allowed_paths")
        if (not isinstance(allowed, list) or not allowed
                or not all(_valid_path(item, pattern=True) for item in allowed)):
            raise _Blocked("allowed_paths_required")
        checks = self.payload.get("checks")
        if not isinstance(checks, list) or not checks:
            raise _Blocked("checks_required")
        names = []
        for check in checks:
            if (not isinstance(check, dict) or not _text(check.get("name"))
                    or not isinstance(check.get("argv"), list) or not check["argv"]
                    or not all(_text(part) for part in check["argv"])):
                raise _Blocked("invalid_check_command")
            names.append(check["name"])
        if len(set(names)) != len(names):
            raise _Blocked("duplicate_check_names")
        branch = self.payload.get("branch")
        if not _text(branch) or branch == self.config["base_branch"]:
            raise _Blocked("invalid_task_branch")
        for branch_name in (branch, self.config["base_branch"]):
            code, _ = self.git(["check-ref-format", "--branch", branch_name], optional=True)
            if code:
                raise _Blocked("invalid_git_branch")
        pr_url = self.ctx.get("pr_url") or self.payload.get("pr_url")
        if pr_url:
            parsed = urlsplit(pr_url)
            repository = urlsplit(self.config["repo_url"])
            if (parsed.scheme != "https" or parsed.netloc.casefold() != repository.netloc.casefold()
                    or not re.fullmatch(re.escape(repository.path.rstrip("/")) + r"/pull/[1-9][0-9]*", parsed.path)
                    or parsed.query or parsed.fragment):
                raise _Blocked("pr_repository_mismatch")
        if self.worktree.is_symlink():
            raise _Blocked("worktree_path_is_symlink")

    def reconcile_process(self) -> None:
        recorded = self.ctx.get("active_process")
        if not recorded:
            return
        confirmation = self.config.get("process_reconciliations", {}).get(self.task_id, {})
        date = _timestamp(confirmation.get("confirmed_at"))
        if (confirmation.get("pid") != recorded.get("pid")
                or confirmation.get("owner") != recorded.get("owner")
                or not _text(confirmation.get("confirmed_by"))
                or date is None or date > self.now()
                or date < recorded.get("started_at", 0)):
            raise _Blocked("awaiting_process_reconciliation")
        self.ctx.setdefault("process_reconciliations", []).append(
            {"process": recorded, "confirmation": confirmation})
        self.ctx.pop("active_process", None)
        self.save()

    def remote_sha(self, branch: str) -> str | None:
        code, raw = self.git(["ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"], optional=True)
        if code == 2:
            return None
        if code:
            raise _Blocked("remote_reference_unavailable")
        lines = [line.split() for line in raw.splitlines() if line.strip()]
        if (len(lines) != 1 or len(lines[0]) != 2 or not _SHA.fullmatch(lines[0][0])
                or lines[0][1] != f"refs/heads/{branch}"):
            raise _Blocked("remote_reference_unknown")
        return lines[0][0]

    def fetch(self, branch: str) -> str:
        remote = self.remote_sha(branch)
        if remote is None:
            raise _Blocked(f"remote_branch_missing:{branch}")
        self.git(["fetch", "--no-tags", "origin",
                  f"refs/heads/{branch}:refs/remotes/origin/{branch}"])
        _, actual = self.git(["rev-parse", "--verify", f"refs/remotes/origin/{branch}"])
        if actual != remote:
            raise _Blocked("remote_changed_during_fetch")
        return actual

    def prepare(self) -> None:
        self.head()  # An unborn repository cannot produce a reviewable PR.
        _, remote_url = self.git(["remote", "get-url", "origin"])
        if _repo(remote_url) != _repo(self.config["repo_url"]):
            raise _Blocked("origin_repository_mismatch")
        if self.ctx.get("prepare_intent"):
            if self.worktree.is_dir():
                self.verify_worktree()
                current = self.head(self.worktree)
                if current != self.ctx["prepare_intent"]["start_sha"]:
                    raise _Blocked("worktree_head_changed_during_prepare")
                self.ctx["local_head"] = current
                self.save("implement")
                return
            # Before worktree creation the branch must not have appeared.
        base_sha = self.fetch(self.config["base_branch"])
        branch = self.payload["branch"]
        existing_pr = bool(self.payload.get("pr_url"))
        remote_branch = self.remote_sha(branch)
        if existing_pr:
            if remote_branch is None:
                raise _Blocked("existing_pr_branch_missing")
            start_sha = self.fetch(branch)
        else:
            if remote_branch is not None:
                raise _Blocked("new_task_branch_already_exists_remotely")
            start_sha = base_sha
        code, local_branch = self.git(
            ["rev-parse", "--verify", f"refs/heads/{branch}"], optional=True)
        if code == 0 and (not existing_pr or local_branch != start_sha):
            raise _Blocked("local_task_branch_already_exists_or_diverged")
        if self.worktree.exists():
            raise _Blocked("unowned_worktree_path_exists")
        self.ctx.update({"base_sha": base_sha, "initial_head": start_sha,
                         "local_head": start_sha, "original_remote_head": remote_branch,
                         "worktree": str(self.worktree),
                         "prepare_intent": {"branch": branch, "start_sha": start_sha}})
        self.save()
        self.worktree.parent.mkdir(parents=True, exist_ok=True)
        if code == 0:
            self.git(["worktree", "add", str(self.worktree), branch])
        else:
            self.git(["worktree", "add", "--no-track", "-b", branch, str(self.worktree), start_sha])
        self.verify_worktree()
        self.save("implement")

    def verify_worktree(self) -> None:
        if not self.worktree.is_dir() or self.worktree.is_symlink():
            raise _Blocked("worktree_missing_or_unsafe")
        _, root = self.git(["rev-parse", "--show-toplevel"], cwd=self.worktree)
        _, branch = self.git(["branch", "--show-current"], cwd=self.worktree)
        if Path(root).resolve() != self.worktree.resolve() or branch != self.payload["branch"]:
            raise _Blocked("worktree_identity_mismatch")

    def protected_patterns(self) -> list[str]:
        extra = self.config.get("protected_paths", [])
        if not isinstance(extra, list) or not all(_valid_path(item, pattern=True) for item in extra):
            raise _Blocked("invalid_protected_paths")
        return [*_PROTECTED_PATTERNS, ".git", ".git/**", "**/.git", "**/.git/**",
                ".codex/**", "**/.codex/**", "controller/**", "**/controller/**", *extra]

    def protected(self, paths: list[str]) -> None:
        patterns = self.protected_patterns()
        for path in paths:
            if not _valid_path(path):
                raise _Blocked(f"unsafe_changed_path:{path}")
            if not any(fnmatchcase(path, item) for item in self.payload["allowed_paths"]):
                raise _Blocked(f"changed_path_outside_allowlist:{path}")
            if any(fnmatchcase(path.casefold(), item.casefold()) for item in patterns):
                raise _Blocked(f"protected_path_changed:{path}")

    def dirty(self) -> list[str]:
        _, raw = self.git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=self.worktree)
        return _status_paths(raw)

    def changed(self) -> list[str]:
        _, raw = self.git(["diff", "--name-only", "--no-renames", "-z",
                           f"{self.ctx['base_sha']}...HEAD"], cwd=self.worktree)
        paths = sorted(set(path for path in raw.split("\x00") if path))
        self.protected(paths)
        return paths

    def stable(self, *, clean: bool = False) -> None:
        self.verify_worktree()
        if self.head(self.worktree) != self.ctx["local_head"]:
            raise _Blocked("head_changed_outside_authorized_commit")
        paths = self.dirty()
        self.protected(paths)
        if clean and paths:
            raise _Blocked("committed_worktree_changed")
        self.protected(self.changed())

    def invalidate(self) -> None:
        for key in ("local_checks", "review", "review_input", "github_evidence", "merge_decision", "ci_wait"):
            self.ctx.pop(key, None)

    def model(self, role: str, prompt: str, schema: dict, sandbox: str) -> dict:
        calls = self.ctx.get("model_calls", 0)
        if calls >= self.limits["max_model_calls"]:
            raise _Blocked("model_call_budget_exhausted")
        self.ctx["model_calls"] = calls + 1
        self.save()  # Count before launch, including failed and interrupted calls.
        self.tick()
        self.ctx["command_sequence"] = self.ctx.get("command_sequence", 0) + 1
        directory = self.output_root / f"{self.ctx['command_sequence']:04d}-{role}"
        self.ctx["active_process"] = {"pid": None, "owner": self.owner,
                                      "kind": role, "output_dir": str(directory),
                                      "started_at": self.now()}
        self.save()
        try:
            receipt = self.codex_run(self.config.get("codex_path", "codex"), self.worktree,
                                     directory, prompt, schema, sandbox=sandbox,
                                     timeout=self.timeout(), tick=self.tick)
        finally:
            self.ctx.pop("active_process", None)
            self.save()
        result = receipt.get("result")
        if not isinstance(result, dict):
            raise _Blocked(f"invalid_{role}_result")
        self.ctx.setdefault("model_receipts", []).append(
            {"role": role, "directory": str(directory), "usage": receipt.get("usage")})
        self.save()
        return result

    def implement(self) -> None:
        self.stable()
        mode = self.config.get("implementation_mode", "workspace")
        if mode not in ("workspace", "patch"):
            raise _Blocked("invalid_implementation_mode")
        proposal = self.ctx.get("patch_proposal") if mode == "patch" else None
        recovering_proposal = (isinstance(proposal, dict)
                               and proposal.get("state") in ("pending", "applied"))
        if not recovering_proposal:
            if self.ctx.get("iterations", 0) >= self.limits["max_iterations"]:
                raise _Blocked("iteration_budget_exhausted")
            self.ctx["iterations"] = self.ctx.get("iterations", 0) + 1
            self.invalidate()
            self.ctx.pop("patch_proposal", None)
            self.save()
            inputs = {"goal": self.payload["goal"], "acceptance": self.payload["acceptance"],
                      "allowed_paths": self.payload["allowed_paths"], "checks": self.payload["checks"],
                      "feedback": self.ctx.get("feedback", [])}
            prompt = (
                "Implement this one authorized task in the current worktree. Read AGENTS.md. "
                "Do not commit, push, create/merge PRs, alter Git metadata, install global tools, "
                "change controller/CI/check configuration, weaken assertions, or edit outside allowed_paths. "
                "Stop and report blocked if requirements conflict or environment prerequisites are missing.\n"
            )
            if mode == "patch":
                from .patches import snapshot_files
                snapshots = snapshot_files(self.worktree, self.payload["allowed_paths"],
                                           self.protected_patterns())
                inputs["source_snapshots"] = snapshots
                prompt += (
                    "This is patch proposal mode: use read-only operations and return edits only. "
                    "Do not write files yourself. status=done means the proposal is ready, not applied. "
                    "For each edited file return its full UTF-8 content and the exact snapshot sha256; "
                    "use expected_sha256='' only for a genuinely new allowed file. No deletions.\n"
                )
                result = self.model("implement", prompt + json.dumps(inputs, ensure_ascii=False),
                                    PATCH_IMPLEMENT_SCHEMA, "read-only")
            else:
                result = self.model("implement", prompt + json.dumps(inputs, ensure_ascii=False),
                                    IMPLEMENT_SCHEMA, "workspace-write")
            self.stable()
            if result.get("status") == "blocked":
                raise _Blocked("implementation_blocked:" + str(result.get("reason", "unknown")))
            if result.get("status") != "done":
                raise _Blocked("invalid_implementation_result")
            if mode == "patch":
                if not isinstance(result.get("edits"), list):
                    raise _Blocked("invalid_patch_proposal")
                proposal = {"state": "pending", "created_at": self.now(), "model_result": result,
                            "snapshot_hashes": [{"path": item["path"], "sha256": item["sha256"]}
                                                for item in snapshots]}
                self.ctx["patch_proposal"] = proposal
                self.save()  # Durable intent precedes the controller's first write.
        else:
            result = proposal["model_result"]
        if mode == "patch":
            from .patches import apply_edits
            # Replaying a saved proposal is hash-checked and idempotent, so a
            # controller crash cannot cause a second model call or blind write.
            self.tick()
            try:
                receipts = apply_edits(self.worktree, result["edits"], self.payload["allowed_paths"],
                                       self.protected_patterns())
            except ValueError as exc:
                # Validation precedes all writes. Retain the rejected proposal
                # for audit; an explicit resume can request a fresh proposal.
                # IO failures remain pending for idempotent partial recovery.
                proposal["state"] = "rejected"
                proposal["rejection"] = str(exc)
                self.ctx["patch_proposal"] = proposal
                self.save()
                raise _Blocked(f"patch_proposal_rejected:{exc}") from exc
            if not isinstance(receipts, list):
                raise _Blocked("patch_application_result_unknown")
            proposal["state"] = "applied"
            proposal["application_receipts"] = receipts
            proposal["applied_at"] = self.now()
            self.ctx["patch_proposal"] = proposal
            self.save()
            self.stable()
        _, raw = self.git(["diff", "--binary", self.ctx["base_sha"], "--"], cwd=self.worktree)
        digest = hashlib.sha256(raw.encode("utf-8"))
        for path in self.dirty():
            file = self.worktree / path
            digest.update(path.encode("utf-8"))
            if file.is_symlink():
                digest.update(os.readlink(file).encode("utf-8"))
            elif file.is_file():
                with file.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
        fingerprint = digest.hexdigest()
        count = self.ctx.get("no_progress_count", 0) + 1 if fingerprint == self.ctx.get("fingerprint") else 0
        self.ctx.update({"fingerprint": fingerprint, "no_progress_count": count,
                         "implementation_summary": result.get("summary", "")})
        self.save()
        if count >= self.limits["no_progress_limit"]:
            raise _Blocked("no_progress_limit_reached")
        self.save("checks")

    def checks(self, *, committed: bool, cwd: Path | None = None) -> bool:
        cwd = cwd or self.worktree
        if cwd == self.worktree:
            self.stable(clean=committed)
        sha = self.head(cwd)
        results = []
        for check in self.payload["checks"]:
            receipt = self.invoke("check", self.check_run, check["argv"], cwd)
            code = receipt.get("returncode")
            if type(code) is not int:
                raise _Blocked("check_result_unknown")
            results.append({"name": check["name"], "status": "success" if code == 0 else "failure",
                            "executed": True, "returncode": code,
                            "stdout": receipt.get("stdout"), "stderr": receipt.get("stderr")})
        if self.head(cwd) != sha:
            raise _Blocked("head_changed_during_checks")
        if cwd == self.worktree:
            self.stable(clean=committed)
        envelope = {"head_sha": sha, "base_sha": self.ctx["base_sha"],
                    "observed_at": self.now(), "results": results}
        if cwd != self.worktree:
            self.ctx["post_merge_checks"] = envelope
        elif committed:
            self.ctx["local_checks"] = envelope
        else:
            self.ctx["precommit_checks"] = envelope
        self.save()
        failed = [item["name"] for item in results if item["status"] != "success"]
        if failed:
            self.ctx["feedback"] = ["Executed checks failed: " + ", ".join(failed),
                                    "Read their saved stdout/stderr receipts before repairing."]
            self.save()
            return False
        return True

    def commit(self) -> None:
        intent = self.ctx.get("commit_intent")
        current = self.head(self.worktree)
        if intent and current != intent["before_head"]:
            _, tree = self.git(["rev-parse", "HEAD^{tree}"], cwd=self.worktree)
            _, parent = self.git(["rev-parse", "HEAD^"], cwd=self.worktree)
            if tree != intent["tree_sha"] or parent != intent["before_head"]:
                raise _Blocked("uncertain_commit_requires_reconciliation")
            self.ctx["local_head"] = current
            self.ctx.pop("commit_intent", None)
            self.invalidate()
            self.save("checks_committed")
            return
        self.stable()
        paths = self.dirty()
        if not paths:
            if not self.changed():
                raise _Blocked("no_reviewable_change")
            self.save("checks_committed")  # Existing PR may only need revalidation.
            return
        self.grant("commit")
        self.git(["add", "--all", "--", *paths], cwd=self.worktree)
        _, staged = self.git(["diff", "--cached", "--name-only", "--no-renames", "-z"], cwd=self.worktree)
        self.protected([path for path in staged.split("\x00") if path])
        _, tree = self.git(["write-tree"], cwd=self.worktree)
        if not _SHA.fullmatch(tree):
            raise _Blocked("staged_tree_unknown")
        self.ctx["commit_intent"] = {"before_head": current, "tree_sha": tree}
        self.save()
        self.git(["commit", "-m", f"Complete workflow task {self.task_id}"], cwd=self.worktree)
        new_head = self.head(self.worktree)
        _, committed_tree = self.git(["rev-parse", "HEAD^{tree}"], cwd=self.worktree)
        _, parent = self.git(["rev-parse", "HEAD^"], cwd=self.worktree)
        if new_head == current or committed_tree != tree or parent != current:
            raise _Blocked("commit_does_not_match_intent")
        self.ctx["local_head"] = new_head
        self.ctx.pop("commit_intent", None)
        self.invalidate()
        self.save("checks_committed")

    def review(self) -> None:
        self.stable(clean=True)
        paths = self.changed()
        if not paths:
            raise _Blocked("no_reviewable_change")
        checks = self.ctx.get("local_checks", {})
        if checks.get("head_sha") != self.ctx["local_head"] or checks.get("base_sha") != self.ctx["base_sha"]:
            self.save("checks_committed")
            return
        from .review_evidence import review_inputs
        _, diff = self.git(["diff", "--binary", "--no-ext-diff", "--no-textconv",
                            f"{self.ctx['base_sha']}...HEAD", "--"], cwd=self.worktree)
        bundle = review_inputs(diff, checks, self.output_root)
        input_path = self.output_root / f"review-input-{self.ctx['local_head']}-{self.ctx['command_sequence']}.json"
        input_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        self.ctx["review_input"] = {"path": str(input_path), "diff_sha256": bundle["diff_sha256"],
                                    "head_sha": self.ctx["local_head"], "base_sha": self.ctx["base_sha"]}
        self.save()
        prompt = (
            "Independently review this task using the actual diff, source, tests, and saved check evidence. "
            "Use read-only operations. Do not accept the implementer's summary as proof. "
            "Report requirement gaps, correctness failures, removed/weakened tests, or scope violations. "
            "Use verdict blocked for missing environment/evidence or ambiguous requirements.\n"
            + json.dumps({"goal": self.payload["goal"], "acceptance": self.payload["acceptance"],
                          "head_sha": self.ctx["local_head"], "base_sha": self.ctx["base_sha"],
                          "changed_files": paths, "local_checks": checks,
                          "actual_evidence": bundle,
                          "diff_command": ["git", "diff", f"{self.ctx['base_sha']}...HEAD"]},
                         ensure_ascii=False)
        )
        result = self.model("review", prompt, REVIEW_SCHEMA, "read-only")
        self.stable(clean=True)
        verdict, count = result.get("verdict"), result.get("blocking_findings")
        findings = result.get("findings")
        if (verdict not in ("pass", "fail", "blocked") or type(count) is not int or count < 0
                or not isinstance(findings, list) or not all(isinstance(item, str) for item in findings)
                or not isinstance(result.get("reason"), str)):
            raise _Blocked("invalid_review_result")
        self.ctx["review"] = {**result, "head_sha": self.ctx["local_head"],
                               "base_sha": self.ctx["base_sha"], "observed_at": self.now(),
                               "input_path": str(input_path), "diff_sha256": bundle["diff_sha256"]}
        self.save()
        if verdict == "blocked":
            raise _Blocked("review_blocked:" + result["reason"])
        if verdict != "pass" or count != 0:
            self.ctx["feedback"] = findings or [result["reason"]]
            self.ctx.pop("patch_proposal", None)
            self.save("implement")
            return
        self.save("push")

    def base_current(self) -> None:
        remote = self.remote_sha(self.config["base_branch"])
        if remote != self.ctx["base_sha"]:
            self.invalidate()
            self.ctx["observed_new_base_sha"] = remote
            self.save()
            raise _Blocked("base_changed_revalidation_required")

    def push(self) -> None:
        self.stable(clean=True)
        self.base_current()
        head, branch = self.ctx["local_head"], self.payload["branch"]
        remote = self.remote_sha(branch)
        if remote != head:
            self.grant("push")
            expected_remote = self.ctx.get("pushed_head", self.ctx.get("original_remote_head"))
            if remote != expected_remote:
                raise _Blocked("remote_task_branch_changed")
            if remote:
                code, _ = self.git(["merge-base", "--is-ancestor", remote, head],
                                   cwd=self.worktree, optional=True)
                if code:
                    raise _Blocked("push_would_not_be_fast_forward")
            self.ctx["push_intent"] = {"head_sha": head, "branch": branch}
            self.save()
            self.git(["push", "origin", f"{head}:refs/heads/{branch}"], cwd=self.worktree)
            if self.remote_sha(branch) != head:
                raise _Blocked("push_result_unconfirmed")
        self.ctx["pushed_head"] = head
        self.ctx.pop("push_intent", None)
        self.save("pr")

    def browser_task(self) -> dict:
        return {"id": self.task_id, "repo_url": self.config["repo_url"],
                "base_branch": self.config["base_branch"], "branch": self.payload["branch"],
                "pr_url": self.ctx.get("pr_url") or self.payload.get("pr_url"),
                "goal": self.payload["goal"], "acceptance": self.payload["acceptance"]}

    def browser_call(self, method: str, *args: object) -> dict:
        if self.browser is None or not hasattr(self.browser, method):
            raise _Blocked("browser_unavailable")
        # The Chrome adapter starts a fresh Codex session too. Count it here,
        # in the same durable budget as implementation and independent review.
        calls = self.ctx.get("model_calls", 0)
        if calls >= self.limits["max_model_calls"]:
            raise _Blocked("model_call_budget_exhausted")
        self.ctx["model_calls"] = calls + 1
        self.save()
        self.tick()
        self.ctx["command_sequence"] = self.ctx.get("command_sequence", 0) + 1
        directory = self.output_root / f"{self.ctx['command_sequence']:04d}-browser-{method}"
        self.ctx["active_process"] = {"pid": None, "owner": self.owner,
                                      "kind": f"browser-{method}", "output_dir": str(directory),
                                      "started_at": self.now()}
        self.save()
        try:
            result = getattr(self.browser, method)(self.browser_task(), *args, directory,
                                                  self.tick, self.timeout())
        finally:
            self.ctx.pop("active_process", None)
            self.save()
        if not isinstance(result, dict):
            raise _Blocked("browser_result_unknown")
        return result

    def pr(self) -> None:
        self.stable(clean=True)
        self.base_current()
        if not self.browser_task()["pr_url"]:
            self.grant("create_pr")
        self.ctx["pr_intent"] = {"branch": self.payload["branch"], "head_sha": self.ctx["local_head"]}
        self.save()
        result = self.browser_call("ensure_pr", self.ctx["local_head"], self.ctx["base_sha"])
        if not _text(result.get("url")):
            raise _Blocked("pr_creation_unconfirmed")
        self.ctx["pr_url"] = result["url"]
        self.ctx.pop("pr_intent", None)
        self.validate()
        self.save("observe")

    def merged(self, evidence: dict) -> str | None:
        pr = evidence.get("pr", {})
        sha = evidence.get("merge_sha") or pr.get("merge_sha")
        trusted_url = self.browser_task()["pr_url"]
        if (pr.get("url") == trusted_url and evidence.get("head_sha") == self.ctx["local_head"]
                and (pr.get("state") == "merged" or evidence.get("status") == "merged")
                and isinstance(sha, str) and _SHA.fullmatch(sha)):
            return sha
        return None

    def ci_waiting_checks(self, evidence: dict, decision: dict) -> list[dict] | None:
        """Wait only when required CI waiting states are the *only* denial."""
        pr = evidence.get("pr")
        if (not isinstance(pr, dict) or evidence.get("head_sha") != self.ctx["local_head"]
                or evidence.get("base_sha") != self.ctx["base_sha"]
                or pr.get("url") != self.browser_task()["pr_url"]):
            return None
        required = self.config.get("merge_policy", {}).get("required_checks")
        checks = evidence.get("github_checks")
        if not isinstance(checks, dict):
            return None
        results = checks.get("results")
        if (not isinstance(required, list) or not required
                or not all(_text(name) for name in required) or len(set(required)) != len(required)
                or checks.get("head_sha") != self.ctx["local_head"]
                or checks.get("base_sha") != self.ctx["base_sha"]
                or not isinstance(results, list)):
            return None
        statuses, waiting_reasons = [], []
        waiting = {"queued", "in_progress", "pending"}
        for name in required:
            matches = [item for item in results if isinstance(item, dict) and item.get("name") == name]
            if len(matches) != 1 or matches[0].get("status") not in waiting | {"success"}:
                return None
            status = matches[0]["status"]
            statuses.append({"name": name, "status": status})
            if status in waiting:
                waiting_reasons.append(f"github_checks required check is not success: {name}")
        reasons = decision.get("reasons")
        if (not waiting_reasons or decision.get("eligible") is not False
                or not isinstance(reasons, list) or len(reasons) != len(waiting_reasons)
                or set(reasons) != set(waiting_reasons)):
            return None
        return statuses

    def schedule_ci_poll(self, statuses: list[dict]) -> None:
        """Reserve an extra observation durably before any waiting begins."""
        count = self.ctx.get("ci_poll_count", 0)
        if type(count) is not int or count < 0:
            raise _Blocked("invalid_ci_poll_count")
        if count >= self.limits["max_ci_polls"]:
            previous = self.ctx.get("ci_wait", {})
            self.ctx["ci_wait"] = {**previous, "state": "limit_exhausted", "statuses": statuses,
                                   "head_sha": self.ctx["local_head"], "base_sha": self.ctx["base_sha"],
                                   "pr_url": self.browser_task()["pr_url"]}
            self.save("observe")
            raise _Blocked("ci_wait_limit_exhausted")
        if self.ctx.get("model_calls", 0) >= self.limits["max_model_calls"]:
            raise _Blocked("model_call_budget_exhausted")
        self.tick()
        now = self.now()
        self.ctx["ci_poll_count"] = count + 1
        self.ctx["ci_wait"] = {"state": "scheduled", "poll_number": count + 1,
                               "head_sha": self.ctx["local_head"], "base_sha": self.ctx["base_sha"],
                               "pr_url": self.browser_task()["pr_url"], "statuses": statuses,
                               "scheduled_at": now,
                               "next_poll_at": now + self.limits["ci_poll_interval_seconds"]}
        self.save("ci_wait")

    def wait_ci(self) -> None:
        record = self.ctx.get("ci_wait", {})
        if (record.get("state") != "scheduled"
                or record.get("head_sha") != self.ctx["local_head"]
                or record.get("base_sha") != self.ctx["base_sha"]
                or record.get("pr_url") != self.browser_task()["pr_url"]
                or _timestamp(record.get("next_poll_at")) is None):
            raise _Blocked("ci_wait_checkpoint_invalid")
        if self.ctx.get("model_calls", 0) >= self.limits["max_model_calls"]:
            raise _Blocked("model_call_budget_exhausted")
        wait_started = time.monotonic()
        wait_remaining = max(0.0, record["next_poll_at"] - self.now())
        while True:
            self.tick()
            remaining = min(record["next_poll_at"] - self.now(),
                            wait_remaining - (time.monotonic() - wait_started))
            if remaining <= 0:
                break
            # Small interruptible segments retain the lease and respond to
            # pause/deadline without occupying a long blocking sleep.
            time.sleep(min(1.0, remaining, self.timeout()))
        self.stable(clean=True)
        record["state"] = "reading"
        self.ctx["ci_wait"] = record
        self.save("observe")
        self.observe(reserved_ci_poll=True)

    def observe(self, *, reserved_ci_poll: bool = False) -> None:
        self.stable(clean=True)
        record = self.ctx.get("ci_wait")
        if not reserved_ci_poll and isinstance(record, dict) and record.get("state") != "resolved":
            if (record.get("state") == "scheduled"
                    and record.get("head_sha") == self.ctx["local_head"]
                    and record.get("base_sha") == self.ctx["base_sha"]
                    and record.get("pr_url") == self.browser_task()["pr_url"]):
                self.save("ci_wait")
                return
            # The prior reserved read may already have run before interruption.
            # Its reservation is never reused to obtain an uncounted extra read.
            saved = self.ctx.get("github_evidence", {})
            decision = evaluate_merge(self.config.get("merge_policy", {}), saved, now=self.now())
            statuses = self.ci_waiting_checks(saved, decision)
            if statuses is not None:
                self.schedule_ci_poll(statuses)
                return
            raise _Blocked("merge_gates_unsatisfied:" + "; ".join(decision["reasons"]))
        evidence = self.browser_call("observe", self.ctx["local_head"], self.ctx["base_sha"])
        merged_sha = self.merged(evidence)
        if merged_sha:
            if isinstance(self.ctx.get("ci_wait"), dict):
                self.ctx["ci_wait"]["state"] = "resolved"
                self.ctx["ci_wait"]["resolved_at"] = self.now()
            self.ctx["merge_receipt"] = {"status": "merged", "merge_sha": merged_sha,
                                         "pr_url": self.browser_task()["pr_url"],
                                         "observation": evidence}
            self.save("post_merge")
            return
        self.base_current()
        # Controller-owned bindings are never taken from model/browser output.
        for name, expected in (("head_sha", self.ctx["local_head"]), ("base_sha", self.ctx["base_sha"])):
            if evidence.get(name) != expected:
                raise _Blocked("browser_observation_sha_mismatch")
        evidence = {**evidence, "repo_url": self.config["repo_url"],
                    "base_branch": self.config["base_branch"],
                    "trusted_pr_url": self.browser_task()["pr_url"],
                    "changed_files": self.changed(), "local_checks": self.ctx.get("local_checks"),
                    "review": self.ctx.get("review")}
        self.ctx["github_evidence"] = evidence
        decision = evaluate_merge(self.config.get("merge_policy", {}), evidence, now=self.now())
        self.ctx["merge_decision"] = decision
        self.save()
        if not decision["eligible"]:
            feedback = evidence.get("feedback", [])
            failure = evidence.get("failure_evidence", {})
            failure_url = urlsplit(failure.get("url", "")) if isinstance(failure, dict) else None
            repository_url = urlsplit(self.config["repo_url"])
            same_repository = (failure_url is not None and failure_url.scheme == "https"
                               and failure_url.netloc.casefold() == repository_url.netloc.casefold()
                               and failure_url.path.startswith(repository_url.path.rstrip("/") + "/"))
            if (evidence.get("failure_kind") == "code" and isinstance(feedback, list)
                    and feedback and all(isinstance(item, str) for item in feedback)
                    and isinstance(failure, dict)
                    and failure.get("head_sha") == self.ctx["local_head"]
                    and failure.get("base_sha") == self.ctx["base_sha"]
                    and same_repository):
                self.ctx["feedback"] = feedback
                self.ctx.pop("patch_proposal", None)
                self.save("implement")
                return
            statuses = self.ci_waiting_checks(evidence, decision)
            if statuses is not None:
                self.schedule_ci_poll(statuses)
                return
            raise _Blocked("merge_gates_unsatisfied:" + "; ".join(decision["reasons"]))
        if isinstance(self.ctx.get("ci_wait"), dict):
            self.ctx["ci_wait"]["state"] = "resolved"
            self.ctx["ci_wait"]["resolved_at"] = self.now()
        self.save("merge")

    def merge(self) -> None:
        self.stable(clean=True)
        # Re-observe on every attempt, including resuming an uncertain merge.
        self.save("observe")
        self.observe()
        if self.phase != "merge":
            return
        self.grant("merge")
        self.base_current()
        self.ctx["merge_intent"] = {"head_sha": self.ctx["local_head"],
                                     "base_sha": self.ctx["base_sha"],
                                     "pr_url": self.browser_task()["pr_url"]}
        self.save()
        result = self.browser_call("merge", self.ctx["github_evidence"])
        self.ctx["merge_attempt_receipt"] = result
        self.save("observe")
        # A click/adapter receipt alone never establishes successful merge.
        observed = self.browser_call("observe", self.ctx["local_head"], self.ctx["base_sha"])
        sha = self.merged(observed)
        if not sha:
            raise _Blocked("merge_result_unconfirmed:" + str(result.get("reason", "unknown")))
        self.ctx["merge_receipt"] = {"status": "merged", "merge_sha": sha,
                                     "pr_url": self.browser_task()["pr_url"], "observation": observed}
        self.ctx.pop("merge_intent", None)
        self.save("post_merge")

    def post_merge(self) -> None:
        receipt = self.ctx.get("merge_receipt", {})
        merged_sha = receipt.get("merge_sha")
        if receipt.get("status") != "merged" or not isinstance(merged_sha, str) or not _SHA.fullmatch(merged_sha):
            raise _Blocked("merge_confirmation_required")
        current_base = self.fetch(self.config["base_branch"])
        code, _ = self.git(["merge-base", "--is-ancestor", merged_sha, current_base], optional=True)
        if code:
            raise _Blocked("merge_not_present_on_current_base")
        path = self.runtime / "post-merge" / self.worktree.name
        if path.is_symlink():
            raise _Blocked("post_merge_worktree_unsafe")
        if path.exists():
            actual = self.head(path)
            if actual != current_base:
                raise _Blocked("post_merge_worktree_requires_reconciliation")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.git(["worktree", "add", "--detach", str(path), current_base])
        self.post_merge_clean(path, current_base)
        self.ctx["post_merge_base_sha"] = current_base
        self.save()
        if not self.checks(committed=True, cwd=path):
            raise _Blocked("post_merge_checks_failed")
        self.post_merge_clean(path, current_base)
        if self.remote_sha(self.config["base_branch"]) != current_base:
            raise _Blocked("post_merge_base_changed_revalidation_required")
        self.ctx["completion"] = {"merged": True, "main_checks_passed": True,
                                  "user_accepted": None, "product_usage_verified": None}
        self.save("complete")

    def post_merge_clean(self, path: Path, expected_sha: str) -> None:
        """Only a clean, identified detached worktree proves main checks."""
        if not path.is_dir() or path.is_symlink():
            raise _Blocked("post_merge_worktree_unsafe")
        _, root = self.git(["rev-parse", "--show-toplevel"], cwd=path)
        _, branch = self.git(["branch", "--show-current"], cwd=path)
        if Path(root).resolve() != path.resolve() or branch:
            raise _Blocked("post_merge_worktree_identity_mismatch")
        if self.head(path) != expected_sha:
            raise _Blocked("post_merge_head_changed")
        _, raw = self.git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=path)
        if _status_paths(raw):
            raise _Blocked("post_merge_worktree_not_clean")

    def run(self) -> dict:
        for key, default in (("model_calls", 0), ("iterations", 0), ("command_sequence", 0),
                             ("ci_poll_count", 0)):
            self.ctx.setdefault(key, default)
        # Deadlines survive pause, block, and restart; resuming does not mint budget.
        self.ctx.setdefault("started_at", self.now())
        self.ctx.setdefault("deadline", self.ctx["started_at"] + float(self.limits["max_seconds"]))
        self.remaining_at_start = self.ctx["deadline"] - self.now()
        self.save()
        self.reconcile_process()
        self.tick()
        self.validate()
        if self.phase != "prepare":
            self.verify_worktree()
        while True:
            self.tick()
            if self.phase == "prepare":
                self.prepare()
            elif self.phase == "implement":
                self.implement()
            elif self.phase in ("checks", "checks_committed"):
                committed = self.phase == "checks_committed"
                if not self.checks(committed=committed):
                    self.ctx.pop("patch_proposal", None)
                    self.save("implement")
                else:
                    self.save("review" if committed else "commit")
            elif self.phase == "commit":
                self.commit()
            elif self.phase == "review":
                self.review()
            elif self.phase == "push":
                self.push()
            elif self.phase == "pr":
                self.pr()
            elif self.phase == "observe":
                self.observe()
            elif self.phase == "ci_wait":
                self.wait_ci()
            elif self.phase == "merge":
                self.merge()
            elif self.phase == "post_merge":
                self.post_merge()
            elif self.phase == "complete":
                return self.store.finish(self.task_id, self.owner, "completed",
                                         "Chrome confirmed merge; current base checks passed")
            else:
                raise _Blocked(f"unknown_phase:{self.phase}")


def run_once(config: dict, store: StateStore, *, browser=None,
             codex_run=run_codex, check_run=run_process) -> dict:
    """Claim at most one task, run its bounded loop, and persist the outcome.

    Authorization is read only from config.grants. No model result grants a
    remote operation. Production execution requires the real Chrome adapter;
    injected callbacks test controller transitions and are not E2E evidence.
    """
    owner = str(uuid.uuid4())
    task = store.claim(owner, lease_seconds=60)
    if task is None:
        return {"status": "idle", "reason": "no_claimable_task_or_worker_already_running"}
    try:
        runner = _Runner(config, store, task, browser, codex_run, check_run)
    except (OSError, ValueError, TypeError) as exc:
        return store.finish(task["id"], owner, "blocked",
                            f"invalid_controller_config:{type(exc).__name__}:{exc}")
    try:
        return runner.run()
    except LeaseLostError:
        # A replacement owner is authoritative; never overwrite its state.
        return {"status": "lease_lost", "id": task["id"], "phase": runner.phase}
    except ProcessStopped as exc:
        status = "paused" if str(exc) == "pause_requested" else "blocked"
        reason = str(exc)
    except _Blocked as exc:
        status, reason = "blocked", str(exc)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        status, reason = "blocked", f"environment_or_controller_error:{type(exc).__name__}:{exc}"
    try:
        runner.ctx["last_stop"] = {"phase": runner.phase, "reason": reason, "at": runner.now()}
        runner.save()
        return store.finish(task["id"], owner, status, reason)
    except LeaseLostError:
        return {"status": "lease_lost", "id": task["id"], "phase": runner.phase, "reason": reason}
