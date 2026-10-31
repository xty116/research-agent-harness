from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import Field

from .context import ContextBuilder
from .domain import (
    Decision,
    Dimension,
    Observation,
    Plan,
    ReportDraft,
    ResearchTask,
    Review,
    RunState,
    digest,
)
from .errors import safe_error
from .memory import Memory
from .providers import Sources
from .reporting import fallback_draft, validate_report, write_report
from .settings import ROOT, Settings, StrictModel, Watchlist
from .skills import SkillRegistry
from .store import BudgetExceeded, Store
from .tools import SPECS, ToolExecutor, tool_catalog


class TriageItem(StrictModel):
    document_id: str
    relevant: bool
    importance: int = Field(ge=1, le=5)
    dimension: Dimension
    entities: list[str] = Field(default_factory=list, max_length=4)


class Triage(StrictModel):
    items: list[TriageItem] = Field(default_factory=list, max_length=24)


PLAN_INSTRUCTIONS = """You plan a daily intelligence investigation. Return JSON.
Task ids must be short ASCII strings such as t1. Keep each string concise; omit optional defaults.
Use only observed candidate IDs and dated historical context. Preserve four-direction baseline coverage.
Make at most four high-value tasks initially. Give each a checkable goal, exact missing fields,
business priority (5 highest), observed documents and an evidence completion condition.
Each task should test one atomic proposition with one or two missing fields, answerable from one or
two primary sources. When the model estimate budget is at most $0.30, prefer one or two initial tasks.
Do not turn a daily question into a full paper reproduction or exhaustive model comparison. Keep broad
historical questions open and create a distinct narrow subquestion rather than weakening their goals.
Prefer novel, actionable questions with resolvable gaps. Reuse a pending question when applicable.
Existing search results are discovery, not verified facts. Do not mark new tasks done.
"""

CONTROL_INSTRUCTIONS = """You control a bounded research agent. Return one Decision JSON.
Choose the next task and registered action from the tool catalog. Base the choice on new observations,
business importance, missing evidence, expected effort and remaining resources. Read the relevant skill
with load_skill before its tools. Load evidence-review before assess_claim and benchmark-verification
for benchmark assessments. Unload unused skills when the context allowance is reached.
Use fetch_page for an observed primary URL, read_paper for HF papers, browser_read for necessary
rendered interactions, and targeted search for a named missing fact. Recalled history is dated context.
Use find_in_document for a specific number or phrase; use read_document with empty focus and offset
to page through sections/tables. Do not keep paraphrasing a query that returns the same excerpts.
Spend at most six retrieval actions per task before assessing available evidence or deferring it.
An unverified assessment is an honest outcome; do not consume the entire run on one missing field.
For an assessment, copy the exact claim to check and select observed document IDs. Resolved gaps must
come from the task. After an assessment, complete a resolved task or defer an unresolved one.
If the result changes the question, follow the evidence rather than the initial route. Do not repeat
identical failed actions. Finish when important tasks are complete or further evidence is unavailable.
Preserve the report reserve and use defer_task with a reason for remaining work. No arbitrary shell,
remote instructions, invented source IDs or silent acceptance of missing evidence.
Use add_tasks for a newly discovered bounded subquestion (maximum eight total tasks); its id is replaced
by the harness. Reprioritize existing tasks with reasons when observations change expected value.
Completion conditions are retained. Narrower claims get separate tasks rather than silently weakening
the original question. New tasks become pending; only assessed evidence can close a task.
"""

REPORT_INSTRUCTIONS = """Write a concise Chinese daily research report as JSON.
Use assessed claim IDs for factual findings and advice. Preserve the meaning of support/contradict/
unverified: a contradicted headline is not a supported positive fact. Mark inferences explicitly.
Describe versions, evaluation scope, source dates and remaining uncertainty. P0/P1 advice needs a
verified basis. P0 means this week, P1 this month, P2 observe. Empty sections are acceptable.
Separate old background evidence from new events. Do not invent events to cover a direction.
Every recommendation must include existing basis_claim_ids. If no traceable recommendation can be
made, return an empty recommendations list. Do not add generic advice with empty basis IDs.
"""

