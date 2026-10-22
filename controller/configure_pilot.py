"""Create this pilot's local files, with every external grant disabled."""
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    repo = ROOT.parent
    if not (repo / ".git").is_dir():
        raise SystemExit("Run from ai-coding-test/controller after source placement")
    config_path, task_path = ROOT / "config.local.json", ROOT / "task.local.json"
    if config_path.exists() or task_path.exists():
        raise SystemExit("Local configuration already exists; preserve it and edit intentionally")
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    config.update(repo_path=str(repo), runtime_dir=str(repo.parent / "codex-workflow" / ".runtime"),
                  codex_path=shutil.which("codex") or "codex")
    task = {
        "id": "pilot-ci-status-001", "dedupe_key": "pilot/required-check-status-v1",
        "goal": "修复CI状态聚合函数：只有全部指定检查确实success才能通过",
        "acceptance": ["非空且不重复的required名称均存在，且值严格等于success时返回True",
                       "failure/skipped/pending/neutral/unknown/cancelled/缺失/非字符串success均返回False",
                       "空required、空名称和重复required名称返回False；补充相应回归测试"],
        "allowed_paths": ["src/ci_status.py", "tests/test_ci_status.py"],
        "checks": [{"name": "verify", "argv": [sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"]},
                   {"name": "acceptance", "argv": [sys.executable, "-X", "utf8", str(ROOT / "acceptance" / "check_ci_status.py"), "."]}],
        "branch": "codex/pilot-ci-status-001", "pr_url": None, "issue_url": None
    }
    for path, value in ((config_path, config), (task_path, task)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"config": str(config_path), "task": str(task_path), "external_grants_enabled": False}, ensure_ascii=False))
