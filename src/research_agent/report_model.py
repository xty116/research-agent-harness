"""Read models for reports. Reconstruct history without inventing research results."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .domain import ReportDraft, RunState

DIMENSIONS = {"benchmark": "Benchmark", "competitor": "竞品", "talent": "人才", "supplier": "供应商"}
STATUS = {"complete": "已完成", "partial": "部分完成", "running": "运行中", "interrupted": "已中断",
          "evaluated": "评测记录", "evaluation_interrupted": "评测中断", "ok": "正常",
          "error": "异常", "not_scanned": "未扫描", "pending": "待处理", "done": "已完成",
          "deferred": "待跟进", "archived": "已归档", "blocked": "已拦截", "unavailable": "不可用",
          "support": "证据支持", "contradict": "证据冲突", "unverified": "未证实"}
KINDS = {"research": "日度研究", "acceptance": "单任务验收", "evaluation": "标注评测",
         "diagnostic": "通道诊断", "demo": "合成演示"}
PHASES = {"scan": "采集线索", "plan": "制定计划", "research": "研究取证", "report": "综合报告",
          "review": "质量评审", "done": "已结束"}
REASONS = {"model_finished": "模型结束研究", "research_finished": "研究阶段结束", "budget_reserve": "已保护报告预算",
           "time_reserve": "已达到研究时间限制", "step_limit": "已达到执行步数限制", "execution_error": "执行遇到异常"}


def kind_of(row: dict) -> str:
    if row.get("mode") == "demo":
        return "demo"
    for prefix, kind in (("eval_", "evaluation"), ("accept_", "acceptance"), ("source-health", "diagnostic")):
        if row["id"].startswith(prefix):
            return kind
    return "research"


def safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return value if not any(c in value for c in "\r\n\x00") else ""
    except ValueError:
        pass
    return ""


def human_issue(value: str) -> str:
    lower = value.lower()
    if "432" in value or "usage limit" in lower:
        return "全网检索额度已用尽，需要恢复搜索账户额度。"
    if "budget" in lower and ("reached" in lower or "exhaust" in lower):
        return "已达到本次或当日预算限制，未完成内容保留为待跟进。"
    if "context" in lower and "budget" in lower:
        return "上下文材料超过当次调用限制，本次未完成自动综合。"
    if "source web:" in lower and "failed" in lower:
        dimension = next((v for k, v in DIMENSIONS.items() if k in lower), "部分方向")
        return f"{dimension} 的全网检索未完成，相关发现需要补充证据。"
    if "recommendation lacks" in lower:
        return "建议缺少可追溯依据，报告已采用保守的结构化记录。"
    return value


def channel_summary(coverage: dict) -> list[dict]:
    result = []
    for key, name, symbol in (("web", "全网检索", "W"), ("papers", "论文", "P"), ("github", "GitHub", "G"),
                               ("huggingface", "HuggingFace", "H"), ("rss", "RSS 订阅", "R")):
        records = [v for k, v in coverage.items() if k.startswith("web:")] if key == "web" else ([coverage[key]] if key in coverage else [])
        expected = 4 if key == "web" else 1
        if not records:
            status = "not_scanned"
        elif len(records) == expected and all(r.get("status") == "ok" for r in records):
            status = "ok"
        elif any(r.get("status") in {"ok", "partial"} for r in records):
            status = "partial"
        else:
            status = "error"
        errors = [r.get("error", "") for r in records if r.get("error")]
        errors += [failure for r in records for failure in r.get("partial_failures", [])]
        result.append({"key": key, "name": name, "symbol": symbol, "status": status,
                       "items": sum(r.get("items", 0) for r in records),
                       "dropped": sum(r.get("dropped_by_date", 0) for r in records),
                       "errors": errors, "message": human_issue(errors[0]) if errors else STATUS.get(status, status)})
    return result


def report_bundle(state: RunState, store, status: str, draft: ReportDraft, metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    kind = kind_of({"id": state.run_id, "mode": state.mode})
    events = store.events(state.run_id, limit=1000)
    cases = [e["payload"] for e in events if e["kind"] == "evaluation_case"]
    title = draft.headline
    if kind == "evaluation":
        title = f"断言核验评测 · {len(cases)} 个标注案例"
    elif kind == "diagnostic":
        title = "检索通道诊断记录"
    elif kind == "acceptance":
        title = "真实原文 · 单任务闭环验收"
    elif state.draft is None:
        title = "研究运行记录 · 综合尚未完成"
    documents = store.run_documents(state.run_id)
    selected = set(state.evidence_ids + state.candidate_ids)
    raw_claims = store.claims(state.claim_ids)
    selected.update(c["document_id"] for r in raw_claims for c in r["payload"]["citations"])
    by_id = {d.id: d for d in documents + store.documents(list(selected))}
    sources = [{"id": d.id, "title": d.title, "url": safe_url(d.url), "source": d.source,
                "kind": d.kind, "dimension": d.dimension, "published_at": d.published_at,
                "fetched_at": d.fetched_at, "characters": len(d.text)} for d in by_id.values()]
    sources_by_id = {d["id"]: d for d in sources}
    claims = []
    latest = {record["question_key"]: record["id"] for record in raw_claims}
    for record in raw_claims:
        payload = record["payload"]
        claims.append({"id": record["id"], "task_id": record["task_id"], "created_at": record["created_at"],
                       "is_current": latest[record["question_key"]] == record["id"],
                       **payload, "citations": [{**c, "source": sources_by_id.get(c["document_id"])}
                                                  for c in payload["citations"]]})
    usage = store.usage(run_id=state.run_id, mode=state.mode)
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "uncertain_requests": 0}
    stages = defaultdict(lambda: {"requests": 0, "tokens": 0, "usd": 0})
    for row in store.usage_rows(state.run_id):
        detail = json.loads(row["usage_json"] or "{}")
        for key in ("prompt_tokens", "completion_tokens"):
            totals[key] += detail.get(key, 0)
        totals["reasoning_tokens"] += (detail.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        totals["uncertain_requests"] += row["status"] != "settled"
        if row["purpose"].startswith("llm:"):
            stage = row["purpose"].split(":")[1]
            stages[stage]["requests"] += 1
            stages[stage]["tokens"] += detail.get("prompt_tokens", 0) + detail.get("completion_tokens", 0)
            stages[stage]["usd"] += row["actual_usd"] if row["actual_usd"] is not None else row["reserved_usd"]
    totals["total_tokens"] = totals["prompt_tokens"] + totals["completion_tokens"]
    updated = metadata.get("updated_at") or state.started_at
    try:
        finish = datetime.now(UTC) if status == "running" else datetime.fromisoformat(updated)
        elapsed = max(0, int((finish - datetime.fromisoformat(state.started_at)).total_seconds()))
    except (ValueError, TypeError):
        elapsed = 0
    warnings = list(dict.fromkeys(draft.limitations + state.warnings + ([metadata["error"]] if metadata.get("error") else [])))
    if kind == "demo":
        scope = "合成演示：内容与决策为离线回放，不是实际情报或真实模型能力评估。"
    elif kind == "evaluation":
        scope = "合成标注证据 + 真实模型调用，仅用于少量语义回归，不代表真实调研准确率。"
    elif kind == "acceptance":
        scope = "预设单任务与缓存真实原文的闭环验收，不代表完整日度发现或四方向覆盖。"
    elif kind == "diagnostic":
        scope = "通道诊断记录，不作为业务研究报告或已核验情报。"
    else:
        scope = "日度研究：结论以已登记断言与原文证据为依据，缺口和失败状态单独保留。"
    return {"schema_version": "1.0", "run_id": state.run_id, "day": state.local_day, "mode": state.mode,
            "kind": kind, "status": status, "title": title, "scope": scope, "phase": state.phase,
            "started_at": state.started_at, "snapshot_at": updated, "elapsed_seconds": elapsed,
            "finish_reason": state.finish_reason, "steps": state.steps,
            "origin": "model" if state.draft else "structured_record",
            "summary": draft.findings[0].text if draft.findings else
                       ("已记录评测结果，查看每个案例的预期与实际判定。" if cases else "本次尚未形成可交付的已核验发现；这不等于外部没有新进展。"),
            "findings": [f.model_dump() for f in draft.findings],
            "recommendations": [r.model_dump() for r in draft.recommendations],
            "claims": claims, "sources": sources, "channels": channel_summary(state.coverage),
            "coverage": state.coverage, "tasks": [t.model_dump() for t in state.tasks],
            "warnings": warnings, "quality": state.review.model_dump() if state.review else None,
            "quality_passed": state.review.passed if state.review else False,
            "evaluation_cases": cases, "usage": {**usage, **totals},
            "stages": [{"stage": k, **v} for k, v in stages.items()],
            "requested_models": sorted({e["payload"].get("model", "") for e in events if e["kind"] == "model_usage"}),
            "provider_models": sorted({e["payload"].get("provider_model", "") for e in events if e["kind"] == "model_usage"}),
            "loaded_skills": state.loaded_skills, "events_count": len(events),
            "counts": {"findings": len(draft.findings), "claims": len(claims), "sources": len(sources),
                       "tasks_done": sum(t.status == "done" for t in state.tasks), "tasks": len(state.tasks),
                       "channels_ok": sum(c["status"] == "ok" for c in channel_summary(state.coverage))}}
