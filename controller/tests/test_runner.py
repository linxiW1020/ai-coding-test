"""Controller tests with fake child processes; these do not prove real E2E.

No test invokes Codex, a browser, a remote service, or git commit. The temporary
directories contain no real git repository; FakeCommands models exact argv.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import tempfile
import unittest

from workflow.runner import PATCH_IMPLEMENT_SCHEMA, run_once
from workflow.state import StateStore


BASE, HEAD, TREE, MERGE, OTHER = (char * 40 for char in "abcde")
REPO_URL = "https://github.com/example/pilot"
PR_URL = REPO_URL + "/pull/1"
BRANCH = "task/one"


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class FakeCommands:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.bootstrap = True
        self.remote = {"main": BASE}
        self.local_branches: dict[str, str] = {}
        self.parents: dict[str, str] = {}
        self.commit_number = 0
        self.heads = {str(repo): BASE}
        self.branches: dict[str, str] = {}
        self.dirty_paths: list[str] = []
        self.status_override: str | None = None
        self.calls: list[tuple[list[str], str]] = []
        self.check_codes: list[int] = []
        self.check_number = 0
        self.after_check = None
        self.pause_on_next = None
        self.push_uncertain_once = False
        self.post_merge_dirty = False
        self.post_merge_attached = False
        self.seed_files: dict[str, str] = {}

    def __call__(self, argv, cwd, output_dir, *, timeout, tick, **kwargs):
        cwd = Path(cwd)
        self.calls.append((list(argv), str(cwd)))
        if self.pause_on_next:
            callback, self.pause_on_next = self.pause_on_next, None
            callback()
        tick(1234)
        output, code = "", 0
        if argv[0] != "git":
            self.check_number += 1
            code = self.check_codes.pop(0) if self.check_codes else 0
            if self.after_check:
                self.after_check(self.check_number, cwd)
        else:
            args = argv[1:]
            action = args[0]
            if action == "check-ref-format":
                output = args[-1]
            elif args[:3] == ["remote", "get-url", "origin"]:
                output = REPO_URL
            elif action == "ls-remote":
                branch = args[-1].removeprefix("refs/heads/")
                if branch in self.remote:
                    output = self.remote[branch] + "\trefs/heads/" + branch + "\n"
                else:
                    code = 2
            elif action == "fetch":
                pass
            elif action == "rev-parse":
                ref = args[-1]
                if ref == "--show-toplevel":
                    output = str(cwd)
                elif ref == "HEAD":
                    if cwd == self.repo and not self.bootstrap:
                        code = 128
                    else:
                        output = self.heads.get(str(cwd), BASE)
                elif ref == "HEAD^{tree}":
                    output = TREE
                elif ref == "HEAD^":
                    output = self.parents.get(self.heads.get(str(cwd), BASE), BASE)
                elif ref.startswith("refs/remotes/origin/"):
                    output = self.remote[ref.removeprefix("refs/remotes/origin/")]
                elif ref.startswith("refs/heads/"):
                    branch = ref.removeprefix("refs/heads/")
                    if branch in self.local_branches:
                        output = self.local_branches[branch]
                    else:
                        code = 128
                else:
                    raise AssertionError(f"unhandled rev-parse: {argv}")
            elif action == "worktree":
                if "--detach" in args:
                    path, sha = Path(args[-2]), args[-1]
                    branch = ""
                elif "-b" in args:
                    branch, path, sha = args[-3], Path(args[-2]), args[-1]
                    self.local_branches[branch] = sha
                else:
                    path, branch = Path(args[-2]), args[-1]
                    sha = self.local_branches[branch]
                path.mkdir(parents=True)
                self.heads[str(path)], self.branches[str(path)] = sha, branch
                for relative, content in self.seed_files.items():
                    file = path / relative
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(content, encoding="utf-8")
            elif action == "branch":
                output = self.branches.get(str(cwd), BRANCH)
                if self.post_merge_attached and "post-merge" in str(cwd):
                    output = "main"
            elif action == "status":
                if "post-merge" in str(cwd):
                    output = "?? contamination.txt\x00" if self.post_merge_dirty else ""
                else:
                    dirty = set(self.dirty_paths)
                    dirty.update(path for path, content in self.seed_files.items()
                                 if (cwd / path).read_text(encoding="utf-8") != content)
                    output = self.status_override if self.status_override is not None else "".join(
                        " M " + path + "\x00" for path in sorted(dirty))
            elif action == "diff":
                if "--cached" in args:
                    output = "".join(path + "\x00" for path in self.dirty_paths)
                elif "--name-only" in args:
                    output = "src/value.py\x00" if self.heads.get(str(cwd), BASE) != BASE else ""
                elif "--binary" in args:
                    output = "diff of the task\n" if self.dirty_paths or self.heads.get(str(cwd), BASE) != BASE else ""
                else:
                    raise AssertionError(f"unhandled diff: {argv}")
            elif action == "add":
                pass
            elif action == "write-tree":
                output = TREE
            elif action == "commit":
                self.commit_number += 1
                new_head = HEAD if self.commit_number == 1 else "f" * 40
                self.parents[new_head] = self.heads[str(cwd)]
                self.heads[str(cwd)] = new_head
                self.local_branches[BRANCH] = new_head
                self.dirty_paths = []
            elif action == "merge-base":
                pass
            elif action == "push":
                sha, branch = args[-1].split(":refs/heads/")
                self.remote[branch] = sha
                if self.push_uncertain_once:
                    self.push_uncertain_once = False
                    raise RuntimeError("connection lost after push")
            else:
                raise AssertionError(f"unhandled fake command: {argv}")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "stdout.log").write_bytes(output.encode("utf-8"))
        (output_dir / "stderr.log").write_bytes(b"")
        return {"returncode": code, "stdout_text": output, "stdout": str(output_dir / "stdout.log"),
                "stderr": str(output_dir / "stderr.log")}


class FakeCodex:
    def __init__(self, commands: FakeCommands) -> None:
        self.commands = commands
        self.calls = []
        self.implementation_status = "done"
        self.review_verdict = "pass"
        self.after_review = None
        self.change_each_time = True
        self.implementations = 0

    def __call__(self, codex, cwd, output_dir, prompt, schema, *, sandbox, timeout, tick):
        tick(4567)
        self.calls.append({"sandbox": sandbox, "prompt": prompt})
        if sandbox == "workspace-write":
            self.implementations += 1
            file = cwd / "src" / "value.py"
            file.parent.mkdir(exist_ok=True)
            file.write_text(str(self.implementations if self.change_each_time else 1), encoding="utf-8")
            self.commands.dirty_paths = ["src/value.py"]
            result = {"status": self.implementation_status, "summary": "implemented", "reason": "test"}
        else:
            if self.after_review:
                self.after_review()
            result = {"verdict": self.review_verdict,
                      "blocking_findings": 0 if self.review_verdict == "pass" else 1,
                      "findings": [] if self.review_verdict == "pass" else ["repair needed"],
                      "reason": "review test"}
        return {"result": result, "usage": [{"input_tokens": 1, "output_tokens": 1}]}


class FakeBrowser:
    def __init__(self, commands: FakeCommands, clock: Clock) -> None:
        self.commands, self.clock = commands, clock
        self.calls: list[str] = []
        self.did_merge = False
        self.uncertain_merge_once = False
        self.confirm_merge = True
        self.observation_head = None
        self.observation_url = PR_URL
        self.code_feedback_once = False
        self.code_feedback_at = None
        self.code_feedback_status = "failure"
        self.stale_failure_evidence = False
        self.check_status_sequence = []
        self.default_check_status = "success"
        self.evidence_overrides = {}
        self.observation_exception_at = None

    def ensure_pr(self, task, head_sha, base_sha, output_dir, tick, timeout):
        self.calls.append("ensure_pr")
        tick(5678)
        return {"url": PR_URL}

    def observe(self, task, head_sha, base_sha, output_dir, tick, timeout):
        self.calls.append("observe")
        tick(5678)
        if self.observation_exception_at == self.calls.count("observe"):
            self.observation_exception_at = None
            raise RuntimeError("simulated Chrome observation failure")
        observed = self.clock()
        head = self.observation_head or head_sha
        evidence = {"repo_url": REPO_URL, "head_sha": head, "base_sha": base_sha,
                "pr": {"url": self.observation_url, "number": 1,
                       "state": "merged" if self.did_merge and self.confirm_merge else "open",
                       "draft": False, "mergeable": True},
                "merge_sha": MERGE if self.did_merge and self.confirm_merge else None,
                "github_checks": {"head_sha": head, "base_sha": base_sha,
                                  "observed_at": observed,
                                  "results": [{"name": "unit", "status": "success"}]},
                "browser_receipt": {"head_sha": head, "base_sha": base_sha,
                                    "observed_at": observed, "url": self.observation_url},
                "branch_rules_observed": True, "required_reviews_satisfied": True,
                "bypass": False, "feedback": []}
        status = self.check_status_sequence.pop(0) if self.check_status_sequence else self.default_check_status
        if status == "missing":
            evidence["github_checks"]["results"] = []
        elif status == "duplicate":
            evidence["github_checks"]["results"] = [{"name": "unit", "status": "pending"}] * 2
        else:
            evidence["github_checks"]["results"][0]["status"] = status
        if self.code_feedback_at == self.calls.count("observe"):
            self.code_feedback_once = True
            self.code_feedback_at = None
        if self.code_feedback_once:
            self.code_feedback_once = False
            evidence["github_checks"]["results"][0]["status"] = self.code_feedback_status
            evidence["failure_kind"] = "code"
            evidence["feedback"] = ["CI found a code error; inspect its saved log"]
            evidence["failure_evidence"] = {"head_sha": OTHER if self.stale_failure_evidence else head,
                                            "base_sha": base_sha, "url": PR_URL + "/checks"}
        evidence.update(self.evidence_overrides)
        return evidence

    def merge(self, task, evidence, output_dir, tick, timeout):
        self.calls.append("merge")
        tick(5678)
        self.did_merge = True
        self.commands.remote["main"] = MERGE
        if self.uncertain_merge_once:
            self.uncertain_merge_once = False
            raise RuntimeError("connection lost after merge click")
        return {"status": "merged", "merge_sha": MERGE, "pr_url": PR_URL, "reason": "clicked"}


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.clock = Clock()
        self.store = StateStore(self.root / "state.sqlite3", clock=self.clock)
        self.commands = FakeCommands(self.repo)
        self.codex = FakeCodex(self.commands)
        self.browser = FakeBrowser(self.commands, self.clock)
        self.config = {
            "repo_path": str(self.repo), "repo_url": REPO_URL, "base_branch": "main",
            "runtime_dir": str(self.root / "runtime"), "codex_path": "unused-fake",
            "grants": {"repo_url": REPO_URL, "approved_by": "operator", "approved_at": 999, "task_ids": ["one"],
                       "commit": True, "push": True, "create_pr": True, "merge": True},
            "merge_policy": {"enabled": True, "repo_url": REPO_URL, "base_branch": "main",
                             "approved_by": "operator", "approved_at": 999,
                             "allowed_paths": ["src/**"], "required_checks": ["unit"],
                             "max_changed_files": 2},
        }
        self.payload = {"goal": "Implement one value", "acceptance": ["unit passes"],
                        "allowed_paths": ["src/**"], "checks": [{"name": "unit", "argv": ["fake-check"]}],
                        "branch": BRANCH, "pr_url": None, "issue_url": None}
        self.store.enqueue("one", self.payload, "trigger:one")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_task(self, *, browser=True) -> dict:
        return run_once(self.config, self.store, browser=self.browser if browser else None,
                        codex_run=self.codex, check_run=self.commands)

    def count_git(self, action: str) -> int:
        return sum(argv[:2] == ["git", action] for argv, _ in self.commands.calls)

    def ci_wait_limits(self, *, polls=3, interval=1, model_calls=12):
        self.config["limits"] = {"max_ci_polls": polls, "ci_poll_interval_seconds": interval,
                                 "max_model_calls": model_calls}

    def advancing_sleep(self, seconds):
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, 1)
        checkpoint = self.store.get("one")
        self.assertEqual(checkpoint["phase"], "ci_wait")
        self.assertGreater(checkpoint["context"]["ci_wait"]["next_poll_at"], self.clock())
        self.clock.value += seconds

    def test_ci_wait_queued_running_pending_then_success_completes_automatically(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits()
        self.browser.check_status_sequence = ["queued", "in_progress", "pending", "success"]
        reservations = []

        def sleep(seconds):
            current = self.store.get("one")["context"]
            item = (current["ci_poll_count"], current["ci_wait"]["statuses"][0]["status"])
            if not reservations or reservations[-1] != item:
                reservations.append(item)
            self.advancing_sleep(seconds)

        with patch("workflow.runner.time.sleep", side_effect=sleep):
            result = self.run_task()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["context"]["ci_poll_count"], 3)
        self.assertEqual(reservations, [(1, "queued"), (2, "in_progress"), (3, "pending")])
        self.assertEqual(result["context"]["ci_wait"]["state"], "resolved")
        self.assertEqual(result["context"]["model_calls"], 10)
        self.assertEqual(self.codex.implementations, 1)
        self.assertEqual(self.browser.calls.count("merge"), 1)

    def test_ci_wait_permanent_pending_is_bounded_and_resume_cannot_get_extra_read(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits(polls=2)
        self.browser.default_check_status = "pending"
        with patch("workflow.runner.time.sleep", side_effect=self.advancing_sleep):
            result = self.run_task()
            self.assertEqual(result["reason"], "ci_wait_limit_exhausted")
            self.assertEqual(result["context"]["ci_poll_count"], 2)
            self.assertEqual(self.browser.calls.count("observe"), 3)
            calls, deadline = result["context"]["model_calls"], result["context"]["deadline"]
            self.store.resume("one")
            again = self.run_task()
        self.assertEqual(again["reason"], "ci_wait_limit_exhausted")
        self.assertEqual(again["context"]["ci_poll_count"], 2)
        self.assertEqual(again["context"]["model_calls"], calls)
        self.assertEqual(again["context"]["deadline"], deadline)
        self.assertEqual(self.browser.calls.count("observe"), 3)
        self.assertNotIn("merge", self.browser.calls)

    def test_ci_wait_unknown_missing_skipped_failed_and_duplicate_never_poll(self) -> None:
        from unittest.mock import patch
        for number, status in enumerate(("unknown", "missing", "skipped", "failure", "duplicate")):
            with self.subTest(status=status):
                self.store = StateStore(self.root / f"no-poll-{number}.sqlite3", clock=self.clock)
                self.store.enqueue("one", self.payload, "one")
                self.config["runtime_dir"] = str(self.root / f"no-poll-runtime-{number}")
                self.commands = FakeCommands(self.repo)
                self.codex = FakeCodex(self.commands)
                self.browser = FakeBrowser(self.commands, self.clock)
                self.browser.default_check_status = status
                self.ci_wait_limits()
                with patch("workflow.runner.time.sleep") as sleep:
                    result = self.run_task()
                self.assertTrue(result["reason"].startswith("merge_gates_unsatisfied"))
                self.assertEqual(result["context"]["ci_poll_count"], 0)
                self.assertEqual(self.browser.calls.count("observe"), 1)
                sleep.assert_not_called()

    def test_ci_wait_policy_or_branch_rules_denial_never_becomes_wait(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits()
        self.browser.default_check_status = "queued"
        self.browser.evidence_overrides["branch_rules_observed"] = False
        self.config["merge_policy"]["enabled"] = False
        with patch("workflow.runner.time.sleep") as sleep:
            result = self.run_task()
        self.assertTrue(result["reason"].startswith("merge_gates_unsatisfied"))
        self.assertEqual(result["context"]["ci_poll_count"], 0)
        self.assertEqual(self.browser.calls.count("observe"), 1)
        sleep.assert_not_called()

    def test_ci_wait_pause_keeps_reservation_and_resume_uses_same_poll(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits(interval=2.5)
        self.browser.check_status_sequence = ["pending", "success"]

        def pause_during_sleep(seconds):
            self.advancing_sleep(seconds)
            self.store.pause("one")

        with patch("workflow.runner.time.sleep", side_effect=pause_during_sleep):
            paused = self.run_task()
        self.assertEqual((paused["status"], paused["phase"]), ("paused", "ci_wait"))
        self.assertEqual(paused["context"]["ci_poll_count"], 1)
        self.assertEqual(paused["context"]["ci_wait"]["state"], "scheduled")
        self.assertEqual(self.browser.calls.count("observe"), 1)
        self.store.resume("one")
        with patch("workflow.runner.time.sleep", side_effect=self.advancing_sleep):
            resumed = self.run_task()
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["context"]["ci_poll_count"], 1)
        self.assertEqual(resumed["context"]["deadline"], paused["context"]["deadline"])
        self.assertEqual(resumed["context"]["model_calls"], 8)

    def test_ci_wait_obeys_model_and_total_time_budgets(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits(model_calls=4)
        self.browser.default_check_status = "pending"
        with patch("workflow.runner.time.sleep") as sleep:
            result = self.run_task()
        self.assertEqual(result["reason"], "model_call_budget_exhausted")
        self.assertEqual(result["context"]["ci_poll_count"], 0)
        sleep.assert_not_called()

    def test_ci_wait_total_deadline_expires_during_wait(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits(interval=1)
        self.config["limits"]["max_seconds"] = 0.5
        self.browser.default_check_status = "pending"
        # Keep pre-wait setup from consuming this artificial half-second
        # deadline; advance UTC only when the fake bounded sleep executes.
        with patch("workflow.runner.time.monotonic", return_value=0), \
                patch("workflow.runner.time.sleep", side_effect=self.advancing_sleep):
            result = self.run_task()
        self.assertEqual(result["reason"], "time_budget_exhausted")
        self.assertEqual(result["context"]["ci_poll_count"], 1)
        self.assertEqual(self.browser.calls.count("observe"), 1)

    def test_ci_wait_budget_survives_code_feedback_and_new_sha(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits(polls=1)
        self.browser.check_status_sequence = ["pending", "failure", "pending"]
        self.browser.code_feedback_at = 2
        with patch("workflow.runner.time.sleep", side_effect=self.advancing_sleep):
            result = self.run_task()
        self.assertEqual(result["reason"], "ci_wait_limit_exhausted")
        self.assertEqual(result["context"]["ci_poll_count"], 1)
        self.assertEqual(self.codex.implementations, 2)
        self.assertEqual(result["context"]["local_head"], "f" * 40)
        self.assertEqual(result["context"]["ci_wait"]["head_sha"], "f" * 40)
        self.assertEqual(self.browser.calls.count("observe"), 3)

    def test_ci_wait_code_feedback_is_handled_before_waiting(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits()
        self.browser.code_feedback_once = True
        self.browser.code_feedback_status = "pending"
        self.codex.after_review = lambda: setattr(self.codex, "implementation_status", "blocked")
        with patch("workflow.runner.time.sleep") as sleep:
            result = self.run_task()
        self.assertTrue(result["reason"].startswith("implementation_blocked"))
        self.assertEqual(result["context"]["ci_poll_count"], 0)
        self.assertEqual(self.codex.implementations, 2)
        sleep.assert_not_called()

    def test_ci_wait_observation_exception_stops_and_resume_uses_new_reservation(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits(polls=2)
        self.browser.check_status_sequence = ["pending", "success"]
        self.browser.observation_exception_at = 2
        with patch("workflow.runner.time.sleep", side_effect=self.advancing_sleep):
            first = self.run_task()
            self.assertEqual(first["status"], "blocked")
            self.assertIn("simulated Chrome observation failure", first["reason"])
            self.assertEqual(first["context"]["ci_poll_count"], 1)
            self.assertEqual(first["context"]["ci_wait"]["state"], "reading")
            self.store.resume("one")
            resumed = self.run_task()
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["context"]["ci_poll_count"], 2)
        self.assertEqual(resumed["context"]["model_calls"], 9)
        self.assertEqual(resumed["context"]["deadline"], first["context"]["deadline"])

    def test_ci_wait_zero_polls_disables_extra_reads(self) -> None:
        from unittest.mock import patch
        self.ci_wait_limits(polls=0)
        self.browser.default_check_status = "pending"
        with patch("workflow.runner.time.sleep") as sleep:
            result = self.run_task()
        self.assertEqual(result["reason"], "ci_wait_limit_exhausted")
        self.assertEqual(result["context"]["ci_poll_count"], 0)
        self.assertEqual(self.browser.calls.count("observe"), 1)
        sleep.assert_not_called()

    def test_ci_wait_limits_reject_nonpositive_or_over_sixty_intervals(self) -> None:
        for index, interval in enumerate((0, -1, 61, float("inf"))):
            with self.subTest(interval=interval):
                if index:
                    self.store.resume("one")
                self.ci_wait_limits(interval=interval)
                result = self.run_task()
                self.assertEqual(result["reason"], "invalid_limit:ci_poll_interval_seconds")
        self.assertEqual(self.codex.calls, [])

    def test_unborn_repository_is_blocked_before_model_or_remote_actions(self) -> None:
        self.commands.bootstrap = False
        result = self.run_task()
        self.assertEqual((result["status"], result["phase"], result["reason"]),
                         ("blocked", "prepare", "bootstrap_required"))
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.count_git("fetch"), 0)
        self.assertEqual(self.count_git("commit"), 0)
        self.assertEqual(self.browser.calls, [])

    def test_missing_commit_grant_stops_with_reviewable_local_changes(self) -> None:
        self.config["grants"]["commit"] = False
        result = self.run_task()
        self.assertEqual((result["status"], result["phase"], result["reason"]),
                         ("blocked", "commit", "authorization_required:commit"))
        self.assertEqual(result["context"]["model_calls"], 1)
        self.assertEqual(self.commands.check_number, 1)
        self.assertEqual(self.count_git("commit"), 0)
        self.assertTrue((Path(result["context"]["worktree"]) / "src/value.py").is_file())

    def test_readonly_patch_proposal_is_applied_and_checked_before_commit_authorization(self) -> None:
        self.config["implementation_mode"] = "patch"
        self.config["grants"]["commit"] = False
        self.commands.seed_files = {"src/value.py": "old value\n"}
        seen = []

        def propose(codex, cwd, output_dir, prompt, schema, *, sandbox, timeout, tick):
            self.assertEqual(sandbox, "read-only")
            self.assertEqual(schema, PATCH_IMPLEMENT_SCHEMA)
            self.assertIn("source_snapshots", prompt)
            self.assertIn("old value", prompt)
            self.assertEqual((cwd / "src/value.py").read_text(encoding="utf-8"), "old value\n")
            tick(4567)
            seen.append("proposal_only")
            return {"result": {"status": "done", "summary": "proposal", "reason": "",
                               "edits": [{"path": "src/value.py",
                                          "expected_sha256": hashlib.sha256((cwd / "src/value.py").read_bytes()).hexdigest(),
                                          "content": "new value\n"}]}}

        result = run_once(self.config, self.store, browser=self.browser,
                          codex_run=propose, check_run=self.commands)
        self.assertEqual((result["status"], result["phase"], result["reason"]),
                         ("blocked", "commit", "authorization_required:commit"))
        self.assertEqual(seen, ["proposal_only"])
        self.assertEqual(result["context"]["model_calls"], 1)
        proposal = result["context"]["patch_proposal"]
        self.assertEqual(proposal["state"], "applied")
        self.assertEqual(proposal["application_receipts"][0]["status"], "applied")
        self.assertEqual((Path(result["context"]["worktree"]) / "src/value.py").read_text(encoding="utf-8"),
                         "new value\n")
        self.assertEqual(self.commands.check_number, 1)
        self.assertEqual(self.count_git("commit"), 0)

    def test_saved_patch_recovers_partial_application_without_second_model_call(self) -> None:
        from unittest.mock import patch
        from workflow.patches import apply_edits
        self.config["implementation_mode"] = "patch"
        self.config["grants"]["commit"] = False
        self.commands.seed_files = {"src/value.py": "old value\n"}
        calls = []

        def propose(codex, cwd, output_dir, prompt, schema, **kwargs):
            calls.append("model")
            return {"result": {"status": "done", "summary": "proposal", "reason": "",
                               "edits": [{"path": "src/value.py",
                                          "expected_sha256": hashlib.sha256((cwd / "src/value.py").read_bytes()).hexdigest(),
                                          "content": "new value\n"}]}}

        def interrupted(*args, **kwargs):
            apply_edits(*args, **kwargs)
            raise OSError("simulated controller interruption after file replacement")

        with patch("workflow.patches.apply_edits", side_effect=interrupted):
            first = run_once(self.config, self.store, browser=self.browser,
                             codex_run=propose, check_run=self.commands)
        self.assertEqual((first["status"], first["phase"]), ("blocked", "implement"))
        self.assertEqual(first["context"]["patch_proposal"]["state"], "pending")
        self.store.resume("one")
        recovered = self.run_task()
        self.assertEqual((recovered["phase"], recovered["reason"]),
                         ("commit", "authorization_required:commit"))
        self.assertEqual(calls, ["model"])
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(recovered["context"]["model_calls"], 1)
        self.assertEqual(recovered["context"]["iterations"], 1)
        self.assertEqual(recovered["context"]["patch_proposal"]["application_receipts"][0]["status"],
                         "already_applied")

    def test_grant_metadata_and_repository_are_mandatory(self) -> None:
        self.config["grants"]["repo_url"] = "https://github.com/elsewhere/repo"
        result = self.run_task()
        self.assertEqual(result["reason"], "authorization_required:commit")
        self.assertEqual(self.count_git("commit"), 0)

    def test_grant_for_another_task_cannot_commit(self) -> None:
        self.config["grants"]["task_ids"] = ["another-task"]
        result = self.run_task()
        self.assertEqual(result["reason"], "authorization_required:commit")
        self.assertEqual(self.count_git("commit"), 0)

    def test_review_receives_actual_diff_and_check_output_not_only_paths(self) -> None:
        result = self.run_task()
        self.assertEqual(result["status"], "completed")
        review = self.codex.calls[1]["prompt"]
        self.assertIn('"actual_evidence"', review)
        self.assertIn('"diff": "diff of the task', review)
        self.assertIn('"tail": ""', review)
        self.assertNotIn('"implementation_summary"', review)
        self.assertTrue(Path(result["context"]["review_input"]["path"]).is_file())

    def test_missing_push_grant_does_not_push(self) -> None:
        self.config["grants"]["push"] = False
        result = self.run_task()
        self.assertEqual((result["phase"], result["reason"]), ("push", "authorization_required:push"))
        self.assertEqual([call["sandbox"] for call in self.codex.calls], ["workspace-write", "read-only"])
        self.assertEqual(self.count_git("push"), 0)
        self.assertEqual(result["context"]["local_checks"]["head_sha"], HEAD)
        self.assertEqual(result["context"]["review"]["head_sha"], HEAD)

    def test_missing_pr_grant_does_not_create_pr(self) -> None:
        self.config["grants"]["create_pr"] = False
        result = self.run_task()
        self.assertEqual((result["phase"], result["reason"]), ("pr", "authorization_required:create_pr"))
        self.assertNotIn("ensure_pr", self.browser.calls)

    def test_missing_merge_grant_stops_after_evidence(self) -> None:
        self.config["grants"]["merge"] = False
        result = self.run_task()
        self.assertEqual((result["phase"], result["reason"]), ("merge", "authorization_required:merge"))
        self.assertTrue(result["context"]["merge_decision"]["eligible"])
        self.assertNotIn("merge", self.browser.calls)

    def test_fake_controller_complete_path_requires_browser_and_main_checks(self) -> None:
        result = self.run_task()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["context"]["merge_receipt"]["merge_sha"], MERGE)
        self.assertEqual(result["context"]["post_merge_checks"]["head_sha"], MERGE)
        self.assertEqual(result["context"]["completion"],
                         {"merged": True, "main_checks_passed": True,
                          "user_accepted": None, "product_usage_verified": None})
        self.assertEqual(self.commands.check_number, 3)
        self.assertEqual(result["context"]["model_calls"], 7)
        self.assertEqual(self.run_task()["status"], "idle")

    def test_existing_pr_repairs_its_exact_branch_without_create_authorization(self) -> None:
        self.payload["pr_url"] = PR_URL
        self.store = StateStore(self.root / "existing-pr.sqlite3", clock=self.clock)
        self.store.enqueue("one", self.payload, "existing-pr:1")
        self.commands.remote[BRANCH] = BASE
        self.config["grants"]["create_pr"] = False
        result = self.run_task()
        self.assertEqual(result["status"], "completed")
        fetches = [argv for argv, _ in self.commands.calls if argv[:2] == ["git", "fetch"]]
        self.assertTrue(any(f"refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}" in argv for argv in fetches))
        pushes = [argv for argv, _ in self.commands.calls if argv[:2] == ["git", "push"]]
        self.assertEqual(pushes, [["git", "push", "origin", f"{HEAD}:refs/heads/{BRANCH}"]])

    def test_browser_sessions_use_the_same_durable_model_budget(self) -> None:
        self.config["limits"] = {"max_model_calls": 3}
        result = self.run_task()
        self.assertEqual((result["phase"], result["reason"]), ("observe", "model_call_budget_exhausted"))
        self.assertEqual(result["context"]["model_calls"], 3)
        self.assertEqual(self.browser.calls, ["ensure_pr"])
        self.store.resume("one")
        again = self.run_task()
        self.assertEqual(again["reason"], "model_call_budget_exhausted")
        self.assertEqual(self.browser.calls, ["ensure_pr"])

    def test_post_merge_worktree_must_be_detached_and_clean(self) -> None:
        self.commands.post_merge_dirty = True
        result = self.run_task()
        self.assertEqual(result["reason"], "post_merge_worktree_not_clean")
        self.assertNotIn("completion", result["context"])

    def test_post_merge_attached_branch_is_not_accepted(self) -> None:
        self.commands.post_merge_attached = True
        result = self.run_task()
        self.assertEqual(result["reason"], "post_merge_worktree_identity_mismatch")
        self.assertNotIn("completion", result["context"])

    def test_post_merge_check_that_pollutes_worktree_is_not_success(self) -> None:
        def pollute(number, cwd):
            if number == 3:
                self.commands.post_merge_dirty = True
        self.commands.after_check = pollute
        result = self.run_task()
        self.assertEqual(result["reason"], "post_merge_worktree_not_clean")
        self.assertNotIn("completion", result["context"])

    def test_local_tests_alone_cannot_complete(self) -> None:
        result = self.run_task(browser=False)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "browser_unavailable")
        self.assertNotIn("completion", result["context"])

    def test_merge_click_without_chrome_confirmation_cannot_complete(self) -> None:
        self.browser.confirm_merge = False
        result = self.run_task()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["reason"].startswith("merge_result_unconfirmed"))
        self.assertNotIn("post_merge_checks", result["context"])

    def test_failed_check_repair_is_bounded_and_model_count_is_persistent(self) -> None:
        self.config["limits"] = {"max_iterations": 1}
        self.commands.check_codes = [1]
        result = self.run_task()
        self.assertEqual(result["reason"], "iteration_budget_exhausted")
        self.assertEqual(result["context"]["iterations"], 1)
        self.store.resume("one")
        again = self.run_task()
        self.assertEqual(again["reason"], "iteration_budget_exhausted")
        self.assertEqual(len(self.codex.calls), 1)
        self.assertEqual(again["context"]["deadline"], result["context"]["deadline"])

    def test_failed_code_check_can_be_repaired_within_budget(self) -> None:
        self.commands.check_codes = [1, 0, 0, 0]
        result = self.run_task()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["context"]["iterations"], 2)
        self.assertEqual(result["context"]["model_calls"], 8)

    def test_model_call_budget_does_not_reset_on_resume(self) -> None:
        self.config["limits"] = {"max_model_calls": 1}
        result = self.run_task()
        self.assertEqual((result["phase"], result["reason"]), ("review", "model_call_budget_exhausted"))
        self.store.resume("one")
        again = self.run_task()
        self.assertEqual(again["reason"], "model_call_budget_exhausted")
        self.assertEqual(len(self.codex.calls), 1)

    def test_expired_deadline_survives_resume(self) -> None:
        self.config["grants"]["commit"] = False
        first = self.run_task()
        self.clock.value = first["context"]["deadline"]
        self.store.resume("one")
        again = self.run_task()
        self.assertEqual(again["reason"], "time_budget_exhausted")
        self.assertEqual(len(self.codex.calls), 1)

    def test_blocked_authorization_resumes_at_checkpoint_without_new_implementation(self) -> None:
        self.config["grants"]["commit"] = False
        first = self.run_task()
        self.config["grants"]["commit"] = True
        self.store.resume("one")
        result = self.run_task()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.codex.implementations, 1)
        self.assertEqual(result["context"]["model_calls"], 7)
        self.assertEqual(result["context"]["deadline"], first["context"]["deadline"])

    def test_reclaimed_pid_requires_matching_human_confirmation(self) -> None:
        old = self.store.claim("old-owner")
        context = {"active_process": {"pid": 777, "owner": "old-owner", "kind": "implement"},
                   "started_at": 1000, "deadline": 2200}
        self.store.checkpoint("one", "old-owner", "prepare", context)
        self.clock.value = old["lease_expires_at"]
        blocked = self.run_task()
        self.assertEqual(blocked["reason"], "awaiting_process_reconciliation")
        self.assertEqual(self.commands.calls, [])
        self.config["process_reconciliations"] = {
            "one": {"pid": 777, "owner": "old-owner", "confirmed_by": "operator", "confirmed_at": 1059}}
        self.store.resume("one")
        self.commands.bootstrap = False
        result = self.run_task()
        self.assertEqual(result["reason"], "bootstrap_required")
        self.assertNotIn("active_process", result["context"])
        self.assertEqual(result["context"]["process_reconciliations"][0]["process"]["pid"], 777)

    def test_pause_is_cooperative_and_keeps_budget(self) -> None:
        self.commands.pause_on_next = lambda: self.store.pause("one")
        result = self.run_task()
        self.assertEqual((result["status"], result["reason"]), ("paused", "pause_requested"))
        self.assertNotIn("active_process", result["context"])
        self.assertEqual(result["context"]["model_calls"], 0)

    def test_checks_cannot_silently_change_head_and_reuse_evidence(self) -> None:
        def mutate(number, cwd):
            if number == 2:
                self.commands.heads[str(cwd)] = OTHER
        self.commands.after_check = mutate
        result = self.run_task()
        self.assertEqual(result["reason"], "head_changed_during_checks")
        self.assertEqual(len(self.codex.calls), 1)
        self.assertNotIn("review", result["context"])

    def test_rename_source_and_destination_are_both_scope_checked(self) -> None:
        self.commands.status_override = "R  src/value.py\x00.github/workflows/ci.yml\x00"
        result = self.run_task()
        self.assertEqual(result["status"], "blocked")
        self.assertIn(".github/workflows/ci.yml", result["reason"])
        self.assertEqual(self.codex.calls, [])

    def test_dotcodex_configuration_is_protected_with_broad_allowlist(self) -> None:
        self.payload["allowed_paths"] = ["**"]
        self.store = StateStore(self.root / "dotcodex.sqlite3", clock=self.clock)
        self.store.enqueue("one", self.payload, "trigger:protected")
        self.commands.status_override = "?? src/.codex/config.toml\x00"
        result = self.run_task()
        self.assertEqual(result["reason"], "protected_path_changed:src/.codex/config.toml")
        self.assertEqual(self.codex.calls, [])

    def test_controller_is_protected_with_broad_allowlist(self) -> None:
        self.payload["allowed_paths"] = ["**"]
        self.store = StateStore(self.root / "controller.sqlite3", clock=self.clock)
        self.store.enqueue("one", self.payload, "trigger:protected")
        self.commands.status_override = "?? controller/main.py\x00"
        result = self.run_task()
        self.assertEqual(result["reason"], "protected_path_changed:controller/main.py")
        self.assertEqual(self.codex.calls, [])

    def test_review_environment_block_does_not_retry_implementation(self) -> None:
        self.codex.review_verdict = "blocked"
        result = self.run_task()
        self.assertTrue(result["reason"].startswith("review_blocked"))
        self.assertEqual(self.codex.implementations, 1)
        self.assertEqual(self.count_git("push"), 0)

    def test_base_change_invalidates_checks_and_review(self) -> None:
        self.codex.after_review = lambda: self.commands.remote.update(main=OTHER)
        result = self.run_task()
        self.assertEqual(result["reason"], "base_changed_revalidation_required")
        self.assertNotIn("local_checks", result["context"])
        self.assertNotIn("review", result["context"])
        self.assertEqual(self.count_git("push"), 0)

    def test_stale_browser_sha_never_merges(self) -> None:
        self.browser.observation_head = OTHER
        result = self.run_task()
        self.assertEqual(result["reason"], "browser_observation_sha_mismatch")
        self.assertNotIn("merge", self.browser.calls)

    def test_merged_observation_for_another_pr_is_not_accepted(self) -> None:
        self.browser.did_merge = True
        self.browser.observation_url = REPO_URL + "/pull/2"
        result = self.run_task()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["reason"].startswith("merge_gates_unsatisfied"))
        self.assertNotIn("post_merge_checks", result["context"])

    def test_exact_ci_code_feedback_can_repair_and_push_same_pr(self) -> None:
        self.config["limits"] = {"max_model_calls": 12}
        self.browser.code_feedback_once = True
        result = self.run_task()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.codex.implementations, 2)
        self.assertEqual(self.count_git("push"), 2)
        self.assertEqual(result["context"]["local_head"], "f" * 40)
        self.assertEqual(result["context"]["review"]["head_sha"], "f" * 40)
        self.assertEqual(result["context"]["local_checks"]["head_sha"], "f" * 40)
        self.assertEqual(self.browser.calls.count("merge"), 1)

    def test_stale_failure_evidence_cannot_start_another_implementation(self) -> None:
        self.browser.code_feedback_once = True
        self.browser.stale_failure_evidence = True
        result = self.run_task()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["reason"].startswith("merge_gates_unsatisfied"))
        self.assertEqual(self.codex.implementations, 1)

    def test_partially_successful_push_is_reconciled_before_repeating(self) -> None:
        self.commands.push_uncertain_once = True
        first = self.run_task()
        self.assertEqual(first["phase"], "push")
        self.assertEqual(self.commands.remote[BRANCH], HEAD)
        self.store.resume("one")
        result = self.run_task()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.count_git("push"), 1)

    def test_partially_successful_merge_is_observed_before_base_gate(self) -> None:
        self.browser.uncertain_merge_once = True
        first = self.run_task()
        self.assertEqual(first["phase"], "merge")
        self.assertTrue(self.browser.did_merge)
        self.store.resume("one")
        self.config["grants"]["merge"] = False  # Reconciliation itself is read-only.
        result = self.run_task()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.browser.calls.count("merge"), 1)

    def test_no_progress_limit_stops_repeated_identical_repairs(self) -> None:
        self.codex.change_each_time = False
        self.commands.check_codes = [1] * 10
        result = self.run_task()
        self.assertEqual(result["reason"], "no_progress_limit_reached")
        self.assertEqual(result["context"]["model_calls"], 3)


if __name__ == "__main__":
    unittest.main()
