"""Explicit local control; no daemon installation or scheduled task changes."""
import argparse
import json
from pathlib import Path
import sys
import time

from .browser import ChromeBrowser
from .policy import evaluate_merge
from .state import StateStore


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Local single-repository Codex workflow")
    parser.add_argument("--config", type=Path, default=Path("config.local.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("task", type=Path)
    status = sub.add_parser("status")
    status.add_argument("task_id", nargs="?")
    for command in ("events", "pause", "resume"):
        child = sub.add_parser(command)
        child.add_argument("task_id")
        if command == "resume":
            child.add_argument("--renew-budget", action="store_true")
            child.add_argument("--by", default="")
            child.add_argument("--reason", default="")
    run = sub.add_parser("run")
    run.add_argument("--watch", action="store_true")
    run.add_argument("--interval", type=float, default=15)
    gate = sub.add_parser("gate")
    gate.add_argument("evidence", type=Path)
    issue = sub.add_parser("import-issue")
    issue.add_argument("url")
    issue.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load(args.config.resolve())
    runtime = Path(config["runtime_dir"]).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    store = StateStore(runtime / "tasks.sqlite3")
    if args.command == "enqueue":
        task = load(args.task)
        if not task.get("id") or not task.get("dedupe_key"):
            parser.error("task requires id and dedupe_key")
        payload = {k: v for k, v in task.items() if k not in ("id", "dedupe_key")}
        emit(store.enqueue(task["id"], payload, task["dedupe_key"]))
    elif args.command == "status":
        emit(store.get(args.task_id) if args.task_id else store.list())
    elif args.command == "events":
        emit(store.events(args.task_id))
    elif args.command == "pause":
        emit(store.pause(args.task_id))
    elif args.command == "resume":
        if args.renew_budget:
            if not args.by.strip() or not args.reason.strip():
                parser.error("Budget renewal requires --by and --reason")
            store.renew_budget(args.task_id, args.by, args.reason)
        emit(store.resume(args.task_id))
    elif args.command == "gate":
        result = evaluate_merge(config.get("merge_policy", {}), load(args.evidence))
        emit(result)
        return 0 if result["eligible"] else 2
    elif args.command == "import-issue":
        repo = config["repo_url"].rstrip("/")
        import re
        if not re.fullmatch(re.escape(repo) + r"/issues/[1-9][0-9]*", args.url):
            parser.error("Issue URL must belong to the configured repository")
        if args.output.exists():
            parser.error("Output exists; refusing overwrite")
        result = ChromeBrowser(config).read_issue({"repo_url": repo, "issue_url": args.url},
                     runtime / "imports" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        emit({"status": "imported_for_confirmation", "output": str(args.output), "enqueued": False})
    elif args.command == "run":
        from .runner import run_once
        if not 1 <= args.interval <= 3600:
            parser.error("interval must be between 1 and 3600 seconds")
        try:
            while True:
                emit(run_once(config, store, browser=ChromeBrowser(config)))
                if not args.watch:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            emit({"status": "stopped", "note": "Active child is stopped; checkpoint is retained"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
