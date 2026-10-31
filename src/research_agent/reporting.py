from __future__ import annotations

import html
import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from .domain import Finding, ReportDraft, RunState, normalized
from .report_model import DIMENSIONS, KINDS, STATUS, report_bundle, safe_url
from .settings import Settings
from .store import Store


def validate_report(draft: ReportDraft, state: RunState, store: Store) -> list[str]:
    errors = []
    claims = {c["id"]: c for c in store.claims(state.claim_ids)}
    latest = {}
    for cid in state.claim_ids:
        if cid in claims:
            latest[claims[cid]["question_key"]] = cid
    for finding in draft.findings:
        if not finding.claim_ids and not finding.inference:
            errors.append("Factual finding has no assessed claim")
        for cid in finding.claim_ids:
            row = claims.get(cid)
            if not row:
                errors.append("Finding cites an unknown claim")
                continue
            if latest[row["question_key"]] != cid:
                errors.append("Finding uses a superseded claim revision")
            if row["payload"]["verdict"] == "unverified" and not finding.inference:
                errors.append("Unverified claim was presented as a fact")
    for recommendation in draft.recommendations:
        if not recommendation.basis_claim_ids:
            errors.append("Recommendation lacks traceable basis")
        rows = [claims.get(cid) for cid in recommendation.basis_claim_ids]
        if any(r is None for r in rows):
            errors.append("Recommendation cites an unknown claim")
        if any(r and latest[r["question_key"]] != r["id"] for r in rows):
            errors.append("Recommendation uses a superseded claim revision")
        if recommendation.priority in {"P0", "P1"} and not any(
            r and r["payload"]["verdict"] != "unverified" for r in rows
        ):
            errors.append("Action priority exceeds the available verified evidence")
    for row in claims.values():
        payload = row["payload"]
        for cite in payload["citations"]:
            docs = store.documents([cite["document_id"]])
            if not docs or normalized(cite["quote"]) not in normalized(docs[0].text):
                errors.append("Persisted quote integrity check failed")
    return list(dict.fromkeys(errors))


def fallback_draft(state: RunState, store: Store) -> ReportDraft:
    findings = []
    tasks = {t.id: t for t in state.tasks}
    latest = {r["question_key"]: r for r in store.claims(state.claim_ids)}
    for row in latest.values():
        claim = row["payload"]
        verdict = claim["verdict"]
        label = {"support": "证据支持", "contradict": "证据冲突", "unverified": "尚未证实"}[verdict]
        task = tasks.get(row["task_id"])
        findings.append(Finding(dimension=task.dimension if task else "benchmark",
                                text=f"{label}：{claim['claim']}。{claim['rationale']}",
                                claim_ids=[row["id"]], inference=verdict == "unverified"))
    return ReportDraft(headline="日度研究记录（部分完成）", findings=findings[-12:],
                       limitations=["自动综合未完成；以下为结构化核验记录，需结合证据复核。",
                                    *state.warnings[-5:]])


def md(text: str) -> str:
    return html.escape(str(text), quote=False).replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("`", "\\`").replace("\n", " ")


