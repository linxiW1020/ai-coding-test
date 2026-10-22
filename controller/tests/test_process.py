import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from workflow.process import ProcessStopped, run_process


class ProcessTests(unittest.TestCase):
    def test_argv_is_literal_and_exit_failure_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_process([sys.executable, "-c", "import sys; print(sys.argv[1]); sys.exit(7)",
                                  'literal $() & ` text'], root, root / "run", timeout=10)
            self.assertEqual(result["returncode"], 7)
            self.assertIn('literal $() & ` text', Path(result["stdout"]).read_text())

    def test_timeout_stops_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = time.monotonic()
            with self.assertRaisesRegex(ProcessStopped, "process_timeout"):
                run_process([sys.executable, "-c", "import time; time.sleep(30)"], root,
                            root / "timeout", timeout=0.3)
            self.assertLess(time.monotonic() - started, 8)

    def test_pause_callback_stops_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def pause(pid):
                calls.append(pid)
                if len(calls) > 1:
                    raise ProcessStopped("pause_requested")
            with self.assertRaisesRegex(ProcessStopped, "pause_requested"):
                run_process([sys.executable, "-c", "import time; time.sleep(30)"], root,
                            root / "pause", timeout=10, tick=pause)
            self.assertEqual(len(set(calls)), 1)
