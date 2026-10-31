import pytest

from research_agent.domain import ClaimAssessment, Document, Plan, ReportDraft, ResearchTask, RunState
from research_agent.harness import PLAN_INSTRUCTIONS
from research_agent.reporting import validate_report


def initialize(store, run_id="retrieval"):
    state = RunState(run_id=run_id, local_day=store.billing_day(), mode="demo", prompt="daily research",
                     loaded_skills=["research-planning", "web-research"])
    store.create_run(state)
    return state


async def test_phrase_location_and_paging_reach_late_tables(harness, store):
    state = initialize(store)
    prefix = "Generic benchmark discussion. " * 300
    doc = store.save_document(state.run_id, Document(url="https://example.com/paper", title="Paper",
                              text=prefix + "\n| Model | N |\n| SpecificModel-3 | 149 |", source="test", kind="full_text"))
    state.evidence_ids = [doc.id]
    found = await harness.executor.execute(state, "find_in_document", {"document_id": doc.id, "query": "149"})
    offset = found.data["matches"][0]["offset"]
    assert offset > 8000
    result = await harness.executor.execute(state, "read_document", {"document_id": doc.id, "offset": offset - 30})
    assert "SpecificModel-3" in result.data["text"]


async def test_disclosed_historical_ids_are_usable_by_planner(harness, store, monkeypatch):
    old = initialize(store, "old")
    document = store.save_document(old.run_id, Document(url="https://example.com/old", title="Prior evidence",
                                   text="A dated previous source", source="test", kind="full_text"))
    current = initialize(store, "current")
    monkeypatch.setattr(harness.memory, "recall", lambda _: [{"id": document.id, "text": document.text}])

    class HistoricalPlanner:
        async def generate(self, state, stage, instructions, context, schema, **kwargs):
            assert context["historical_memory"][0]["id"] == document.id
            return Plan(tasks=[])

    harness.model = HistoricalPlanner()
    await harness._generate(current, "plan", PLAN_INSTRUCTIONS, Plan, strong=True)
    assert document.id in current.evidence_ids
    assert document.id in RunState.model_validate_json(store.get_run(current.run_id)["checkpoint"]).evidence_ids


def test_invalid_plan_update_does_not_partially_change_priority(harness, store):
    from research_agent.domain import Decision
    state = initialize(store)
    task = ResearchTask(id="task1", dimension="benchmark", goal="Check scope", done_when="Original scope obtained")
    state.tasks = [task]
    decision = Decision(action="finish", reason="Test atomic validation", reprioritize=[
        {"task_id": task.id, "priority": 5, "reason": "Potential change"},
        {"task_id": "missing", "priority": 5, "reason": "Invalid reference"}])
    with pytest.raises(ValueError):
        harness._apply_plan_updates(state, decision)
    assert task.priority == 3


def test_report_rejects_unverified_facts_and_untraceable_priorities(store):
    state = initialize(store)
    claim = ClaimAssessment(claim="Model A leads the benchmark", verdict="unverified", rationale="Version missing")
    cid = store.save_claim(state.run_id, "task1", claim)
    state.claim_ids = [cid]
    report = ReportDraft(headline="Report validation", findings=[{"dimension": "benchmark", "text": claim.claim, "claim_ids": [cid]}],
                         recommendations=[{"priority": "P0", "action": "Buy this dataset", "basis_claim_ids": [cid]}])
    errors = validate_report(report, state, store)
    assert any("Unverified" in e for e in errors)
    assert any("priority exceeds" in e for e in errors)


def test_report_cannot_revert_to_superseded_claim(store):
    state = initialize(store)
    claim = ClaimAssessment(claim="Model A leads the benchmark", verdict="unverified", rationale="Version missing")
    old = store.save_claim(state.run_id, "task1", claim)
    new = store.save_claim(state.run_id, "task1", claim.model_copy(update={"rationale": "New evidence still lacks scope"}))
    state.claim_ids = [old, new]
    draft = ReportDraft(headline="Revision test", findings=[{"dimension": "benchmark", "text": claim.claim,
                                                            "claim_ids": [old], "inference": True}])
    assert "Finding uses a superseded claim revision" in validate_report(draft, state, store)


async def test_browser_cannot_act_on_an_unobserved_selector(harness, store):
    state = initialize(store)
    document = store.save_document(state.run_id, Document(url="https://example.com/", title="Page",
                                   text="Observed search result", source="test"))
    state.candidate_ids = [document.id]
    with pytest.raises(ValueError, match="snapshot"):
        await harness.executor.execute(state, "browser_read", {"document_id": document.id,
            "steps": [{"operation": "click", "selector": "#unobserved"}]})


def test_optional_review_notes_do_not_pass_material_failures():
    from research_agent.domain import Review, ReviewIssue
    review = Review(factuality=9, completeness=8, citations=9, timeliness=8, actionability=8,
                    summary="Primary paper supports a claim about its own sample scope.",
                    notes=["Single source; independent replication not claimed."])
    assert review.passed
    review.issues = [ReviewIssue(dimension="factuality", issue="Unsupported rank claim", blocking=True)]
    assert not review.passed
