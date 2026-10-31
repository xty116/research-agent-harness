from __future__ import annotations

import math
import re
import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from .domain import Document
from .settings import Settings
from .store import Store


def terms(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,8}", text)))[:12]


class Memory:
    """FTS plus optional local embeddings, with provenance and task lifecycle retained."""

    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self._embedding_model = None
        self._embedding_failed = False
        self._embedding_lock = threading.RLock()
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        self.embedding_status = ("not initialized (lexical recall active)" if settings.memory.embedding_enabled
                                 else "disabled (lexical recall active)")

    def _embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not self.settings.memory.embedding_enabled:
            return []
        with self._embedding_lock:
            if self._embedding_failed or not texts:
                return []
            if query and texts[0] in self._query_cache:
                self._query_cache.move_to_end(texts[0])
                return [self._query_cache[texts[0]]]
            return self._embed_locked(texts, query=query)

    def _embed_locked(self, texts: list[str], *, query: bool) -> list[list[float]]:
        try:
            if self._embedding_model is None:
                from fastembed import TextEmbedding
                self._embedding_model = TextEmbedding(
                    self.settings.memory.embedding_model,
                    cache_dir=str(self.settings.path(self.settings.memory.embedding_cache)),
                    threads=2,
                )
            prepared = ["Instruct: Given a research question, retrieve relevant evidence.\nQuery: " + t
                        for t in texts] if query else texts
            vectors = [v.tolist() for v in self._embedding_model.embed(prepared)]
            self.embedding_status = "active"
            if query:
                self._query_cache[texts[0]] = vectors[0]
                if len(self._query_cache) > 128:
                    self._query_cache.popitem(last=False)
            return vectors
        except Exception as exc:
            self._embedding_failed = True
            self.embedding_status = f"unavailable: {type(exc).__name__}; lexical recall active"
            return []

    def index(self, docs: list[Document]):
        if not self.settings.memory.embedding_enabled or not docs:
            return
        import json
        with self.store.lock:
            known = {r[0] for r in self.store.db.execute("SELECT document_id FROM vectors WHERE model=?",
                                                        (self.settings.memory.embedding_model,))}
        missing = [d for d in docs if d.id not in known][:30]
        vectors = self._embed([d.title + "\n" + d.text[:1200] for d in missing])
        with self.store.transaction() as db:
            for doc, vector in zip(missing, vectors, strict=False):
                db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?)",
                           (doc.id, self.settings.memory.embedding_model, json.dumps(vector)))

    def recall(self, query: str, limit: int | None = None) -> list[dict]:
        limit = limit or self.settings.memory.recall_limit
        found = self.store.search_documents(terms(query), limit * 2)
        with self.store.lock:
            rows = self.store.db.execute("SELECT document_id,vector FROM vectors WHERE model=?",
                                         (self.settings.memory.embedding_model,)).fetchall()
        vectors = self._embed([query], query=True) if rows else []
        semantic_ids = []
        if vectors:
            import json
            q = vectors[0]
            scored = []
            for row in rows:
                v = json.loads(row[1])
                if len(q) != len(v):
                    continue
                denominator = math.sqrt(sum(x*x for x in q) * sum(x*x for x in v)) or 1
                scored.append((sum(a*b for a, b in zip(q, v, strict=True))/denominator, row[0]))
            semantic_ids = [i for score, i in sorted(scored, reverse=True)[:limit * 2] if score > .25]
        # Reciprocal-rank fusion lets semantic matches compete with lexical results.
        ranks: dict[str, float] = {}
        for ranking in ([d.id for d in found], semantic_ids):
            for position, doc_id in enumerate(ranking):
                ranks[doc_id] = ranks.get(doc_id, 0) + 1 / (60 + position + 1)
        ranked_ids = sorted(ranks, key=lambda doc_id: (-ranks[doc_id], doc_id))
        found = self.store.documents(ranked_ids)
        cutoff = (datetime.now(UTC) - timedelta(days=self.settings.memory.history_days)).isoformat()
        unique = {d.id: d for d in found if d.fetched_at >= cutoff}
        return [{"id": d.id, "title": d.title, "url": d.url, "as_of": d.fetched_at,
                 "published_at": d.published_at, "text": d.text[:600], "kind": d.kind,
                 "use": "historical context; reverify time-sensitive conclusions"}
                for d in list(unique.values())[:limit]]

    def conclusions(self, query: str) -> list[dict]:
        keys = [t.casefold() for t in terms(query)]
        rows = self.store.latest_claims()
        selected = [r for r in rows if any(k in (r["payload"].get("entity", "") + " " +
                                                r["payload"]["claim"]).casefold() for k in keys)]
        return [{"id": r["id"], "as_of": r["created_at"], "question_key": r["question_key"],
                 "claim": r["payload"]["claim"], "verdict": r["payload"]["verdict"],
                 "rationale": r["payload"]["rationale"], "citations": r["payload"]["citations"],
                 "use": "dated prior conclusion, not automatically a current fact"}
                for r in selected[:self.settings.memory.recall_limit]]
