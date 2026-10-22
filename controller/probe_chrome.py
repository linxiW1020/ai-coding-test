"""Read-only proof that a separately launched codex exec can use local Chrome."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

from workflow.process import run_codex
from workflow.browser import chrome_trace

ROOT = Path(__file__).resolve().parent
RUNTIME = Path(json.loads((ROOT / "config.local.json").read_text(encoding="utf-8")).get("runtime_dir")) if (ROOT / "config.local.json").is_file() else ROOT / ".runtime"
SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"status": {"type": "string", "enum": ["passed", "blocked", "failed"]},
                         "browser": {"type": "string"}, "url": {"type": "string"},
                         "title": {"type": "string"}, "observed_text": {"type": "string"},
                         "limitation": {"type": "string"}},
          "required": ["status", "browser", "url", "title", "observed_text", "limitation"]}
PROMPT = """这是一次已获授权的只读 Chrome 能力预检。你是本地调度器独立启动的 codex exec 子进程。
必须通过 mcp cua_repl 使用本地 Chrome（名称 chrome），不要使用内置浏览器、web搜索、curl、Playwright独立浏览器或读取浏览器文件。
首次调用严格只执行：var wfTab = await cua.createBrowserTab("chrome", "https://learn.chatgpt.com/docs/non-interactive-mode", {"sessionName":"🔎 Codex自动调用预检"});
读取返回的真实页面，确认标题和正文中 codex exec 的用途。若首次失败，按工具返回指导仅重试一次同一Chrome入口，不换通道绕过限制。
只读代理只允许这些完整代码：await wfTab.getAXState(); 或 await wfTab.getScreenshot(); 或 await wfTab.close();
不修改账号、权限、配置，不提交任何表单。完成后关闭自己创建的标签页。
返回JSON。只有真实打开Chrome、读取指定页面正文后才能status passed。不可用就blocked并说明具体限制，不能根据记忆填passed。
url应是实际最终URL，observed_text用自己的话简述可见内容，不超过30个英文词。browser填Chrome。
无需委派、不运行shell、不读取其他资料。"""

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    directory = RUNTIME / "probes" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    try:
        receipt = run_codex(shutil.which("codex") or "codex", ROOT, directory, PROMPT, SCHEMA,
                            timeout=args.timeout, skip_git=True, role="browser-read",
                            browser_target="https://learn.chatgpt.com/docs/non-interactive-mode")
        result = receipt["result"]
        trace = chrome_trace(receipt, ["https://learn.chatgpt.com/docs/non-interactive-mode", "codex exec"])
        (directory / "chrome-trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        if result["status"] == "passed" and not trace["verified"]:
            result = {**result, "status": "blocked", "limitation": "No matching returned Chrome page evidence"}
        print(json.dumps({"evidence_dir": str(directory), **result}, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc), "evidence_dir": str(directory)}, ensure_ascii=False))
        raise SystemExit(1)
