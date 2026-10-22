"""Independent, prewritten pilot acceptance; invoked outside model worktree."""
import importlib.util
from pathlib import Path
import sys


def verify(worktree: Path):
    spec = importlib.util.spec_from_file_location("pilot_ci_status", worktree / "src" / "ci_status.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    check = module.required_checks_succeeded
    assert check({"unit": "success", "lint": "success"}, ["unit", "lint"]) is True, "all required success"
    for status in ("failure", "skipped", "pending", "neutral", "unknown", "cancelled", None, True, 1):
        assert check({"unit": status}, ["unit"]) is False, f"must reject {status!r}"
    assert check({}, ["unit"]) is False, "missing is not success"
    assert check({"unit": "success"}, []) is False, "empty requirement cannot pass vacuously"
    assert check({"unit": "success"}, ["unit", "unit"]) is False, "duplicate required names are invalid"
    assert check({"unit": "success"}, [""]) is False, "empty check names are invalid"
    print("Independent pilot contract: PASS")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: check_ci_status.py WORKTREE")
    verify(Path(sys.argv[1]).resolve())
