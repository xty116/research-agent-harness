"""Paid, bounded controller integration on a cached real paper; not a full daily run."""
import asyncio
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from research_agent.application import create_harness
from research_agent.domain import ResearchTask, RunState
from research_agent.settings import load_settings


async def main():
    settings = load_settings()
    settings.budget.per_run_usd = .09
    settings.budget.report_reserve_usd = .02
    settings.budget.max_steps = 8
    settings.budget.max_seconds = 180
    harness = create_harness(settings)
    try:
        with harness.store.lock:
            row = harness.store.db.execute("SELECT id FROM documents WHERE kind='full_text' "
                "AND text LIKE '%149 firm-quarters%' AND text LIKE '%86 US companies%' LIMIT 1").fetchone()
        if not row:
            raise SystemExit("The cached real paper is unavailable; perform discovery before this acceptance test")
        document = harness.store.documents([row[0]])[0]
        day = datetime.now(ZoneInfo(settings.schedule.timezone)).date().isoformat()
        state = RunState(run_id="accept_" + uuid.uuid4().hex[:12], local_day=day, mode="live", phase="research",
                         prompt="真实模型闭环验收：预设一个缓存真实论文的样本范围核验任务。不是完整日度调研，也不构成四方向覆盖。",
                         loaded_skills=["research-planning", "web-research", "benchmark-verification", "evidence-review"],
                         candidate_ids=[document.id], evidence_ids=[document.id])
        task = ResearchTask(id="acceptance_scope", dimension="benchmark", entity="EPSBench", priority=5,
                            goal="Verify that EPSBench studies 149 firm-quarters from 86 US companies.",
                            missing=["sample scope"], document_ids=[document.id],
                            done_when="Assess this exact sample-scope claim using a verbatim quote from the cached paper.")
        state.tasks = [task]
        harness.store.create_run(state)
        harness.store.save_document(state.run_id, document)
        harness.store.save_task(state.run_id, task)
        harness.store.event(state.run_id, "acceptance_setup", {"scope": "Preset task and cached real source; model-directed controller",
                                                               "document_id": document.id, "url": document.url})
        result = await harness.run(resume_id=state.run_id)
        row = harness.store.get_run(result.run_id)
        print(json.dumps({"run_id": result.run_id, "status": row["status"], "report": row["report_path"],
                          "tasks": [t.status for t in result.tasks], "claims": len(result.claim_ids),
                          "critic_passed": result.review.passed if result.review else False,
                          "scope": "Preset single-task integration; partial expected without full discovery coverage",
                          "steps": result.steps, "usage": harness.store.usage(run_id=result.run_id, mode="live")}, ensure_ascii=False))
        if not result.claim_ids or result.tasks[0].status != "done" or not result.review or row["status"] != "partial":
            raise SystemExit(2)
    finally:
        await harness.model.close()
        harness.store.close()


if __name__ == "__main__":
    asyncio.run(main())
