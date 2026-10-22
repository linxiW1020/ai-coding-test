"""Component smoke only: genuine Codex edit plus independent checks, no commits."""
import json
from pathlib import Path
import shutil
import sys
import time

from workflow.process import run_codex, run_process
from workflow.patches import snapshot_files, apply_edits
from workflow.runner import PATCH_IMPLEMENT_SCHEMA

ROOT = Path(__file__).resolve().parent
PILOT = ROOT.parent if (ROOT.parent / ".git").is_dir() else ROOT.parent / "ai-coding-test"
RUNTIME = Path(json.loads((ROOT / "config.local.json").read_text(encoding="utf-8")).get("runtime_dir")) if (ROOT / "config.local.json").is_file() else ROOT / ".runtime"

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    directory = RUNTIME / "local-smoke" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    worktree = directory / "worktree"
    for name in ("src/__init__.py", "src/ci_status.py", "tests/test_ci_status.py"):
        target = worktree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PILOT / name, target)
    acceptance = [sys.executable, str(ROOT / "acceptance" / "check_ci_status.py"), str(worktree)]
    before = run_process(acceptance, worktree, directory / "before", timeout=30)
    allowed = ["src/ci_status.py", "tests/test_ci_status.py"]
    snapshot = snapshot_files(worktree, allowed, [])
    prompt = """这是一个已授权的本地隔离修复任务。你只负责返回结构化修改提案，不能直接写文件或执行工具。外层控制器将在范围和原文件SHA验证后应用并独立测试。
尚未授权任何git提交/推送/PR。只能修改src/ci_status.py和tests/test_ci_status.py。
目标：required_checks_succeeded(checks, required)只有非空、不重复、不含空字符串的required列表里每项都存在且值严格等于字符串success时返回True，其余返回False。
现有tests有5个基础用例，通过了正常和缺失情况；你应补失败、skipped、pending、neutral、unknown及重复required名的回归用例。
返回edits，每项path必须精确相对路径，expected_sha256使用下面原始文件给定的sha256，content为新的完整文件内容。不要删除文件。
不调用工具、不操作浏览器、不读凭据、不改配置、不提交。status done只表示提案已生成，实际写入和通过由外层证据决定。无需委派。
原始文件：
""" + json.dumps(snapshot, ensure_ascii=False)
    try:
        model = run_codex(shutil.which("codex") or "codex", worktree, directory / "codex", prompt, PATCH_IMPLEMENT_SCHEMA,
                          sandbox="read-only", timeout=180, skip_git=True)
        proposal = model["result"]
        application = apply_edits(worktree, proposal["edits"], allowed, []) if proposal["status"] == "done" else []
        (directory / "application.json").write_text(json.dumps(application, ensure_ascii=False, indent=2), encoding="utf-8")
        after = run_process(acceptance, worktree, directory / "after", timeout=30)
        unit = run_process([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                           worktree, directory / "unit", timeout=30)
        result = {"kind": "isolated_component_smoke", "before_returncode": before["returncode"],
                  "model_result": model["result"], "after_returncode": after["returncode"],
                  "unit_returncode": unit["returncode"], "git_actions": False,
                  "application": application,
                  "passed": before["returncode"] != 0 and bool(application) and after["returncode"] == 0 and unit["returncode"] == 0}
    except Exception as exc:
        result = {"kind": "isolated_component_smoke", "passed": False, "reason": str(exc), "git_actions": False}
    (directory / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**result, "evidence_dir": str(directory)}, ensure_ascii=False, indent=2))
