from __future__ import annotations

import os

from .demo import DemoModel, DemoSources
from .harness import Harness
from .providers import DeepSeekModel, Sources
from .settings import Settings, load_watchlist
from .store import Store


def create_harness(settings: Settings) -> Harness:
    data = settings.path(settings.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    (data / "samples").mkdir(exist_ok=True)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(data / "browsers"))
    os.environ.setdefault("HF_HOME", str(data / "hf"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    store = Store(data / f"agent-{settings.mode}.sqlite3", settings.schedule.timezone)
    if settings.mode == "demo":
        model, sources = DemoModel(), DemoSources()
    else:
        model, sources = DeepSeekModel(settings, store), Sources(settings, store)
    return Harness(settings, load_watchlist(), store, model, sources)
