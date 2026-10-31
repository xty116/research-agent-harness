from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(os.environ.get("AGENT_ROOT", Path(__file__).resolve().parents[2]))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMSettings(StrictModel):
    base_url: str = "https://api.deepseek.com"
    weak_model: str = "deepseek-chat"
    strong_model: str = "deepseek-reasoner"
    timeout_seconds: int = Field(default=90, ge=1, le=300)
    max_output_tokens: int = Field(default=3000, ge=256, le=10000)
    strong_max_output_tokens: int = Field(default=6500, ge=512, le=16000)
    reasoning_effort: Literal["low", "high", "max"] = "low"
    retries: int = Field(default=2, ge=0, le=4)
    input_usd_per_million: float = Field(default=1, gt=0)
    output_usd_per_million: float = Field(default=5, gt=0)


class BudgetSettings(StrictModel):
    per_run_usd: float = Field(default=.3, gt=0)
    per_day_usd: float = Field(default=.6, gt=0)
    report_reserve_usd: float = Field(default=.12, gt=0)
    search_credits_per_run: int = Field(default=16, ge=0)
    search_credits_per_day: int = Field(default=30, ge=0)
    max_steps: int = Field(default=24, ge=1, le=100)
    max_seconds: int = Field(default=600, ge=5, le=7200)
    max_repeated_action: int = Field(default=2, ge=1, le=3)

    @model_validator(mode="after")
    def reserves_fit(self):
        if self.report_reserve_usd >= self.per_run_usd:
            raise ValueError("report reserve must be below the run budget")
        return self


class ContextSettings(StrictModel):
    max_input_tokens: int = Field(default=24000, ge=3000)
    max_evidence_tokens: int = Field(default=4500, ge=200)
    max_memory_tokens: int = Field(default=1800, ge=100)
    max_skill_tokens: int = Field(default=2600, ge=100)
    recent_observations: int = Field(default=4, ge=1, le=10)


class MemorySettings(StrictModel):
    recall_limit: int = Field(default=8, ge=1, le=30)
    history_days: int = Field(default=30, ge=1)
    retry_after_hours: int = Field(default=24, ge=1)
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B-Q"
    embedding_enabled: bool = False
    embedding_cache: str = "data/models"


class SourceSettings(StrictModel):
    time_window_days: int = Field(default=7, ge=1, le=90)
    max_items_per_source: int = Field(default=6, ge=1, le=20)
    rss_feeds: list[str] = Field(default_factory=list)


class ScheduleSettings(StrictModel):
    enabled: bool = True
    timezone: str = "Australia/Sydney"
    hour: int = Field(default=8, ge=0, le=23)
    minute: int = Field(default=30, ge=0, le=59)


class WebSettings(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)


class Topic(StrictModel):
    dimension: Literal["benchmark", "competitor", "talent", "supplier"]
    weight: int = Field(ge=1, le=5)
    query: str = Field(min_length=3, max_length=400)


class Watchlist(StrictModel):
    daily_prompt: str
    topics: list[Topic]


class Settings(StrictModel):
    mode: Literal["live", "demo"] = "live"
    data_dir: str = "data"
    reports_dir: str = "reports"
    secrets_file: str = ""
    llm: LLMSettings = Field(default_factory=LLMSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    sources: SourceSettings = Field(default_factory=SourceSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    web: WebSettings = Field(default_factory=WebSettings)

    def path(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else ROOT / p


def load_settings(path: Path | None = None) -> Settings:
    local = ROOT / "config/settings.local.yaml"
    path = path or Path(os.environ.get("AGENT_CONFIG", local if local.exists() else ROOT / "config/settings.yaml"))
    settings = Settings.model_validate(yaml.safe_load(path.read_text()))
    values = dotenv_values(ROOT / ".env")
    secret_path = os.environ.get("AGENT_SECRETS_FILE") or values.get("AGENT_SECRETS_FILE") or settings.secrets_file
    secrets = dotenv_values(secret_path) if secret_path and Path(secret_path).is_file() else {}
    for key in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY", "GITHUB_TOKEN", "OPENALEX_API_KEY", "HF_TOKEN"):
        value = values.get(key) or secrets.get(key)
        if value:
            os.environ.setdefault(key, value)
    if os.environ.get("AGENT_MODE"):
        settings.mode = os.environ["AGENT_MODE"]
    return settings


def load_watchlist() -> Watchlist:
    return Watchlist.model_validate(yaml.safe_load((ROOT / "config/watchlist.yaml").read_text()))
