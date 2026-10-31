import json
import socket

import httpx
import pytest

from research_agent.domain import ClaimAssessment, Document
from research_agent.network import NetworkPolicyError, PublicHTTP, public_addresses
from research_agent.settings import ROOT
from research_agent.skills import SkillRegistry
from research_agent.tools import ProfileArgs, validate_assessment


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://[::1]/", "http://169.254.169.254/latest",
                                  "file:///etc/passwd", "https://user:pass@example.com/", "http://localhost/",
                                  "https://example.com:8080/"])
async def test_private_and_unsupported_targets_are_rejected(url):
    with pytest.raises(NetworkPolicyError):
        await public_addresses(url)


async def test_mixed_public_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(NetworkPolicyError):
        await public_addresses("https://example.com")


async def test_fetch_pins_ip_and_blocks_private_redirect(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(NetworkPolicyError):
        await PublicHTTP().request("GET", "http://example.com/start")
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "example.com"


def test_unknown_evidence_and_fabricated_quotes_are_rejected():
    doc = Document(id="real", url="https://example.com", title="test", text="Actual primary result is 42 points.",
                   kind="full_text", source="test")
    assessment = ClaimAssessment(claim="The result is 42 points", verdict="support", rationale="Measured result",
                                 citations=[{"document_id": "real", "quote": "Invented result is 90 points", "stance": "support"}])
    with pytest.raises(ValueError, match="quote"):
        validate_assessment(assessment, [doc])
    assessment.citations[0].quote = "Actual primary result is 42 points."
    assessment.citations[0].document_id = "invented-id"
    with pytest.raises(ValueError, match="outside"):
        validate_assessment(assessment, [doc])


def test_snippets_alone_cannot_establish_verified_fact():
    doc = Document(id="snippet", url="https://example.com", title="test", text="Model A ranks first on BenchOne.", source="search")
    assessment = ClaimAssessment(claim="Model A ranks first on BenchOne", verdict="support", rationale="Search hit",
        citations=[{"document_id": "snippet", "quote": doc.text, "stance": "support"}])
    with pytest.raises(ValueError, match="snippets"):
        validate_assessment(assessment, [doc])


def test_skill_reference_cannot_escape_curated_directory():
    registry = SkillRegistry(ROOT / "skills")
    with pytest.raises(ValueError):
        registry.reference("web-research", "../../../../.env")
    assert "Source routing" in registry.reference("web-research", "references/routing.md")


def test_dataset_profile_is_bounded_and_does_not_leak_rows(harness, settings):
    root = settings.path(settings.data_dir) / "samples"
    root.mkdir(parents=True)
    (root / "sample.json").write_text(json.dumps([{"id": "secret-person", "split": "train", "text": "private text"},
                                                 {"id": "secret-person", "split": "test", "text": None}]))
    result = harness.executor._profile(ProfileArgs(filename="sample.json", id_field="id", split_field="split"))
    assert result["cross_split_identifier_count"] == 1
    assert result["missing_counts"]["text"] == 1
    assert "secret-person" not in json.dumps(result) and "private text" not in json.dumps(result)
    with pytest.raises(ValueError):
        harness.executor._profile(ProfileArgs(filename="../../settings.yaml"))


async def test_tool_requires_skill_activation_and_known_source(harness):
    from research_agent.domain import RunState
    state = RunState(run_id="tooltest", local_day=harness.store.billing_day(), mode="demo", prompt="test")
    harness.store.create_run(state)
    with pytest.raises(ValueError, match="Load the skill"):
        await harness.executor.execute(state, "fetch_page", {"document_id": "missing"})
    await harness.executor.execute(state, "load_skill", {"name": "web-research"})
    with pytest.raises(ValueError, match="observed"):
        await harness.executor.execute(state, "fetch_page", {"document_id": "missing"})