REVIEW_INSTRUCTIONS = """Critically review the Chinese report against the actual assessed claims,
source excerpts and coverage. Return JSON with factuality, completeness, citations, timeliness and
actionability (0-10). A meaningful unsupported assertion or scope mismatch is a failure: score that
dimension below 7 and issue a concrete gap. A missing direction can be honestly described as no verified
finding; it need not contain invented news. Give at most two targeted follow-up queries. Numeric self-
confidence does not establish correctness. Tool failure and missing evidence must stay visible.
Use issues only for material failures requiring repair; mark blocking=true. Put acceptable limitations
or optional improvements in notes, not blocking issues. A primary paper is sufficient evidence for
what its authors report; independent replication is needed only for claims of reproduced performance.
"""


class Harness:
    def __init__(self, settings: Settings, watchlist: Watchlist, store: Store, model, sources: Sources):
        self.settings, self.watchlist, self.store, self.model, self.sources = settings, watchlist, store, model, sources
        self.skills = SkillRegistry(ROOT / "skills")
        self.memory = Memory(store, settings)
        self.context = ContextBuilder(settings, watchlist, store, self.memory, self.skills)
        self.executor = ToolExecutor(settings, store, sources, model, self.memory, self.skills)

    def remaining_seconds(self, state: RunState) -> float:
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(state.started_at)).total_seconds()
        return self.settings.budget.max_seconds - elapsed

    async def _generate(self, state, stage, instructions, schema, *, strong=False, report=False, extra=None):
        context = await asyncio.to_thread(self.context.build, state, tool_catalog(), stage, extra)
        # IDs disclosed by memory are observed evidence too, not fabricated references.
        disclosed = [r["id"] for r in context["historical_memory"]]
        disclosed += [c["document_id"] for row in context["historical_conclusions"] for c in row["citations"]]
        disclosed += [doc_id for task in context["pending_questions"] for doc_id in task["document_ids"]]
        for doc in self.store.documents(list(dict.fromkeys(disclosed))):
            if doc.id not in state.evidence_ids + state.candidate_ids:
                state.evidence_ids.append(doc.id)
                self.store.save_document(state.run_id, doc)
        self.store.checkpoint(state)
        self.store.event(state.run_id, "context", {"stage": stage, **context["context_accounting"]})
        timeout = min(self.settings.llm.timeout_seconds * (self.settings.llm.retries + 1) + 10,
                      max(1, self.remaining_seconds(state)))
        async with asyncio.timeout(timeout):
            return await self.model.generate(state, stage, instructions, context, schema, strong=strong, report=report)

    async def run(self, resume_id: str | None = None, scheduled: bool = False) -> RunState:
        day = datetime.now(ZoneInfo(self.settings.schedule.timezone)).date().isoformat()
        lease_name = "research:" + self.settings.mode
        owner = uuid.uuid4().hex
        self.store.acquire(lease_name, owner)
        heartbeat = asyncio.create_task(self._heartbeat(lease_name, owner))
        state = None
        try:
            if resume_id:
                row = self.store.get_run(resume_id)
                if not row or row["status"] not in {"running", "interrupted"}:
                    raise ValueError("Only unfinished runs can resume")
                state = RunState.model_validate_json(row["checkpoint"])
                if state.mode != self.settings.mode:
                    raise ValueError("Run mode mismatch")
                self.store.event(state.run_id, "resumed", {"phase": state.phase, "steps": state.steps})
            else:
                key = f"{self.settings.mode}:{day}" if scheduled else None
                if key and self.store.daily_run(key):
                    raise ValueError("This local day's scheduled run already exists")
                state = RunState(run_id=uuid.uuid4().hex[:16], local_day=day, mode=self.settings.mode,
                                 prompt=self.watchlist.daily_prompt, loaded_skills=["research-planning"])
                self.store.create_run(state, key)
                self.store.event(state.run_id, "started", {"mode": state.mode, "daily_key": key})
            try:
                await self._drive(state)
            except asyncio.CancelledError:
                self.store.checkpoint(state)
                self.store.finish(state, "interrupted", error="Process stopped; checkpoint can resume")
                try:
                    path = write_report(state, self.settings, self.store, "interrupted")
                    self.store.attach_report(state.run_id, str(path))
                except Exception as report_error:
                    self.store.event(state.run_id, "report_export_error", {"error": safe_error(report_error)})
                raise
            except Exception as exc:
                state.warnings.append(f"Execution interrupted: {safe_error(exc)}")
                state.finish_reason = "execution_error"
                self.store.event(state.run_id, "error", {"type": type(exc).__name__, "message": safe_error(exc)})
                await self._finalize(state, force_partial=True, allow_model=False)
            return state
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self.store.release(lease_name, owner)

    async def _heartbeat(self, name, owner):
        while True:
            await asyncio.sleep(20)
            self.store.renew(name, owner)

    async def _drive(self, state):
        if state.phase == "scan":
            await self._scan(state)
            await self._triage(state)
            state.phase = "plan"
            self.store.checkpoint(state)
        if state.phase == "plan":
            plan = await self._generate(state, "plan", PLAN_INSTRUCTIONS, Plan, strong=True)
            known = set(state.candidate_ids + state.evidence_ids)
            for task in plan.tasks:
                if not set(task.document_ids) <= known:
                    raise ValueError("Plan referenced unobserved evidence IDs: " + ",".join(sorted(set(task.document_ids) - known))[:160])
                task.id = "task_" + digest([task.dimension, task.entity.casefold(), task.goal.casefold()])
                task.status = "pending"
                task.attempts = 0
                task.retry_after = None
                self.store.save_task(state.run_id, task)
            state.tasks = plan.tasks
            self.store.event(state.run_id, "plan", plan.model_dump())
            state.phase = "research"
            self.store.checkpoint(state)
        if state.phase == "research":
            await self._research(state)
            state.phase = "report"
            self.store.checkpoint(state)
        await self._finalize(state)

    async def _scan(self, state):
        since = (date.fromisoformat(state.local_day) - timedelta(days=self.settings.sources.time_window_days)).isoformat()

        async def source(name, call, dimension):
            if name in state.coverage and state.coverage[name].get("status") == "ok":
                return
            try:
                async with asyncio.timeout(45):
                    docs = await call()
                kept = []
                dropped = 0
                for doc in docs:
                    if doc.published_at and not since <= doc.published_at[:10] <= state.local_day:
                        dropped += 1
                        continue
                    if dimension:
                        doc.dimension = dimension
                    saved = self.store.save_document(state.run_id, doc)
                    kept.append(saved.id)
                state.candidate_ids = list(dict.fromkeys(state.candidate_ids + kept))
                failures = getattr(docs, "failures", [])
                state.coverage[name] = {"status": "partial" if failures else "ok", "items": len(kept), "dropped_by_date": dropped,
                                        "partial_failures": failures,
                                        "document_ids": kept}
                if failures:
                    state.warnings.append(f"Source {name} partially failed: {', '.join(failures)}")
            except Exception as exc:
                state.coverage[name] = {"status": "error", "error": safe_error(exc)}
                state.warnings.append(f"Source {name} failed ({type(exc).__name__})")
            self.store.event(state.run_id, "source_scan", {"source": name, **state.coverage[name]})
            self.store.checkpoint(state)

        # Bounded fan-out. Mandatory discovery coverage; subsequent investigation is model-directed.
        sem = asyncio.Semaphore(3)

        async def bounded(name, call, dim=None):
            async with sem:
                await source(name, call, dim)

        jobs = []
        for topic in self.watchlist.topics:
            jobs.append(bounded("web:" + topic.dimension,
                                lambda t=topic: self.sources.web_search(state, t.query,
                                                                       days=self.settings.sources.time_window_days),
                                topic.dimension))
        jobs += [bounded("papers", lambda: self.sources.papers("LLM benchmark evaluation dataset", since)),
                 bounded("github", lambda: self.sources.github(f"LLM benchmark created:>{since}"), "competitor"),
                 bounded("huggingface", self.sources.huggingface), bounded("rss", self.sources.rss)]
        await asyncio.gather(*jobs)
        # Preserve direction diversity when bounding the initial triage context.
        docs = self.store.documents(state.candidate_ids)
        ordered = []
        groups = [[d.id for d in docs if d.dimension == dimension] for dimension in
                  ("benchmark", "competitor", "talent", "supplier")]
        for index in range(max((len(g) for g in groups), default=0)):
            for group in groups:
                if index < len(group):
                    ordered.append(group[index])
        if len(ordered) > 24:
            state.warnings.append(f"Initial triage bounded to 24 of {len(ordered)} candidates; others remain archived")
        state.candidate_ids = ordered[:24]

    async def _triage(self, state):
        docs = self.store.documents(state.candidate_ids)
        if not docs:
            return
        context = {"date": state.local_day,
                   "topics": [t.model_dump() for t in self.watchlist.topics],
                   "documents": [{"id": d.id, "title": d.title[:150], "text": d.text[:160],
                                  "date": d.published_at, "source": d.source} for d in docs]}
        result = await self.model.generate(state, "triage", "Classify research relevance, business importance "
                                           "(1-5), dimension and public entities. Return JSON for each observed id.",
                                           context, Triage)
        known = {d.id for d in docs}
        judged = {r.document_id: r for r in result.items if r.document_id in known}
        state.candidate_scores = {key: row.model_dump(exclude={"document_id"}) for key, row in judged.items()}
        state.candidate_ids = [i for i in state.candidate_ids if i not in judged or judged[i].relevant]
        state.candidate_ids.sort(key=lambda i: -judged[i].importance if i in judged else -3)
        self.store.event(state.run_id, "triage", {"kept": len(state.candidate_ids), "judged": len(judged)})

    async def _research(self, state):
        while state.steps < self.settings.budget.max_steps:
            if self.remaining_seconds(state) < 55:
                state.finish_reason = "time_reserve"
                break
            usage = self.store.usage(run_id=state.run_id, mode=state.mode)
            if usage["usd"] >= self.settings.budget.per_run_usd - self.settings.budget.report_reserve_usd:
                state.finish_reason = "budget_reserve"
                break
            try:
                decision = await self._generate(state, "controller", CONTROL_INSTRUCTIONS, Decision, strong=True)
            except BudgetExceeded:
                state.finish_reason = "budget_reserve"
                break
            fingerprint = digest([decision.action, decision.task_id, decision.arguments])
            count = state.actions.get(fingerprint, 0) + 1
            state.actions[fingerprint] = count
            state.steps += 1
            self.store.event(state.run_id, "decision", decision.model_dump())
            self.store.checkpoint(state)  # dispatch intent durable before any external call
            if count > self.settings.budget.max_repeated_action:
                observation = Observation(action=decision.action, task_id=decision.task_id, status="blocked",
                                          summary="Identical action repetition limit reached; choose another path or defer")
            else:
                try:
                    self._apply_plan_updates(state, decision)
                    if decision.action not in SPECS:
                        raise ValueError("Unknown action")
                    retrieval = {"read_document", "find_in_document", "fetch_page", "read_paper", "browser_read",
                                 "search_web", "search_papers", "search_github", "memory_search"}
                    attempts = sum(o.task_id == decision.task_id and o.action in retrieval
                                   for o in state.observations)
                    if decision.task_id and decision.action in retrieval and attempts >= 6:
                        raise ValueError("Task retrieval limit reached. Assess available evidence, defer the task, or select another task.")
                    timeout = min(70, max(1, self.remaining_seconds(state) - 45))
                    async with asyncio.timeout(timeout):
                        observation = await self.executor.execute(state, decision.action, decision.arguments,
                                                                   decision.task_id)
                except BudgetExceeded as exc:
                    observation = Observation(action=decision.action, task_id=decision.task_id,
                                              status="blocked", summary=str(exc))
                except Exception as exc:
                    observation = Observation(action=decision.action, task_id=decision.task_id, status="error",
                                              summary=safe_error(exc))
            state.observations.append(observation)
            for task in state.tasks:
                if task.id == decision.task_id:
                    task.attempts += 1
                self.store.save_task(state.run_id, task)
            self.store.event(state.run_id, "observation", observation.model_dump())
            self.store.checkpoint(state)
            if decision.action == "finish" and observation.status == "ok":
                break
        else:
            state.finish_reason = "step_limit"

    def _apply_plan_updates(self, state, decision):
        known = set(state.candidate_ids + state.evidence_ids)
        if any(not any(t.id == u.task_id for t in state.tasks) for u in decision.reprioritize):
            raise ValueError("Priority update references an unknown task")
        for task in decision.add_tasks:
            task.id = "task_" + digest([task.dimension, task.entity.casefold(), task.goal.casefold()])
        new = [t for t in decision.add_tasks if not any(t.id == old.id for old in state.tasks)]
        if len(state.tasks) + len({t.id for t in new}) > 8 or any(not set(t.document_ids) <= known for t in new):
            raise ValueError("New task exceeds the task limit or cites unknown evidence")
        for update in decision.reprioritize:
            task = next((t for t in state.tasks if t.id == update.task_id), None)
            if task is None:
                raise ValueError("Priority update references an unknown task")
            before = task.priority
            task.priority, task.reason = update.priority, update.reason
            self.store.event(state.run_id, "priority_changed", {"task_id": task.id,
                             "before": before, "after": task.priority, "reason": update.reason})
        for task in decision.add_tasks:
            task.id = "task_" + digest([task.dimension, task.entity.casefold(), task.goal.casefold()])
            if any(t.id == task.id for t in state.tasks):
                continue
            if len(state.tasks) >= 8 or not set(task.document_ids) <= known:
                raise ValueError("New task exceeds the task limit or cites unknown evidence")
            task.status, task.attempts, task.retry_after = "pending", 0, None
            self.store.save_task(state.run_id, task)
            state.tasks.append(task)
            self.store.event(state.run_id, "task_added", task.model_dump())

    async def _finalize(self, state, force_partial=False, allow_model=True):
        for task in state.tasks:
            if task.status not in {"done", "deferred"}:
                self.store.defer_task(state.run_id, task, self.settings.memory.retry_after_hours)
        try:
            if allow_model and self.remaining_seconds(state) > 5:
                state.phase = "report"
                state.report_attempts += 1
                state.draft = await self._generate(state, "report", REPORT_INSTRUCTIONS, ReportDraft,
                                                   strong=True, report=True)
                self.store.event(state.run_id, "report_draft", state.draft.model_dump())
                errors = validate_report(state.draft, state, self.store)
                if errors:
                    state.warnings.extend(errors)
                    state.draft = fallback_draft(state, self.store)
                    force_partial = True
                state.phase = "review"
                self.store.checkpoint(state)
                state.review = await self._generate(state, "critic", REVIEW_INSTRUCTIONS, Review,
                                                    strong=True, report=True,
                                                    extra={"draft": state.draft.model_dump()})
                # One bounded quality repair, chosen by the same controller.
                if not state.review.passed and state.report_attempts < 2 and self.remaining_seconds(state) > 80:
                    for issue in [i for i in state.review.issues if i.blocking][:2]:
                        if issue.query and len(state.tasks) < 8:
                            dimension = issue.dimension if issue.dimension in {"benchmark", "competitor", "talent", "supplier"} else "benchmark"
                            task = ResearchTask(id="task_" + digest(issue.query), dimension=dimension,
                                                goal=issue.issue, priority=4, reason=("Critic follow-up query: " + issue.query)[:300],
                                                missing=[issue.issue], done_when="Resolve the named review gap with quoted evidence")
                            self.store.save_task(state.run_id, task)
                            state.tasks.append(task)
                    state.warnings.append("Critic requested targeted repair")
                    state.phase = "research"
                    self.store.checkpoint(state)
                    await self._research(state)
                    await self._finalize(state, force_partial=force_partial)
                    return
        except Exception as exc:
            state.warnings.append(f"Report/review incomplete: {safe_error(exc)}")
            force_partial = True
        if state.draft is None:
            state.draft = fallback_draft(state, self.store)
            force_partial = True
        failed_sources = any(v.get("status") != "ok" for v in state.coverage.values()) or len(state.coverage) < 8
        unresolved = any(t.status != "done" for t in state.tasks)
        passed = state.review is not None and state.review.passed
        errors = validate_report(state.draft, state, self.store)
        state.warnings.extend(e for e in errors if e not in state.warnings)
        status = "complete" if passed and not (force_partial or failed_sources or unresolved or errors) else "partial"
        state.phase = "done"
        if not state.finish_reason:
            state.finish_reason = "research_finished"
        path = write_report(state, self.settings, self.store, status)
        self.store.event(state.run_id, "finished", {"status": status, "report": str(path), "steps": state.steps})
        self.store.finish(state, status, str(path))
        await asyncio.to_thread(self.memory.index, self.store.documents(state.evidence_ids))
