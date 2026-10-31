"""Synthetic replay adapter. This never calls a model or the network."""
from __future__ import annotations

from .domain import Document

HEADLINE = "Model A ranks first overall on CodeBench v2."
QUOTE = "Model B leads the overall CodeBench v2 ranking. Model A leads only the open-model subset."


class DemoSources:
    async def web_search(self, state, query, **kwargs):
        return [Document(url="https://research-demo.invalid/model-a", title="Synthetic Model A ranking headline",
                         text=HEADLINE, source="demo", published_at=state.local_day)]

    async def papers(self, query, since=None):
        return []

    async def github(self, query):
        return []

    async def huggingface(self):
        return []

    async def rss(self):
        return []

    async def fetch(self, url):
        return Document(url=url, title="Synthetic official CodeBench v2 results", text=QUOTE,
                        source="demo", kind="full_text")


class DemoModel:
    """Deterministic model-output replay for harness validation, not an intelligence benchmark."""

    async def close(self):
        pass

    async def generate(self, state, stage, instructions, context, schema, **kwargs):
        if stage == "triage":
            return schema.model_validate({"items": [{"document_id": d["id"], "relevant": True,
                                                      "importance": 5, "dimension": "benchmark",
                                                      "entities": ["Model A"]} for d in context["documents"]]})
        if stage == "plan":
            doc_id = state.candidate_ids[0]
            return schema.model_validate({"tasks": [{"id": "candidate_task", "dimension": "benchmark",
                "goal": "Verify whether Model A ranks first overall on CodeBench v2", "entity": "Model A",
                "priority": 5, "reason": "Headline requires category verification",
                "missing": ["leaderboard category"], "document_ids": [doc_id],
                "done_when": "Resolve the overall-rank assertion using a quoted original result"}],
                "summary": "Check a potentially overgeneralized ranking claim"})
        if stage == "controller":
            task = state.tasks[0]
            common = {"task_id": task.id, "reason": "Synthetic replay exercises evidence-dependent routing"}
            loaded = state.loaded_skills
            if "web-research" not in loaded:
                action, args = "load_skill", {"name": "web-research"}
            elif not state.evidence_ids:
                action, args = "fetch_page", {"document_id": state.candidate_ids[0]}
            elif "evidence-review" not in loaded:
                action, args = "load_skill", {"name": "evidence-review"}
            elif "benchmark-verification" not in loaded:
                action, args = "load_skill", {"name": "benchmark-verification"}
            elif not state.claim_ids:
                action, args = "assess_claim", {"claim": HEADLINE, "entity": "Model A",
                                                "document_ids": [state.evidence_ids[-1]]}
            elif task.status != "done":
                action, args = "complete_task", {}
            else:
                action, args = "finish", {}
            return schema.model_validate({**common, "action": action, "arguments": args})
        if stage in {"claim_assessment", "conflict_adjudication"}:
            doc_id = context["evidence"][0]["id"]
            return schema.model_validate({"claim": HEADLINE, "entity": "Model A", "verdict": "contradict",
                "rationale": "The original result distinguishes overall and open-model rankings.",
                "citations": [{"document_id": doc_id, "quote": QUOTE, "stance": "contradict"}],
                "confidence": .85, "resolved_gaps": ["leaderboard category"]})
        if stage == "report":
            return schema.model_validate({"headline": "合成案例：模型榜单口径核验",
                "findings": [{"dimension": "benchmark", "text": "原始结果与全榜第一的新闻表述冲突：模型 A 仅在开放模型子榜单领先。",
                              "claim_ids": state.claim_ids}],
                "recommendations": [{"priority": "P1", "action": "对齐榜单分类后再安排评测样本覆盖检查。",
                                     "basis_claim_ids": state.claim_ids, "uncertainty": "演示数据，不用于实际业务决策。"}],
                "limitations": ["本次为合成回放，不能用于估计真实模型准确率或运行成本。"]})
        if stage == "critic":
            return schema.model_validate({"factuality": 8, "completeness": 8, "citations": 8,
                "timeliness": 8, "actionability": 8, "issues": [],
                "summary": "合成轨迹保留了榜单分类与证据，实际效果仍需真实案例评测。"})
        raise ValueError(f"Unsupported demo stage: {stage}")
