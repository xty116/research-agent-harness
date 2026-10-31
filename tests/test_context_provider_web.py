import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from research_agent.context import token_upper_bound
from research_agent.domain import Document, ReportDraft, RunState
from research_agent.providers import DeepSeekModel, ModelError
from research_agent.tools import tool_catalog
from research_agent.web import create_app


def new_state(store):
    state = RunState(run_id="regression", local_day=store.billing_day(), mode="demo",
                     prompt="每天调研 Benchmark、竞品、人才与供应商", loaded_skills=["research-planning"])
    store.create_run(state)
    return state


def test_context_keeps_four_directions_and_counts_review_draft(harness, store, settings):
    state = new_state(store)
    for n in range(24):
        dimension = ("benchmark", "competitor", "talent", "supplier")[n % 4]
        doc = store.save_document(state.run_id, Document(url=f"https://example.com/{n}", title="候选线索" * 40,
                                   text="待核验材料。" * 800, source="test", dimension=dimension))
        state.candidate_ids.append(doc.id)
        state.candidate_scores[doc.id] = {"importance": 5, "dimension": dimension}
    state.coverage = {name: {"status": "ok", "document_ids": state.candidate_ids} for name in
                      ("web:benchmark", "web:competitor", "web:talent", "web:supplier", "papers", "github", "rss", "huggingface")}
    draft = ReportDraft(headline="预算与材料压缩回归测试", limitations=["仍需核验" * 80])
    plan = harness.context.build(state, tool_catalog(), "plan")
    assert {d["dimension"] for d in plan["candidate_manifest"]} == {"benchmark", "competitor", "talent", "supplier"}
    context = harness.context.build(state, tool_catalog(), "critic", {"draft": draft.model_dump()})
    assert context["tool_catalog"] == []
    assert all("document_ids" not in value for value in context["coverage"].values())
    assert context["draft"] == draft.model_dump()
    assert token_upper_bound(json.dumps(context, ensure_ascii=False)) < settings.context.max_input_tokens - 2500


class Answer(BaseModel):
    result: str


async def test_model_routing_schema_retry_and_actual_usage(settings, store, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    settings.llm.retries = 1
    state = new_state(store)
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        content = "invalid JSON" if len(calls) == 1 else '{"result":"ok"}'
        return httpx.Response(200, json={"model": "actual-provider-model", "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                                         "choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    model = DeepSeekModel(settings, store, httpx.MockTransport(respond))
    try:
        assert (await model.generate(state, "planner", "Plan", {}, Answer, strong=True)).result == "ok"
        assert (await model.generate(state, "triage", "Classify", {}, Answer)).result == "ok"
        assert [c["model"] for c in calls] == ["deepseek-reasoner", "deepseek-reasoner", "deepseek-chat"]
        assert calls[0]["thinking"]["type"] == "enabled"
        assert calls[-1]["thinking"]["type"] == "disabled"
        assert store.usage(run_id=state.run_id, mode="demo")["usd"] == pytest.approx(.0006)
        events = store.events(state.run_id)
        assert any(e["kind"] == "model_schema_error" for e in events)
        assert "test-secret" not in json.dumps(events)
        assert any(e["payload"].get("provider_model") == "actual-provider-model" for e in events)
    finally:
        await model.close()


async def test_uncertain_timeout_keeps_reservation(settings, store, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    settings.llm.retries = 0
    state = new_state(store)

    def timeout(request):
        raise httpx.ReadTimeout("unknown provider outcome", request=request)

    model = DeepSeekModel(settings, store, httpx.MockTransport(timeout))
    try:
        with pytest.raises(ModelError):
            await model.generate(state, "planner", "Plan", {}, Answer, strong=True)
        assert store.usage(run_id=state.run_id, mode="demo")["usd"] > .01
    finally:
        await model.close()


async def test_definite_auth_failure_releases_reservation(settings, store, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    state = new_state(store)
    model = DeepSeekModel(settings, store, httpx.MockTransport(lambda _: httpx.Response(401)))
    try:
        with pytest.raises(ModelError, match="401"):
            await model.generate(state, "planner", "Plan", {}, Answer, strong=True)
        assert store.usage(run_id=state.run_id, mode="demo")["usd"] == 0
    finally:
        await model.close()


async def test_dashboard_report_and_events_are_read_only_and_escape_sources(harness, settings):
    state = await harness.run()
    harness.store.event(state.run_id, "error", {"message": "<script>alert('xss')</script>"})
    with TestClient(create_app(settings, harness, schedule=False)) as client:
        assert client.get("/health/ready").json()["status"] == "ready"
        assert client.get("/api/runs").status_code == 200
        assert client.get(f"/api/runs/{state.run_id}/events").json()
        assert client.post("/api/runs", json={}).status_code == 405
        assert client.get("/api/runs/unknown").status_code == 404
        text = client.get(f"/runs/{state.run_id}?tab=trace").text
        assert "&lt;script&gt;alert" in text
        assert "<script>alert" not in text
        assert "合成" in client.get(f"/runs/{state.run_id}/report").text
