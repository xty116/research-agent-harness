from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .report_model import DIMENSIONS, KINDS, PHASES, REASONS, STATUS, human_issue, safe_url

ASSETS = Path(__file__).parent
environment = Environment(loader=FileSystemLoader(ASSETS / "templates"), autoescape=select_autoescape(["html", "xml"]),
                          trim_blocks=True, lstrip_blocks=True)


def timestamp(value, timezone="Australia/Sydney", pattern="%m-%d %H:%M"):
    if not value:
        return "未记录"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(ZoneInfo(timezone)).strftime(pattern)
    except (ValueError, TypeError):
        return str(value)[:30]


def duration(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} 分 {seconds:02} 秒"


environment.filters.update(timestamp=timestamp, duration=duration, safe_url=safe_url,
                           issue=human_issue, pretty=lambda v: json.dumps(v, ensure_ascii=False, indent=2),
                           money=lambda v: f"${float(v or 0):.4f}")
environment.globals.update(status_labels=STATUS, dimension_labels=DIMENSIONS, kind_labels=KINDS,
                           phase_labels=PHASES, reason_labels=REASONS)


def render(name: str, **context):
    return environment.get_template(name).render(**context)


def standalone_report(bundle: dict, timezone: str):
    css = (ASSETS / "static/app.css").read_text()
    return render("standalone.html", report=bundle, timezone=timezone, css=css)
