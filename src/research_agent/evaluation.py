"""Small labeled semantic regression; synthetic evidence, real model calls."""
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .domain import ClaimAssessment, Document, RunState
from .errors import safe_error
from .reporting import atomic_write, write_report
from .settings import ROOT
from .tools import validate_assessment


async def evaluate(harness):
    s, store = harness.settings, harness.store
    state = RunState(run_id="eval_" + uuid.uuid4().hex[:12], mode=s.mode,
                     local_day=datetime.now(ZoneInfo(s.schedule.timezone)).date().isoformat(),
                     prompt="Labeled synthetic evidence evaluation; not a daily intelligence run.")
    store.create_run(state)
    results = []
    try:
        for case in json.loads((ROOT / "evals/cases.json").read_text()):
            document = Document(id="fixture_" + case["id"], url="https://evaluation.invalid/" + case["id"],
                                title="Synthetic labeled evaluation fixture", text=case["text"],
                                source="evaluation", kind="full_text")
            try:
                result = await harness.model.generate(state, "evaluation", "Evaluate the exact claim using only "
                    "the supplied synthetic source. A mismatch of version or advertised versus delivered scope "
                    "is unverified unless directly contradicted. Copy exact source quotes. Return JSON.",
                    {"claim": case["claim"], "missing_fields": [], "evidence": [document.model_dump()]},
                    ClaimAssessment, report=True)
                validate_assessment(result, [document])
                passed = result.verdict == case["expected"] and result.claim == case["claim"]
                row = {"id": case["id"], "expected": case["expected"], "actual": result.verdict,
                       "passed": passed, "assessment": result.model_dump()}
            except Exception as exc:
                row = {"id": case["id"], "expected": case["expected"], "passed": False, "error": safe_error(exc)}
            results.append(row)
            store.event(state.run_id, "evaluation_case", row)
        state.phase = "done"
        summary = {"run_id": state.run_id, "passed": sum(r["passed"] for r in results), "total": len(results),
                   "scope": "Synthetic labeled evidence; real model. Not real-world accuracy or full-agent quality.",
                   "usage": store.usage(run_id=state.run_id, mode=s.mode), "cases": results}
        path = s.path(s.reports_dir) / "evaluations" / f"{state.run_id}.json"
        atomic_write(path, json.dumps(summary, ensure_ascii=False, indent=2))
        store.finish(state, "evaluated", error=f"Synthetic fixture evaluation {summary['passed']}/{len(results)}")
        report_path = write_report(state, s, store, "evaluated")
        store.attach_report(state.run_id, str(report_path))
        return {**summary, "path": str(path)}
    except BaseException:
        # Evaluation does not become a resumable daily task and never writes fixture evidence into live memory.
        store.finish(state, "evaluation_interrupted")
        raise