def safe_link(url: str) -> str:
    return quote(safe_url(url), safe=":/?#[]@!$&'*+,;=%~_-")


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_report(state: RunState, settings: Settings, store: Store, status: str) -> Path:
    draft = state.draft or fallback_draft(state, store)
    bundle = report_bundle(state, store, status, draft, store.get_run(state.run_id))
    claims = store.claims(state.claim_ids)
    lines = [f"# {md(bundle['title'])}", "",
             f"日期：{state.local_day} · 状态：{STATUS.get(status, status)} ({status}) · 运行：{state.run_id}", "",
             f"记录类型：{KINDS[bundle['kind']]} · {md(bundle['scope'])}", "",
             f"本次记录：{bundle['counts']['findings']} 项发现、{len(claims)} 条断言、{len(bundle['sources'])} 份来源；用时 {bundle['elapsed_seconds']} 秒。", ""]
    if state.mode == "demo":
        lines += ["> **离线演示：所有发现、网页和模型决策均为合成测试数据，不是实际情报。**", ""]
    lines += ["## 覆盖与信源状态", "", "| 调研方向 | 扫描状态 |", "| --- | --- |"]
    for dimension in ("benchmark", "competitor", "talent", "supplier"):
        record = state.coverage.get("web:" + dimension, {})
        lines.append(f"| {DIMENSIONS[dimension]} | {md(STATUS.get(record.get('status', 'not_scanned'), '未知'))} |")
    lines += ["", "| 发现通道 | 状态 | 保留条目 | 日期过滤 |", "| --- | --- | ---: | ---: |"]
    for channel in bundle["channels"]:
        lines.append(f"| {channel['name']} | {STATUS.get(channel['status'], channel['status'])} | {channel['items']} | {channel['dropped']} |")
    for channel in bundle["channels"]:
        for error in channel["errors"]:
            lines.append(f"\n> {channel['name']}：{md(error)}\n")
    if bundle["evaluation_cases"]:
        lines += ["", "## 标注案例结果", "", "| 案例 | 预期 | 实际 | 结果 |", "| --- | --- | --- | --- |"]
        for case in bundle["evaluation_cases"]:
            lines.append(f"| {md(case['id'])} | {case.get('expected', '—')} | {case.get('actual', '未返回')} | {'通过' if case.get('passed') else '未通过'} |")
    lines += ["", "## 研究发现", ""]
    for finding in draft.findings:
        label = "（推断或未证实）" if finding.inference else ""
        lines.append(f"- **{finding.dimension}**{label}：{md(finding.text)} `{' / '.join(finding.claim_ids)}`")
    if not draft.findings:
        lines.append("本次未形成可交付的已核验发现；这不等于外部没有新进展。")
    lines += ["", "## 数据储备建议", ""]
    for advice in draft.recommendations:
        lines.append(f"- **{advice.priority}**：{md(advice.action)}；依据：`{', '.join(advice.basis_claim_ids)}`；不确定性：{md(advice.uncertainty) or '见引用证据'}")
    if not draft.recommendations:
        lines.append("证据不足以形成具体行动建议，保留观察与补查。")
    lines += ["", "## 断言与原始证据", ""]
    cutoff = date.fromisoformat(state.local_day) - timedelta(days=settings.sources.time_window_days)
    for row in claims:
        claim = row["payload"]
        lines += [f"### {row['id']} · {claim['verdict']}", "", md(claim["claim"]), "", md(claim["rationale"]), ""]
        for cite in claim["citations"]:
            docs = store.documents([cite["document_id"]])
            if not docs:
                continue
            doc = docs[0]
            url = safe_link(doc.url)
            link = f"[{md(doc.title)}]({url})" if url else md(doc.title)
            lines.append(f"- {link} · `{doc.id}` · {doc.kind} · 发布时间：{doc.published_at or '未知'}")
            lines.append(f"  > {md(cite['quote'])}")
            if doc.published_at and doc.published_at[:10] < cutoff.isoformat():
                lines.append("  - 历史背景证据：不作为本期新增事件。")
    lines += ["", "## 未解决问题", ""]
    for task in state.tasks:
        if task.status != "done":
            lines.append(f"- {md(task.goal)}；状态：{task.status}；缺口：{md(', '.join(task.missing))}；下次：{task.retry_after or '待安排'}")
    lines += ["", "## 风险与质量评审", ""]
    lines.extend("- " + md(s) for s in list(dict.fromkeys(draft.limitations + state.warnings)))
    if state.review:
        lines.append("- Critic：`" + json.dumps(state.review.model_dump(exclude={"issues", "summary"}), ensure_ascii=False) + "`")
        lines.append("- " + md(state.review.summary))
        for issue in state.review.issues:
            lines.append(f"- Critic {'阻断问题' if issue.blocking else '非阻断意见'}：{md(issue.issue)}")
        for note in state.review.notes:
            lines.append("- 评审备注：" + md(note))
    else:
        lines.append("- Critic 未完成，本次不会被标记为 complete。" if bundle["kind"] in {"research", "acceptance", "demo"}
                     else "- 本记录属于诊断或标注评测，不执行业务日报 Critic。")
    usage = store.usage(run_id=state.run_id, mode=state.mode)
    lines += ["", "## 运行审计", "", f"- 工具/控制步骤：{state.steps}",
              f"- 模型估算费用（含未确认请求保留额度）：${usage['usd']:.6f}；非供应商账单。",
              f"- Tavily 基础搜索 credits（含未确认请求）：{usage['search_credits']}",
              f"- 已返回 token：输入 {bundle['usage']['prompt_tokens']}，输出 {bundle['usage']['completion_tokens']}；思考 token 为输出的子集。",
              f"- 停止原因：{state.finish_reason or status}",
              "- 语义判定包含模型评估；数值置信度不是经校准的准确率。"]
    directory = settings.path(settings.reports_dir) / state.mode
    path = directory / f"{state.local_day}-{state.run_id}.md"
    atomic_write(path, "\n".join(lines) + "\n")
    atomic_write(path.with_suffix(".json"), state.model_dump_json(indent=2))
    atomic_write(path.with_suffix(".report.json"), json.dumps(bundle, ensure_ascii=False, indent=2))
    from .rendering import standalone_report
    atomic_write(path.with_suffix(".html"), standalone_report(bundle, settings.schedule.timezone))
    return path


def backfill_reports(settings: Settings, store: Store, *, force=False) -> dict:
    """Administrative regeneration of derived artifacts; original conclusions/status remain untouched."""
    import uuid
    owner = uuid.uuid4().hex
    lease = "research:" + settings.mode
    store.acquire(lease, owner)
    result = {"generated": 0, "skipped": 0, "failed": []}
    try:
        for row in store.all_runs():
            if row["status"] == "running":
                result["skipped"] += 1
                continue
            path = Path(row["report_path"]) if row.get("report_path") else None
            if not force and path and all(path.with_suffix(s).is_file() for s in (".md", ".json", ".report.json", ".html")):
                result["skipped"] += 1
                continue
            try:
                state = RunState.model_validate_json(row["checkpoint"])
                path = write_report(state, settings, store, row["status"])
                store.attach_report(state.run_id, str(path))
                result["generated"] += 1
            except Exception as exc:
                from .errors import safe_error
                result["failed"].append({"run_id": row["id"], "error": safe_error(exc)})
    finally:
        store.release(lease, owner)
    return result
