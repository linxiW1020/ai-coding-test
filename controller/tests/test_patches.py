import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workflow.patches import (
    MAX_FILE_BYTES, MAX_FILES, apply_edits, snapshot_files,
)


def digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "src").mkdir()
        (self.root / "src/a.py").write_bytes(b"old\r\n")
        self.allowed = ["src/**", "tests/**"]

    def edit(self, path="src/a.py", content="new\n", expected=None):
        return {"path": path, "expected_sha256": digest("old\r\n") if expected is None else expected,
                "content": content}

    def apply(self, edits, allowed=None, protected=None):
        return apply_edits(self.root, edits, self.allowed if allowed is None else allowed,
                           [] if protected is None else protected)

    def test_replace_and_create_with_raw_byte_hashes(self):
        receipts = self.apply([self.edit(), self.edit("tests/nested/new.py", "你好\n", "")])
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"new\n")
        self.assertEqual((self.root / "tests/nested/new.py").read_bytes(), "你好\n".encode())
        self.assertEqual([item["status"] for item in receipts], ["applied", "applied"])
        self.assertEqual(receipts[0]["before_sha256"], digest("old\r\n"))
        self.assertEqual(receipts[1]["before_sha256"], "")
        self.assertEqual(receipts[1]["sha256"], digest("你好\n"))
        self.assertEqual(list(self.root.rglob(".codex-edit-*")), [])

    def test_snapshot_is_readonly_bounded_sorted_and_preserves_newlines(self):
        (self.root / "src/b.py").write_text("二", encoding="utf-8")
        (self.root / "src/not-text.bin").write_bytes(b"\xff\x00")
        (self.root / "src/secret.py").write_text("secret", encoding="utf-8")
        before = (self.root / "src/a.py").stat().st_mtime_ns
        rows = snapshot_files(self.root, self.allowed, ["src/secret.py"])
        self.assertEqual([row["path"] for row in rows], ["src/a.py", "src/b.py"])
        self.assertEqual(rows[0], {"path": "src/a.py", "sha256": digest("old\r\n"), "content": "old\r\n"})
        self.assertEqual((self.root / "src/a.py").stat().st_mtime_ns, before)

    def test_snapshot_exact_allowlist_does_not_inspect_unrelated_directory(self):
        (self.root / "unrelated").mkdir()
        (self.root / "unrelated/private.bin").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        with patch("workflow.patches.os.scandir", side_effect=AssertionError("unneeded scan")):
            rows = snapshot_files(self.root, ["src/a.py"], [])
        self.assertEqual(len(rows), 1)

    def test_snapshot_overlapping_patterns_do_not_duplicate_files(self):
        rows = snapshot_files(self.root, ["src/**", "src/a.py", "src/*.py"], [])
        self.assertEqual([row["path"] for row in rows], ["src/a.py"])

    def test_all_sha_checks_happen_before_any_mutation(self):
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.apply([self.edit("tests/new.py", "created", ""), self.edit(expected="0" * 64)])
        self.assertFalse((self.root / "tests").exists())
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"old\r\n")

    def test_existing_file_cannot_claim_creation_and_missing_file_needs_empty_sha(self):
        for edit in (self.edit(expected=""), self.edit("src/new.py", expected="a" * 64)):
            with self.subTest(edit=edit):
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    self.apply([edit])

    def test_replay_does_not_replace_or_change_mtime(self):
        edits = [self.edit(), self.edit("src/new.py", "created", "")]
        self.apply(edits)
        before = (self.root / "src/a.py").stat().st_mtime_ns
        with patch("workflow.patches.os.replace", side_effect=AssertionError("replay wrote")):
            receipts = self.apply(edits)
        self.assertEqual([item["status"] for item in receipts], ["already_applied"] * 2)
        self.assertEqual((self.root / "src/a.py").stat().st_mtime_ns, before)

    def test_invalid_windows_and_traversal_paths_are_rejected(self):
        for path in ("../outside.py", "/src/a.py", "C:/src/a.py", "src\\a.py", "src/../a.py",
                     "src//a.py", "./src/a.py", "src/a.py:stream", "src/NUL.py", "src/com1",
                     "src/LPT9.txt", "src/CON .txt", "src/COM¹.txt", "src/dir./a.py",
                     "src/dir /a.py", "src/a.py.", "src/a.py ", "src/a\x00.py", "src/a?.py"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.apply([self.edit(path, expected="")], allowed=["**"])

    def test_protected_paths_stay_protected_with_broad_allowlist(self):
        for path in (".github/workflows/ci.yml", "AGENTS.md", "nested/CLAUDE.md", ".codex/config.toml",
                     "workflow/policy.py", "tests/test_policy.py", "package.json", ".git/config",
                     "nested/.git/config", "src/sensitive.py"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "protected"):
                    self.apply([self.edit(path, expected="")], allowed=["**"], protected=["src/sensitive.py"])

    def test_unlisted_path_rejected(self):
        with self.assertRaisesRegex(ValueError, "allowlist"):
            self.apply([self.edit("docs/readme.md", expected="")])

    def test_duplicate_paths_are_case_insensitive(self):
        for path in ("src/a.py", "src/A.py"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    self.apply([self.edit(), self.edit(path)])
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"old\r\n")

    def test_count_and_byte_limits_reject_without_writes(self):
        cases = ([self.edit(f"src/f{i}.py", "x", "") for i in range(MAX_FILES + 1)],
                 [self.edit(content="é" * (MAX_FILE_BYTES // 2 + 1))])
        for edits in cases:
            with self.subTest(count=len(edits)):
                with self.assertRaises(ValueError):
                    self.apply(edits)
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"old\r\n")

    def test_snapshot_count_total_and_file_limits(self):
        for index in range(MAX_FILES):
            (self.root / f"src/f{index:02}.py").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "limit"):
            snapshot_files(self.root, self.allowed, [])
        for path in (self.root / "src").glob("f*.py"):
            path.unlink()
        for index in range(5):
            (self.root / f"src/f{index}.py").write_bytes(b"x" * MAX_FILE_BYTES)
        with self.assertRaisesRegex(ValueError, "limit"):
            snapshot_files(self.root, self.allowed, [])
        (self.root / "src/f0.py").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            snapshot_files(self.root, ["src/f0.py"], [])

    def test_nontext_existing_file_and_nontext_content_are_rejected(self):
        (self.root / "src/bin.py").write_bytes(b"\xff")
        for edit in (self.edit("src/bin.py", "text", ""), self.edit(content="a\x00b"),
                     self.edit(content="\ud800"), {"path": "src/a.py", "content": "x"}):
            with self.subTest(edit=repr(edit)):
                with self.assertRaises(ValueError):
                    self.apply([edit])

    def test_root_must_be_existing_real_directory(self):
        for root in (self.root / "absent", self.root / "src/a.py", str(self.root)):
            with self.subTest(root=root):
                with self.assertRaises(ValueError):
                    snapshot_files(root, [], [])

    def test_symlink_file_parent_and_root_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "a.py").write_text("outside", encoding="utf-8")
        try:
            (self.root / "src/link.py").symlink_to(outside / "a.py")
            (self.root / "src/linked").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"OS does not allow creating symlinks: {error}")
        for path in ("src/link.py", "src/linked/a.py"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "symlink|junction"):
                    self.apply([self.edit(path, expected="")])
                with self.assertRaisesRegex(ValueError, "symlink|junction"):
                    snapshot_files(self.root, [path], [])
        with self.assertRaisesRegex(ValueError, "real directories"):
            snapshot_files(self.root / "src/linked", ["a.py"], [])
        self.assertEqual((outside / "a.py").read_text(encoding="utf-8"), "outside")

    def test_reparse_attribute_is_denied_even_without_symlink_mode(self):
        original = Path.lstat
        def reparse(path):
            info = original(path)
            if path == self.root / "src":
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", reparse):
            with self.assertRaisesRegex(ValueError, "junction"):
                self.apply([self.edit()])

    @unittest.skipUnless(os.name == "nt", "Windows junction integration check")
    def test_real_windows_junction_parent_and_root_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "a.py").write_bytes(b"outside")
        junction = self.root / "src/junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        with self.assertRaisesRegex(ValueError, "junction"):
            self.apply([self.edit("src/junction/a.py", "changed", digest("outside"))])
        for allowed in (["src/junction/a.py"], ["src/**"]):
            with self.subTest(allowed=allowed):
                with self.assertRaisesRegex(ValueError, "junction"):
                    snapshot_files(self.root, allowed, [])
        with self.assertRaisesRegex(ValueError, "real directories"):
            snapshot_files(junction, ["a.py"], [])
        self.assertEqual((outside / "a.py").read_bytes(), b"outside")

    def test_snapshot_protected_directories_are_pruned_without_reading_them(self):
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / ".github/workflows/ci.yml").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        (self.root / ".git").mkdir()
        (self.root / ".git/config").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        rows = snapshot_files(self.root, ["**"], [])
        self.assertEqual([row["path"] for row in rows], ["src/a.py"])

    def test_staging_failure_cleans_only_own_temporary_files(self):
        unrelated = self.root / "src/.codex-edit-unrelated"
        unrelated.write_text("preserve", encoding="utf-8")
        with patch("workflow.patches.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.apply([self.edit()])
        self.assertEqual(list((self.root / "src").glob(".codex-edit-*")), [unrelated])
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"old\r\n")

    def test_change_during_staging_is_detected_before_replacements(self):
        real_fsync = os.fsync
        def change_file(descriptor):
            real_fsync(descriptor)
            (self.root / "src/a.py").write_bytes(b"external change")
        with patch("workflow.patches.os.fsync", side_effect=change_file):
            with self.assertRaisesRegex(ValueError, "changed during preflight"):
                self.apply([self.edit(), self.edit("src/new.py", "created", "")])
        self.assertFalse((self.root / "src/new.py").exists())
        self.assertEqual(list(self.root.rglob(".codex-edit-*")), [])


if __name__ == "__main__":
    unittest.main()
