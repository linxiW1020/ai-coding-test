"""Single-worker, lease, recovery and idempotency invariants."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from workflow.state import (
    ConflictError, InvalidTransitionError, LeaseLostError, StateStore,
)


class FakeClock:
    def __init__(self, value: float = 1000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.clock = FakeClock()
        self.store = StateStore(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def enqueue(self, task_id: str = "task-1") -> dict:
        return self.store.enqueue(task_id, {"request": "小任务"}, f"issue:{task_id}")

    def test_duplicate_trigger_reuses_original_task(self) -> None:
        first = self.enqueue()
        second = self.store.enqueue("other-id", {"request": "小任务"}, "issue:task-1")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(len(self.store.events("task-1")), 1)

    def test_canonical_payload_and_conflicting_identity(self) -> None:
        self.store.enqueue("one", {"b": 2, "a": 1}, "key")
        self.assertFalse(self.store.enqueue("one", {"a": 1, "b": 2}, "key")["created"])
        with self.assertRaises(ConflictError):
            self.store.enqueue("two", {"a": 2, "b": 2}, "key")
        with self.assertRaises(ConflictError):
            self.store.enqueue("one", {"a": 1, "b": 2}, "other-key")

    def test_concurrent_claims_have_one_winner_globally(self) -> None:
        self.enqueue("task-1")
        self.enqueue("task-2")
        barrier = threading.Barrier(2)

        def compete(owner: str) -> dict | None:
            independent_store = StateStore(self.path, clock=self.clock)
            barrier.wait()
            return independent_store.claim(owner)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, ["owner-a", "owner-b"]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.list("running")), 1)
        self.assertEqual(len(self.store.list("queued")), 1)
        self.assertIsNone(self.store.claim("owner-c"))

    def test_expired_lease_recovers_checkpoint_and_fences_old_owner(self) -> None:
        self.enqueue()
        original = self.store.claim("old-owner", lease_seconds=10)
        self.assertEqual(original["phase"], "prepare")
        self.assertEqual(original["context"], {})
        self.store.checkpoint("task-1", "old-owner", "review",
                              {"head_sha": "abc123", "child_pid": 42})
        self.clock.value += 10  # Equality is expired, not still leased.
        for mutation in (
            lambda: self.store.heartbeat("task-1", "old-owner"),
            lambda: self.store.checkpoint("task-1", "old-owner", "wrong", {}),
            lambda: self.store.finish("task-1", "old-owner", "completed"),
        ):
            with self.assertRaises(LeaseLostError):
                mutation()
        reopened = StateStore(self.path, clock=self.clock)
        recovered = reopened.claim("new-owner")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["previous_owner"], "old-owner")
        self.assertEqual(recovered["attempt"], 2)
        self.assertEqual(recovered["phase"], "review")
        self.assertEqual(recovered["context"], {"head_sha": "abc123", "child_pid": 42})
        with self.assertRaises(LeaseLostError):
            self.store.finish("task-1", "old-owner", "completed")
        self.assertEqual(self.store.events("task-1")[-1]["kind"], "lease_reclaimed")

    def test_owner_cannot_be_reused_for_later_attempt(self) -> None:
        self.enqueue()
        self.store.claim("first-owner", lease_seconds=1)
        self.clock.value += 1
        with self.assertRaises(ConflictError):
            self.store.claim("first-owner")
        self.store.claim("second-owner")
        self.store.finish("task-1", "second-owner", "paused", "stop complete")
        self.store.resume("task-1")
        with self.assertRaises(ConflictError):
            self.store.claim("first-owner")
        self.assertEqual(self.store.claim("third-owner")["attempt"], 3)

    def test_running_pause_keeps_slot_until_child_is_stopped(self) -> None:
        self.enqueue("task-1")
        self.enqueue("task-2")
        self.store.claim("owner-a")
        requested = self.store.pause("task-1")
        self.assertEqual(requested["status"], "running")
        self.assertTrue(requested["pause_requested"])
        self.assertEqual(requested["owner"], "owner-a")
        self.assertIsNone(self.store.claim("owner-b"))
        self.assertTrue(self.store.heartbeat("task-1", "owner-a")["pause_requested"])
        with self.assertRaises(InvalidTransitionError):
            self.store.resume("task-1")
        paused = self.store.finish("task-1", "owner-a", "paused", "child stopped")
        self.assertIsNone(paused["owner"])
        self.assertEqual(self.store.claim("owner-b")["id"], "task-2")

    def test_pause_pending_and_resume_preserve_checkpoint(self) -> None:
        self.enqueue()
        self.assertEqual(self.store.pause("task-1")["status"], "paused")
        self.assertIsNone(self.store.claim("owner-a"))
        self.store.resume("task-1")
        self.store.claim("owner-a")
        self.store.checkpoint("task-1", "owner-a", "checks", {"round": 2})
        self.store.finish("task-1", "owner-a", "blocked", "needs decision")
        resumed = self.store.resume("task-1")
        self.assertIsNone(resumed["reason"])
        claimed = self.store.claim("owner-b")
        self.assertEqual((claimed["phase"], claimed["context"]), ("checks", {"round": 2}))

    def test_recovery_of_pause_request_retains_request_and_takes_precedence(self) -> None:
        self.enqueue("task-1")
        self.enqueue("task-2")
        self.store.claim("owner-a", lease_seconds=5)
        self.store.pause("task-1")
        self.clock.value += 5
        recovery = self.store.claim("owner-b")
        self.assertEqual(recovery["id"], "task-1")
        self.assertTrue(recovery["pause_requested"])
        self.assertTrue(recovery["recovered"])

    def test_unreconciled_child_blocks_global_slot_until_same_task_is_resumed(self) -> None:
        self.enqueue("task-1")
        self.enqueue("task-2")
        self.store.claim("owner-a")
        self.store.checkpoint("task-1", "owner-a", "implement",
                              {"active_process": {"pid": 123, "owner": "owner-a"}})
        self.store.finish("task-1", "owner-a", "blocked", "awaiting_process_reconciliation")
        self.assertIsNone(self.store.claim("owner-b"))
        self.store.resume("task-1")
        recovered = self.store.claim("owner-c")
        self.assertEqual(recovered["id"], "task-1")
        self.store.checkpoint("task-1", "owner-c", "implement", {})
        self.store.finish("task-1", "owner-c", "blocked", "another decision needed")
        self.assertEqual(self.store.claim("owner-d")["id"], "task-2")

    def test_heartbeat_extends_lease_and_finish_requires_live_owner(self) -> None:
        self.enqueue()
        self.store.claim("owner-a", lease_seconds=10)
        self.clock.value += 5
        beat = self.store.heartbeat("task-1", "owner-a", lease_seconds=20)
        self.assertEqual(beat["lease_expires_at"], 1025)
        with self.assertRaises(LeaseLostError):
            self.store.finish("task-1", "owner-b", "completed")
        with self.assertRaises(InvalidTransitionError):
            self.store.finish("task-1", "owner-a", "queued")
        self.store.finish("task-1", "owner-a", "completed", "checks passed")
        with self.assertRaises(InvalidTransitionError):
            self.store.resume("task-1")
        with self.assertRaises(InvalidTransitionError):
            self.store.pause("task-1")

    def test_backward_clock_cannot_move_lease_or_event_time_backwards(self) -> None:
        self.enqueue()
        self.store.claim("owner-a", lease_seconds=10)
        self.clock.value = 1005
        self.store.heartbeat("task-1", "owner-a", lease_seconds=10)
        self.clock.value = 900
        reopened = StateStore(self.path, clock=self.clock)
        beat = reopened.heartbeat("task-1", "owner-a", lease_seconds=1)
        self.assertEqual(beat["lease_expires_at"], 1015)
        self.assertEqual(beat["updated_at"], 1005)
        times = [event["at"] for event in self.store.events("task-1")]
        self.assertEqual(times, sorted(times))

    def test_rejected_expired_write_cannot_be_revived_by_clock_rollback(self) -> None:
        self.enqueue()
        self.store.claim("owner-a", lease_seconds=10)
        self.clock.value = 1010
        with self.assertRaises(LeaseLostError):
            self.store.heartbeat("task-1", "owner-a")
        self.clock.value = 900
        reopened = StateStore(self.path, clock=self.clock)
        with self.assertRaises(LeaseLostError):
            reopened.finish("task-1", "owner-a", "completed")
        recovered = reopened.claim("owner-b")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["updated_at"], 1010)

    def test_invalid_json_values_and_lease_lengths_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.enqueue("one", {"score": float("nan")}, "key")
        with self.assertRaises(ValueError):
            self.store.enqueue("one", {1: "ambiguous"}, "key")
        self.enqueue()
        for duration in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.store.claim("owner-a", duration)

    def test_budget_renewal_preserves_history_phase_and_operation_intents(self) -> None:
        self.enqueue()
        self.store.claim("owner-a")
        context = {"started_at": 900, "deadline": 950, "model_calls": 8,
                   "iterations": 3, "no_progress_count": 2, "no_progress": 2, "ci_poll_count": 3,
                   "command_sequence": 73, "fingerprint": "unchanged",
                   "review": {"head_sha": "old", "observed_at": 910},
                   "push_intent": {"head_sha": "abc"}}
        self.store.checkpoint("task-1", "owner-a", "push", context)
        self.store.finish("task-1", "owner-a", "blocked", "budget exhausted")
        renewed = self.store.renew_budget("task-1", "operator", "Approved one more bounded attempt")
        self.assertEqual((renewed["status"], renewed["phase"]), ("blocked", "push"))
        current = renewed["context"]
        for key in ("started_at", "deadline", "model_calls", "iterations", "no_progress_count", "no_progress", "ci_poll_count"):
            self.assertNotIn(key, current)
        self.assertEqual(current["command_sequence"], 73)
        self.assertEqual(current["push_intent"], context["push_intent"])
        self.assertEqual(current["review"], context["review"])
        self.assertEqual(current["budget_history"], [{
            "started_at": 900, "deadline": 950, "model_calls": 8,
            "iterations": 3, "no_progress_count": 2, "no_progress": 2, "ci_poll_count": 3,
            "confirmed_by": "operator", "confirmed_at": 1000,
            "reason": "Approved one more bounded attempt"}])
        self.assertEqual(self.store.events("task-1")[-1]["kind"], "budget_renewed")

    def test_budget_renewal_rejects_running_old_pid_and_missing_confirmation(self) -> None:
        self.enqueue()
        self.store.claim("owner-a")
        with self.assertRaises(InvalidTransitionError):
            self.store.renew_budget("task-1", "operator", "more work")
        self.store.checkpoint("task-1", "owner-a", "implement",
                              {"active_process": {"pid": 123, "owner": "owner-a"}})
        self.store.finish("task-1", "owner-a", "blocked", "awaiting_process_reconciliation")
        with self.assertRaises(InvalidTransitionError):
            self.store.renew_budget("task-1", "operator", "more work")
        for confirmed_by, reason in (("", "reason"), ("operator", "  ")):
            with self.assertRaises(ValueError):
                self.store.renew_budget("task-1", confirmed_by, reason)
        self.assertNotIn("budget_history", self.store.get("task-1")["context"])

    def test_ordinary_resume_keeps_exhausted_budget_and_never_adds_history(self) -> None:
        self.enqueue()
        self.store.claim("owner-a")
        context = {"started_at": 900, "deadline": 950, "model_calls": 8,
                   "iterations": 3, "no_progress_count": 2, "ci_poll_count": 3}
        self.store.checkpoint("task-1", "owner-a", "review", context)
        self.store.finish("task-1", "owner-a", "blocked", "budget exhausted")
        resumed = self.store.resume("task-1")
        self.assertEqual(resumed["context"], context)


if __name__ == "__main__":
    unittest.main()
