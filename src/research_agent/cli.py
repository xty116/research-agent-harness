from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path

from .application import create_harness
from .settings import load_settings


def main():
    parser = argparse.ArgumentParser(description="Resumable daily research agent")
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run one daily investigation")
    run.add_argument("--demo", action="store_true")
    run.add_argument("--resume")
    run.add_argument("--scheduled", action="store_true")
    run.add_argument("--fail-on-partial", action="store_true", help="Exit with code 2 if the report is partial")
    serve = sub.add_parser("serve", help="Serve the read-only dashboard and daily scheduler")
    serve.add_argument("--demo", action="store_true")
    sub.add_parser("doctor", help="Check local configuration without paid requests")
    repair = sub.add_parser("repair-reports", help="Generate missing report artifacts without calling models")
    repair.add_argument("--force", action="store_true")
    repair.add_argument("--demo", action="store_true")
    sub.add_parser("evaluate", help="Run four synthetic labeled evidence cases against the real model; paid usage is budgeted")
    archive = sub.add_parser("archive-task", help="Administratively retire a question without claiming it was resolved")
    archive.add_argument("task_id")
    archive.add_argument("--reason", required=True)
    replay = sub.add_parser("replay", help="Inspect persisted decisions without executing tools")
    replay.add_argument("run_id")
    replay.add_argument("--demo", action="store_true")
    backup = sub.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    settings = load_settings(args.config)
    if getattr(args, "demo", False):
        settings.mode = "demo"
        settings.schedule.enabled = False
    if args.command == "serve":
        import uvicorn

        from .logging_config import configure_logging
        from .web import create_app
        configure_logging(settings)
        uvicorn.run(create_app(settings), host=settings.web.host, port=settings.web.port,
                    log_config=None, log_level="info", access_log=False)
        return

    async def execute():
        h = create_harness(settings)
        try:
            if args.command == "run":
                state = await h.run(resume_id=args.resume, scheduled=args.scheduled)
                row = h.store.get_run(state.run_id)
                print(json.dumps({"run_id": state.run_id, "status": row["status"], "report": row["report_path"],
                                  "steps": state.steps, "usage": h.store.usage(run_id=state.run_id, mode=state.mode)}, ensure_ascii=False))
                if args.fail_on_partial and row["status"] != "complete":
                    raise SystemExit(2)
            elif args.command == "doctor":
                checks = {"mode": settings.mode, "database": h.store.db.execute("PRAGMA quick_check").fetchone()[0],
                          "configured_credentials": {k: bool(os.environ.get(k)) for k in
                                                     ("DEEPSEEK_API_KEY", "TAVILY_API_KEY", "OPENALEX_API_KEY")},
                          "models": {"weak": settings.llm.weak_model, "strong": settings.llm.strong_model},
                          "skills": [s["name"] for s in h.skills.catalog()],
                          "browser_package": importlib.util.find_spec("playwright") is not None,
                          "embedding_enabled": settings.memory.embedding_enabled,
                          "schedule": settings.schedule.model_dump(), "budget": settings.budget.model_dump()}
                print(json.dumps(checks, ensure_ascii=False, indent=2))
            elif args.command == "evaluate":
                from .evaluation import evaluate
                result = await evaluate(h)
                print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False, indent=2))
                if result["passed"] != result["total"]:
                    raise SystemExit(2)
            elif args.command == "replay":
                print(json.dumps(h.store.events(args.run_id, limit=1000), ensure_ascii=False, indent=2))
            elif args.command == "backup":
                h.store.backup(args.destination)
                print(str(args.destination))
            elif args.command == "repair-reports":
                from .reporting import backfill_reports
                result = backfill_reports(settings, h.store, force=args.force)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if result["failed"]:
                    raise SystemExit(2)
            elif args.command == "archive-task":
                h.store.archive_task(args.task_id, args.reason)
                print(json.dumps({"task_id": args.task_id, "status": "archived"}))
        finally:
            await h.model.close()
            h.store.close()

    asyncio.run(execute())


if __name__ == "__main__":
    main()
