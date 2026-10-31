from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from research_agent.context import ContextBuilder
from research_agent.domain import ClaimAssessment, Document, ResearchTask, RunState
from research_agent.memory import Memory
from research_agent.settings import ROOT, BudgetSettings, load_watchlist
from research_agent.skills import SkillRegistry
from research_agent.store import BudgetExceeded


def state(store, name="run1"):
    s = RunState(run_id=name, mode="demo", local_day=store.billing_day(), prompt="fixed daily task")
    store.create_run(s)
    return s


def test_concurrent_reservations_cannot_overspend(store):
    s = state(store)
    limits = BudgetSettings(per_run_usd=.15, per_day_usd=.30, report_reserve_usd=.02)

    def reserve(_):
        try:
            return store.reserve(s, "model", .06, 0, limits)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = [x for x in pool.map(reserve, range(10)) if x]
    assert len(accepted) == 2
    assert store.usage(run_id=s.run_id, mode="demo")["usd"] == .12


def test_reservations_survive_reopen_and_reserve_report_funds(store):
    s = state(store)
    limits = BudgetSettings(per_run_usd=.15, per_day_usd=.2, report_reserve_usd=.03)
    reservation = store.reserve(s, "research", .11, 0, limits)
    with pytest.raises(BudgetExceeded):
        store.reserve(s, "research", .02, 0, limits)
    store.reserve(s, "report", .03, 0, limits, report=True)
    assert store.usage(run_id=s.run_id, mode="demo")["usd"] == .14
    from research_agent.store import Store
    other = Store(store.path)
    try:
        assert other.usage(run_id=s.run_id, mode="demo")["usd"] == .14
        store.settle(reservation, .01)
        assert other.usage(run_id=s.run_id, mode="demo")["usd"] == .04
    finally:
        other.close()


def test_daily_budget_counts_across_runs_and_uses_request_day(store, monkeypatch):
    a, b = state(store, "a"), state(store, "b")
    limits = BudgetSettings(per_run_usd=.2, per_day_usd=.15, report_reserve_usd=.01)
    monkeypatch.setattr(store, "billing_day", lambda: "2030-01-01")
    store.reserve(a, "a", .1, 0, limits)
    with pytest.raises(BudgetExceeded):
        store.reserve(b, "b", .1, 0, limits)
    monkeypatch.setattr(store, "billing_day", lambda: "2030-01-02")
    store.reserve(b, "b", .1, 0, limits)
    assert store.usage(day="2030-01-02", mode="demo")["usd"] == .1


def test_changed_pages_and_claim_revisions_are_preserved(store):
    s = state(store)
    one = store.save_document(s.run_id, Document(url="https://example.com/x", title="A", text="Earlier result",
                                                source="test"))
    two = store.save_document(s.run_id, Document(url=one.url, title="A", text="Updated result", source="test"))
    assert one.id != two.id
    first = ClaimAssessment(claim="Model A leads the benchmark", entity="Model A", verdict="unverified",
                            rationale="No original result")
    old = store.save_claim(s.run_id, "t1", first)
    new = store.save_claim(s.run_id, "t1", first.model_copy(update={"rationale": "Still missing version"}))
    assert len(store.claims()) == 2
    assert store.latest_claims()[0]["id"] == new
    assert store.claims([new, old])[0]["id"] == new


def test_new_evidence_can_surface_not_yet_due_question(store, settings):
    old = state(store, "old")
    task = ResearchTask(id="open1", dimension="benchmark", goal="Verify Model A rank", entity="Model A",
                        missing=["scope"], done_when="Scope established")
    store.defer_task(old.run_id, task, 48)
    assert store.recall_tasks() == []
    current = state(store, "current")
    doc = store.save_document(current.run_id, Document(url="https://example.com/new", title="Model A official results",
                                                       text="New official information", source="test"))
    current.candidate_ids = [doc.id]
    ctx = ContextBuilder(settings, load_watchlist(), store, Memory(store, settings), SkillRegistry(ROOT / "skills"))
    payload = ctx.build(current, [], "plan")
    assert payload["pending_questions"][0]["id"] == task.id


def test_memory_records_history_as_dated_context(store, settings):
    s = state(store)
    store.save_document(s.run_id, Document(url="https://example.com/model", title="ModelAlpha evaluation",
                                           text="ModelAlpha v1 measured on BenchOne", source="test"))
    memory = Memory(store, settings)
    recalled = memory.recall("ModelAlpha")
    assert recalled and "reverify" in recalled[0]["use"]
    assert recalled[0]["as_of"] <= datetime.now(UTC).isoformat()


def test_archival_retires_question_without_deleting_history_or_marking_done(store):
    s = state(store)
    task = ResearchTask(id="retired", dimension="benchmark", goal="Temporary development review",
                        done_when="Resolve the question", missing=["scope"])
    store.save_task(s.run_id, task)
    store.checkpoint(s)
    store.archive_task(task.id, "Engineering-only acceptance item; not a standing business question")
    assert not store.recall_tasks(include_not_due=True)
    payload = store.db.execute("SELECT payload FROM tasks WHERE id=?", (task.id,)).fetchone()[0]
    assert ResearchTask.model_validate_json(payload).status == "archived"
    assert store.events(s.run_id)[-1]["kind"] == "task_archived"
