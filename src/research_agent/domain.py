from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from .settings import StrictModel

Dimension = Literal["benchmark", "competitor", "talent", "supplier"]


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: Any, length: int = 20) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:length]


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class Document(StrictModel):
    id: str = ""
    url: str
    title: str
    text: str
    source: str
    published_at: str | None = None
    fetched_at: str = Field(default_factory=utcnow)
    kind: Literal["snippet", "full_text", "metadata"] = "snippet"
    dimension: Dimension = "benchmark"
    entities: list[str] = Field(default_factory=list)

    def identified(self) -> Document:
        self.id = "doc_" + digest([self.url, normalized(self.text)])
        return self


class ResearchTask(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    dimension: Dimension
    goal: str = Field(min_length=4, max_length=500)
    entity: str = Field(default="", max_length=100)
    priority: int = Field(default=3, ge=1, le=5)
    reason: str = Field(default="", max_length=300)
    missing: list[str] = Field(default_factory=list, max_length=8)
    document_ids: list[str] = Field(default_factory=list, max_length=12)
    done_when: str = Field(min_length=3, max_length=500)
    status: Literal["pending", "running", "done", "deferred", "archived"] = "pending"
    attempts: int = Field(default=0, ge=0)
    retry_after: str | None = None


class Plan(StrictModel):
    tasks: list[ResearchTask] = Field(default_factory=list, max_length=8)
    summary: str = Field(default="", max_length=500)


class PriorityUpdate(StrictModel):
    task_id: str
    priority: int = Field(ge=1, le=5)
    reason: str = Field(min_length=3, max_length=240)


class Decision(StrictModel):
    action: str = Field(max_length=80)
    task_id: str = Field(default="", max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)
    add_tasks: list[ResearchTask] = Field(default_factory=list, max_length=2)
    reprioritize: list[PriorityUpdate] = Field(default_factory=list, max_length=4)


class Citation(StrictModel):
    document_id: str
    quote: str = Field(min_length=8, max_length=1200)
    stance: Literal["support", "contradict", "context"] = "support"


class ClaimAssessment(StrictModel):
    claim: str = Field(min_length=4, max_length=500)
    entity: str = Field(default="", max_length=100)
    verdict: Literal["support", "contradict", "unverified"]
    rationale: str = Field(min_length=3, max_length=600)
    citations: list[Citation] = Field(default_factory=list, max_length=6)
    confidence: float = Field(default=0.5, ge=0, le=1)
    resolved_gaps: list[str] = Field(default_factory=list, max_length=8)


class Finding(StrictModel):
    dimension: Dimension
    text: str = Field(min_length=3, max_length=1200)
    claim_ids: list[str] = Field(default_factory=list, max_length=8)
    inference: bool = False


class Recommendation(StrictModel):
    priority: Literal["P0", "P1", "P2"]
    action: str = Field(min_length=3, max_length=600)
    basis_claim_ids: list[str] = Field(default_factory=list, max_length=8)
    uncertainty: str = Field(default="", max_length=400)


class ReportDraft(StrictModel):
    headline: str = Field(min_length=3, max_length=200)
    findings: list[Finding] = Field(default_factory=list, max_length=16)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=12)


class ReviewIssue(StrictModel):
    dimension: str
    issue: str = Field(max_length=400)
    query: str = Field(default="", max_length=400)
    blocking: bool = True


class Review(StrictModel):
    factuality: int = Field(ge=0, le=10)
    completeness: int = Field(ge=0, le=10)
    citations: int = Field(ge=0, le=10)
    timeliness: int = Field(ge=0, le=10)
    actionability: int = Field(ge=0, le=10)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=5)
    summary: str = Field(max_length=500)
    notes: list[str] = Field(default_factory=list, max_length=5)

    @property
    def passed(self) -> bool:
        return min(self.factuality, self.completeness, self.citations,
                   self.timeliness, self.actionability) >= 7 and not any(issue.blocking for issue in self.issues)


class Observation(StrictModel):
    action: str
    task_id: str = ""
    status: Literal["ok", "error", "unavailable", "blocked"]
    summary: str
    document_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class RunState(StrictModel):
    run_id: str
    local_day: str
    mode: Literal["live", "demo"]
    prompt: str
    started_at: str = Field(default_factory=utcnow)
    phase: Literal["scan", "plan", "research", "report", "review", "done"] = "scan"
    coverage: dict[str, Any] = Field(default_factory=dict)
    candidate_ids: list[str] = Field(default_factory=list)
    candidate_scores: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    tasks: list[ResearchTask] = Field(default_factory=list)
    loaded_skills: list[str] = Field(default_factory=list)
    loaded_references: dict[str, str] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    actions: dict[str, int] = Field(default_factory=dict)
    steps: int = 0
    report_attempts: int = 0
    draft: ReportDraft | None = None
    review: Review | None = None
    warnings: list[str] = Field(default_factory=list)
    finish_reason: str = ""
