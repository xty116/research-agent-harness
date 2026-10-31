from pathlib import Path

import pytest

from research_agent.demo import DemoModel, DemoSources
from research_agent.harness import Harness
from research_agent.settings import Settings, load_watchlist
from research_agent.store import Store


@pytest.fixture
def settings(tmp_path: Path):
    s = Settings(mode="demo", data_dir=str(tmp_path / "data"), reports_dir=str(tmp_path / "reports"))
    s.schedule.enabled = False
    s.schedule.timezone = "UTC"
    return s


@pytest.fixture
def store(settings):
    s = Store(settings.path(settings.data_dir) / "test.sqlite3", "UTC")
    yield s
    s.close()


@pytest.fixture
def harness(settings, store):
    return Harness(settings, load_watchlist(), store, DemoModel(), DemoSources())
