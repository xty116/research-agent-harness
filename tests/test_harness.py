import asyncio
from pathlib import Path

import pytest

from research_agent.demo import HEADLINE, DemoModel, DemoSources
from research_agent.domain import Decision
from research_agent.harness import Harness
from research_agent.settings import load_watchlist
from research_agent.store import BudgetExceeded, RunBusy


async def test_complete_trace_keeps_contradiction_and_source(harness, store):
    state = await harness.run()
    row = store.get_run(state.run_id)
    assert row["status"] == "complete"
    claims = store.claims(state.claim_ids)
    assert claims[0]["payload"]["verdict"] == "contradict"
    assert claims[0]["payload"]["claim"] == HEADLINE
    assert all(t.status == "done" for t in state.tasks)
    actions = [e["payload"]["action"] for e in store.events(state.run_id) if e["kind"] == "decision"]
    assert actions.index("fetch_page") < actions.index("assess_claim") < actions.index("complete_task")
    report = Path(row["report_path"]).read_text()
    assert "离线演示" in report and "open-model subset" in report and state.claim_ids[0] in report
    assert len(state.coverage) == 8


class BrokenReview(DemoModel):
    async def generate(self, state, stage, *args, **kwargs):
        if stage == "critic":
            raise ValueError("broken JSON")
        return await super().generate(state, stage, *args, **kwargs)


async def test_broken_critic_cannot_turn_into_pass(harness, store):
    harness.model = BrokenReview()
    state = await harness.run()
    assert store.get_run(state.run_id)["status"] == "partial"
    assert state.review is None
    assert any("Report/review incomplete" in w for w in state.warnings)


class CorruptQuote(DemoModel):
    async def generate(self, state, stage, *args, **kwargs):
        result = await super().generate(state, stage, *args, **kwargs)
        if stage in {"claim_assessment", "conflict_adjudication"}:
            result.citations[0].quote = "A completely fabricated quote that never appeared in this source."
        return result


async def test_invented_quote_never_becomes_persisted_verified_fact(harness, store):
    harness.model = CorruptQuote()
    harness.executor.model = harness.model
    state = await harness.run()
    assert state.claim_ids == []
    assert store.get_run(state.run_id)["status"] == "partial"
    assert state.steps <= harness.settings.budget.max_steps
    assert any("quote" in o.summary.lower() for o in state.observations)


class CancelOnce(DemoModel):
    cancelled = False

    async def generate(self, state, stage, *args, **kwargs):
        if stage == "controller" and not self.cancelled:
            self.cancelled = True
            raise asyncio.CancelledError()
        return await super().generate(state, stage, *args, **kwargs)


async def test_resume_keeps_scan_and_plan(harness, store):
    harness.model = CancelOnce()
    harness.executor.model = harness.model
    with pytest.raises(asyncio.CancelledError):
        await harness.run()
    interrupted = store.runs()[0]
    assert interrupted["status"] == "interrupted"
    assert Path(interrupted["report_path"]).with_suffix(".html").is_file()
    assert '"status": "interrupted"' in Path(interrupted["report_path"]).with_suffix(".report.json").read_text()
    scans = len([e for e in store.events(interrupted["id"]) if e["kind"] == "source_scan"])
    state = await harness.run(resume_id=interrupted["id"])
    assert store.get_run(state.run_id)["status"] == "complete"
    assert len([e for e in store.events(state.run_id) if e["kind"] == "source_scan"]) == scans
    assert len(store.claims(state.claim_ids)) == 1


async def test_daily_schedule_is_idempotent(harness):
    await harness.run(scheduled=True)
    with pytest.raises(ValueError, match="already exists"):
        await harness.run(scheduled=True)


async def test_only_one_executor_can_run(harness, store):
    store.acquire("research:demo", "other-owner")
    with pytest.raises(RunBusy):
        await harness.run()


class ExhaustedController(DemoModel):
    async def generate(self, state, stage, *args, **kwargs):
        if stage == "controller":
            raise BudgetExceeded("test budget exhausted")
        return await super().generate(state, stage, *args, **kwargs)


async def test_budget_stop_preserves_unresolved_task(harness, store):
    harness.model = ExhaustedController()
    state = await harness.run()
    assert state.finish_reason == "budget_reserve"
    assert state.tasks[0].status == "deferred"
    assert store.recall_tasks(include_not_due=True)[0].retry_after
    assert store.get_run(state.run_id)["status"] == "partial"


class NewQuestion(DemoModel):
    async def generate(self, state, stage, *args, **kwargs):
        result = await super().generate(state, stage, *args, **kwargs)
        if stage == "controller" and not state.steps:
            return Decision.model_validate({**result.model_dump(),
                "reprioritize": [{"task_id": state.tasks[0].id, "priority": 4,
                                  "reason": "New source changes expected investigation effort"}],
                "add_tasks": [{"id": "temporary", "dimension": "competitor", "goal": "Check a newly discovered model-version question",
                               "entity": "Model A", "priority": 3, "missing": ["version"],
                               "done_when": "Establish the exact model revision"}]})
        return result


async def test_observation_driven_plan_changes_are_durable(harness, store):
    harness.model = NewQuestion()
    state = await harness.run()
    assert len(state.tasks) == 2
    assert state.tasks[0].priority == 4
    assert state.tasks[1].status == "deferred"
    kinds = {e["kind"] for e in store.events(state.run_id)}
    assert {"task_added", "priority_changed"} <= kinds


class FailedSources(DemoSources):
    async def web_search(self, state, query, **kwargs):
        raise RuntimeError("upstream down")


async def test_empty_or_failed_sources_do_not_produce_complete_report(settings, store):
    h = Harness(settings, load_watchlist(), store, DemoModel(), FailedSources())
    state = await h.run()
    assert store.get_run(state.run_id)["status"] == "partial"
    assert any(v["status"] == "error" for v in state.coverage.values())
