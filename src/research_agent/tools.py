from __future__ import annotations

import asyncio
import csv
import json
from collections import Counter
from typing import Any, Literal

from pydantic import Field

from .context import excerpt, token_upper_bound
from .domain import ClaimAssessment, Document, Observation, RunState, digest, normalized
from .memory import Memory
from .providers import Sources
from .settings import Settings, StrictModel
from .skills import SkillRegistry
from .store import Store


class EmptyArgs(StrictModel):
    pass


class SkillArgs(StrictModel):
    name: str = Field(max_length=80)


class ReferenceArgs(StrictModel):
    skill: str = Field(max_length=80)
    path: str = Field(max_length=150)


class QueryArgs(StrictModel):
    query: str = Field(min_length=3, max_length=400)


class FetchArgs(StrictModel):
    document_id: str


class ReadArgs(FetchArgs):
    focus: str = Field(default="", max_length=400)
    offset: int = Field(default=0, ge=0, le=60000)


class FindArgs(FetchArgs):
    query: str = Field(min_length=2, max_length=100)


class AssessArgs(StrictModel):
    claim: str = Field(min_length=4, max_length=500)
    entity: str = Field(default="", max_length=100)
    document_ids: list[str] = Field(min_length=1, max_length=4)


class DeferArgs(StrictModel):
    reason: str = Field(min_length=3, max_length=300)


class ProfileArgs(StrictModel):
    filename: str = Field(max_length=120)
    id_field: str = Field(default="", max_length=80)
    split_field: str = Field(default="", max_length=80)


class BrowserStep(StrictModel):
    operation: Literal["click", "select"]
    selector: str = Field(max_length=180)
    value: str = Field(default="", max_length=180)


class BrowserArgs(FetchArgs):
    steps: list[BrowserStep] = Field(default_factory=list, max_length=1)


SPECS: dict[str, tuple[type[StrictModel], str]] = {
    "load_skill": (SkillArgs, "Load a skill's full instructions before using its tools."),
    "unload_skill": (SkillArgs, "Unload an unused skill to reclaim context space."),
    "read_reference": (ReferenceArgs, "Read a Markdown reference explicitly linked by a loaded skill."),
    "search_web": (QueryArgs, "Find sources; one basic Tavily credit. Query in English or Chinese."),
    "search_papers": (QueryArgs, "Look up scholarly metadata through OpenAlex."),
    "search_github": (QueryArgs, "Search GitHub repository metadata; not evidence of implementation behavior."),
    "fetch_page": (FetchArgs, "Read the page at a registered document's URL; preserve a new content version."),
    "read_paper": (FetchArgs, "Read a registered HuggingFace paper page as Markdown."),
    "read_document": (ReadArgs, "Retrieve a relevant excerpt from an already stored document."),
    "find_in_document": (FindArgs, "Find an exact case-insensitive phrase/number; returns offsets and nearby source text."),
    "memory_search": (QueryArgs, "Recall relevant historical evidence; time-sensitive claims require rechecking."),
    "assess_claim": (AssessArgs, "Weak model evaluates a claim using selected documents and exact quotes."),
    "complete_task": (EmptyArgs, "Close a task only after a grounded, non-unverified assessment exists."),
    "defer_task": (DeferArgs, "Preserve the unresolved question and a future retry time."),
    "profile_dataset": (ProfileArgs, "Inspect a local CSV/JSON sample inside data/samples. Aggregates only."),
    "browser_read": (BrowserArgs, "Read a JS page and list controls. Optional steps: [{operation: click/select, selector: previously observed CSS selector, value: select value}]. One step per call."),
    "finish": (EmptyArgs, "End research and preserve unfinished tasks for the next run."),
}
CONTROL = {"load_skill", "unload_skill", "read_reference", "finish", "complete_task", "defer_task"}


