from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .harness import Harness
from .store import RunBusy

log = logging.getLogger(__name__)


async def scheduler_loop(harness: Harness):
    """One durable local-day key; catch up after sleep and resume interrupted work."""
    cfg = harness.settings.schedule
    while True:
        try:
            unfinished = [r for r in harness.store.runs(100) if r["status"] in {"running", "interrupted"}]
            if unfinished:
                await harness.run(resume_id=unfinished[-1]["id"])
            elif cfg.enabled:
                now = datetime.now(ZoneInfo(cfg.timezone))
                due = now.replace(hour=cfg.hour, minute=cfg.minute, second=0, microsecond=0)
                key = f"{harness.settings.mode}:{now.date().isoformat()}"
                if now >= due and not harness.store.daily_run(key):
                    state = await harness.run(scheduled=True)
                    log.info("Scheduled run finished: %s", state.run_id)
        except RunBusy:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Scheduler attempt failed: %s", type(exc).__name__)
        await asyncio.sleep(30)