def tool_catalog() -> list[dict]:
    return [{"name": name, "description": description,
             "arguments": {key: spec.get("type", "structured")
                           for key, spec in model.model_json_schema().get("properties", {}).items()},
             "required": model.model_json_schema().get("required", [])}
            for name, (model, description) in SPECS.items()]


def validate_assessment(assessment: ClaimAssessment, documents: list[Document]) -> ClaimAssessment:
    docs = {d.id: d for d in documents}
    for citation in assessment.citations:
        if citation.document_id not in docs:
            raise ValueError("Assessment cites a document outside the selected evidence")
        if normalized(citation.quote) not in normalized(docs[citation.document_id].text):
            raise ValueError("Citation quote is not present in the source text")
    if assessment.verdict != "unverified":
        stance = "support" if assessment.verdict == "support" else "contradict"
        relevant = [c for c in assessment.citations if c.stance == stance]
        if not relevant:
            raise ValueError("Verdict lacks a matching quoted evidence stance")
        if all(docs[c.document_id].kind == "snippet" for c in relevant):
            raise ValueError("Search snippets alone cannot establish a verified verdict; fetch a primary page")
    return assessment


class ToolExecutor:
    def __init__(self, settings: Settings, store: Store, sources: Sources, model,
                 memory: Memory, skills: SkillRegistry):
        self.settings, self.store, self.sources, self.model = settings, store, sources, model
        self.memory, self.skills = memory, skills

    async def execute(self, state: RunState, action: str, arguments: dict, task_id: str = "") -> Observation:
        if action not in SPECS:
            raise ValueError("Unknown tool/action")
        args = SPECS[action][0].model_validate(arguments)
        task = next((t for t in state.tasks if t.id == task_id), None)
        if task_id and task is None:
            raise ValueError("Task ID is not in the current plan")
        if action not in CONTROL and not self.skills.allowed(action, state.loaded_skills):
            raise ValueError(f"Load the skill advertising {action} before calling it")
        if action == "load_skill":
            skill = self.skills.get(args.name)
            prospective = list(dict.fromkeys(state.loaded_skills + [skill.name]))
            size = token_upper_bound(json.dumps({n: self.skills.get(n).body for n in prospective}))
            if size > self.settings.context.max_skill_tokens:
                raise ValueError("Skill context allowance reached; unload an unused skill first")
            state.loaded_skills = prospective
            return Observation(action=action, status="ok", summary=f"Loaded {skill.name}",
                               data={"skill": skill.name, "revision": skill.revision})
        if action == "unload_skill":
            if args.name == "research-planning":
                raise ValueError("The run's planning contract remains loaded")
            state.loaded_skills = [n for n in state.loaded_skills if n != args.name]
            state.loaded_references = {k: v for k, v in state.loaded_references.items()
                                       if not k.startswith(args.name + "/")}
            return Observation(action=action, status="ok", summary="Skill unloaded")
        if action == "read_reference":
            if args.skill not in state.loaded_skills:
                raise ValueError("Load the parent skill first")
            text = self.skills.reference(args.skill, args.path)
            if len(text.encode()) > 2500:
                raise ValueError("Reference exceeds the inline reference limit")
            combined = {"skills": {n: self.skills.get(n).body for n in state.loaded_skills},
                        "references": {**state.loaded_references, f"{args.skill}/{args.path}": text}}
            if token_upper_bound(json.dumps(combined, ensure_ascii=False)) > self.settings.context.max_skill_tokens:
                raise ValueError("Skill/reference context allowance reached; unload an unused skill first")
            state.loaded_references[f"{args.skill}/{args.path}"] = text
            return Observation(action=action, status="ok", summary="Reference loaded")
        if action == "memory_search":
            recalled = await asyncio.to_thread(self.memory.recall, args.query)
            ids = [r["id"] for r in recalled]
            state.evidence_ids = list(dict.fromkeys(state.evidence_ids + ids))
            return Observation(action=action, task_id=task_id, status="ok", summary=f"Recalled {len(ids)} historical sources",
                               document_ids=ids, data={"memory": recalled})
        if action in {"search_web", "search_papers", "search_github"}:
            if action == "search_web":
                docs = await self.sources.web_search(state, args.query)
            elif action == "search_papers":
                docs = await self.sources.papers(args.query)
            else:
                docs = await self.sources.github(args.query)
            return self._ingest(state, action, docs, task)
        if action in {"fetch_page", "read_paper", "read_document", "find_in_document", "browser_read"}:
            known_ids = set(state.candidate_ids + state.evidence_ids)
            if args.document_id not in known_ids:
                raise ValueError("Document must be an observed candidate or recalled source")
            docs = self.store.documents([args.document_id])
            if not docs:
                raise ValueError("Document not found")
            doc = docs[0]
            if action == "find_in_document":
                import re
                hits = [{"offset": m.start(), "text": doc.text[max(0, m.start() - 180):m.start() + 420]}
                        for m in list(re.finditer(re.escape(args.query), doc.text, re.IGNORECASE))[:6]]
                return Observation(action=action, task_id=task_id, status="ok", summary=f"Found {len(hits)} phrase matches",
                                   document_ids=[doc.id], data={"matches": hits, "document_length": len(doc.text)})
            if action == "read_document":
                text = excerpt(doc.text, args.focus, 2000) if args.focus else doc.text[args.offset:args.offset + 2000]
                duplicate = any(o.action == action and o.document_ids == [doc.id] and o.data.get("text") == text
                                for o in state.observations)
                return Observation(action=action, task_id=task_id, status="ok", summary="Stored excerpt retrieved",
                                   document_ids=[doc.id], data={"text": text, "document_length": len(doc.text),
                                   "duplicate_excerpt": duplicate, "next_offset": min(args.offset + 2000, len(doc.text)),
                                   "hint": "Use find_in_document for specific terms or read_document with empty focus and offset to page through text. Repeated excerpts do not close evidence gaps."})
            if action == "read_paper":
                from urllib.parse import urlsplit
                p = urlsplit(doc.url)
                if p.hostname != "huggingface.co" or not p.path.startswith("/papers/"):
                    raise ValueError("read_paper requires a HuggingFace paper URL")
                read_url = doc.url.removesuffix(".md").split("?", 1)[0] + ".md"
                fetched = await self.sources.fetch(read_url)
            elif action == "browser_read":
                selectors = {c["selector"] for o in state.observations if o.action == "browser_read"
                             and o.data.get("browser_url") == doc.url for c in o.data.get("controls", [])}
                if any(step.selector not in selectors for step in args.steps):
                    raise ValueError("First call browser_read with no steps; use a selector returned by that snapshot")
                fetched, controls = await self._browser(doc.url, args.steps)
                observation = self._ingest(state, action, [fetched], task)
                observation.data.update({"browser_url": fetched.url, "controls": controls})
                return observation
            else:
                fetched = await self.sources.fetch(doc.url)
            return self._ingest(state, action, [fetched], task)
        if action == "assess_claim":
            if task is None:
                raise ValueError("A claim assessment must belong to a research task")
            if task.dimension == "benchmark" and "benchmark-verification" not in state.loaded_skills:
                raise ValueError("Load benchmark-verification before assessing a benchmark claim")
            known = set(state.candidate_ids + state.evidence_ids)
            if not set(args.document_ids) <= known:
                raise ValueError("Evidence was not observed in this run")
            docs = self.store.documents(args.document_ids)
            context = {"claim": args.claim, "entity": args.entity, "goal": task.goal,
                       "missing_fields": task.missing,
                       "evidence": [{"id": d.id, "title": d.title, "url": d.url, "kind": d.kind,
                                     "text": excerpt(d.text, args.claim, 1800), "date": d.published_at} for d in docs],
                       "verification_rules": self.skills.get("evidence-review").body}
            if task.dimension == "benchmark":
                context["benchmark_rules"] = self.skills.get("benchmark-verification").body
            assessment = await self.model.generate(
                state, "claim_assessment", "Evaluate only the exact stated claim. Use exact quotes. "
                "A missing version, scope or measurement makes a broader claim unverified. "
                "Separate search snippets from primary material. resolved_gaps may only contain exact strings "
                "from missing_fields; omit any that remain unresolved. Preserve the exact input claim. Output JSON.", context, ClaimAssessment)
            if assessment.verdict == "contradict":
                context["initial_assessment"] = assessment.model_dump()
                assessment = await self.model.generate(
                    state, "conflict_adjudication", "Adjudicate the conflict against the quoted primary evidence. "
                    "Preserve the exact claim, distinguish contradiction from missing evidence, and output JSON.",
                    context, ClaimAssessment, strong=True)
            if normalized(assessment.claim) != normalized(args.claim):
                raise ValueError("Assessment changed the claim under review")
            validate_assessment(assessment, docs)
            if not set(assessment.resolved_gaps) <= set(task.missing):
                raise ValueError("Assessment resolved a gap not present in the task")
            cid = self.store.save_claim(state.run_id, task.id, assessment)
            state.claim_ids.append(cid)
            if assessment.verdict != "unverified":
                task.missing = [gap for gap in task.missing if gap not in assessment.resolved_gaps]
            return Observation(action=action, task_id=task_id, status="ok", summary=assessment.rationale,
                               document_ids=args.document_ids, data={"claim_id": cid, "verdict": assessment.verdict})
        if action == "complete_task":
            if task is None:
                raise ValueError("Task required")
            claims = [c for c in self.store.claims(state.claim_ids) if c["task_id"] == task.id]
            if not claims or claims[-1]["payload"]["verdict"] == "unverified" or task.missing:
                raise ValueError("Task still has unresolved evidence requirements; assess or defer it")
            task.status = "done"
            self.store.save_task(state.run_id, task)
            return Observation(action=action, task_id=task_id, status="ok", summary="Task completed with an assessment")
        if action == "defer_task":
            if task is None:
                raise ValueError("Task required")
            task.reason = args.reason
            self.store.defer_task(state.run_id, task, self.settings.memory.retry_after_hours)
            return Observation(action=action, task_id=task_id, status="ok", summary="Unresolved task preserved for retry")
        if action == "profile_dataset":
            result = await asyncio.to_thread(self._profile, args)
            doc = Document(url="artifact://dataset/" + digest(args.filename), title="Bounded dataset profile",
                           text=json.dumps(result, ensure_ascii=False), source="local-profile", kind="full_text",
                           dimension="supplier")
            return self._ingest(state, action, [doc], task)
        if action == "finish":
            state.finish_reason = "model_finished"
            return Observation(action=action, status="ok", summary="Research stage finished")
        raise ValueError("Action is not implemented")

    def _ingest(self, state, action, docs, task):
        saved = []
        for doc in docs[:12]:
            if task:
                doc.dimension = task.dimension
                if task.entity:
                    doc.entities = [task.entity]
            doc = self.store.save_document(state.run_id, doc)
            saved.append(doc)
        ids = [d.id for d in saved]
        state.evidence_ids = list(dict.fromkeys(state.evidence_ids + ids))
        if task:
            task.document_ids = list(dict.fromkeys(task.document_ids + ids))[-12:]
            task.status = "running"
        return Observation(action=action, task_id=task.id if task else "", status="ok",
                           summary=f"Retrieved {len(ids)} evidence documents", document_ids=ids,
                           data={"sources": [{"id": d.id, "title": d.title, "url": d.url,
                                              "kind": d.kind, "preview": d.text[:240]} for d in saved]})

    def _profile(self, args: ProfileArgs):
        root = (self.settings.path(self.settings.data_dir) / "samples").resolve()
        path = root / args.filename
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError("Dataset must be a regular file inside data/samples")
        if path.stat().st_size > 8_000_000:
            raise ValueError("Sample exceeds the 8MB inspection limit")
        if path.suffix.lower() in {".csv", ".tsv"}:
            with path.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f, delimiter="\t" if path.suffix == ".tsv" else ",")
                rows = []
                for row in reader:
                    rows.append(row)
                    if len(rows) >= 10001:
                        break
        elif path.suffix.lower() == ".json":
            rows = json.loads(path.read_text())
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ValueError("JSON sample must be an array of objects")
        else:
            raise ValueError("Supported samples are CSV, TSV and JSON")
        truncated = len(rows) > 10000
        rows = rows[:10000]
        fields = sorted({k for row in rows for k in row if isinstance(k, str)})[:100]
        nulls = {k: sum(row.get(k) in (None, "") for row in rows) for k in fields}
        result: dict[str, Any] = {"rows_scanned": len(rows), "truncated": truncated,
                                  "sampling": "bounded prefix; not a representative population estimate",
                                  "fields": fields, "missing_counts": nulls}
        if args.id_field and args.split_field:
            groups: dict[str, set] = {}
            for row in rows:
                identifier, split = row.get(args.id_field), row.get(args.split_field)
                if identifier is not None and split is not None:
                    groups.setdefault(str(identifier), set()).add(str(split))
            result["cross_split_identifier_count"] = sum(len(s) > 1 for s in groups.values())
        result["duplicate_row_count"] = sum(n - 1 for n in Counter(digest(r) for r in rows).values())
        return result

    async def _browser(self, url: str, steps: list[BrowserStep]) -> tuple[Document, list[dict]]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ValueError("Browser extra is not installed; use another source") from exc
        from .network import public_addresses
        await public_addresses(url)
        count = 0
        async with asyncio.timeout(40):
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(service_workers="block", accept_downloads=False)

                    async def route_request(route):
                        nonlocal count
                        count += 1
                        request = route.request
                        if count > 35 or request.method not in {"GET", "HEAD"} or request.resource_type in {"image", "media", "font"}:
                            await route.abort()
                            return
                        try:
                            body, _, headers = await self.sources.http.request("GET", request.url, max_bytes=1_000_000)
                            safe_headers = {k: v for k, v in headers.items()
                                            if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "set-cookie"}}
                            await route.fulfill(status=200, body=body, headers=safe_headers)
                        except Exception:
                            await route.abort()

                    await context.route("**/*", route_request)
                    if hasattr(context, "route_web_socket"):
                        await context.route_web_socket("**/*", lambda ws: ws.close())
                    page = await context.new_page()
                    page.set_default_timeout(8000)
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(500)
                    for step in steps:
                        locator = page.locator(step.selector)
                        if step.operation == "select":
                            await locator.select_option(step.value)
                        else:
                            await locator.click()
                        await page.wait_for_timeout(300)
                    text = await page.locator("body").inner_text()
                    controls = await page.locator("button,select,a[href]").evaluate_all("""elements => elements.slice(0,30).map(e => ({
                        selector: e.id ? '#' + CSS.escape(e.id) : e.tagName.toLowerCase() + ':nth-of-type(' +
                            ([...e.parentElement.children].filter(s => s.tagName === e.tagName).indexOf(e)+1) + ')',
                        label: (e.innerText || e.getAttribute('aria-label') || '').slice(0,120),
                        tag: e.tagName.toLowerCase(),
                        options: e.tagName === 'SELECT' ? [...e.options].slice(0,20).map(o=>({value:o.value,label:o.label})) : []
                    }))""")
                    # Only unambiguous selectors are usable on a subsequent snapshot.
                    controls = [c for c in controls if await page.locator(c["selector"]).count() == 1]
                    return (Document(url=page.url, title=await page.title(), text=text[:40000],
                                     source="browser", kind="full_text"), controls)
                finally:
                    await browser.close()
