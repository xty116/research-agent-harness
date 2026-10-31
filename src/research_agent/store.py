from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .domain import ClaimAssessment, Document, ResearchTask, RunState, digest, utcnow
from .settings import BudgetSettings


class BudgetExceeded(RuntimeError):
    pass


class RunBusy(RuntimeError):
    pass


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, daily_key TEXT UNIQUE, local_day TEXT NOT NULL, mode TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 checkpoint TEXT NOT NULL, report_path TEXT, error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_day_mode ON runs(local_day, mode, started_at);
CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
 source TEXT NOT NULL, published_at TEXT, fetched_at TEXT NOT NULL, kind TEXT NOT NULL,
 dimension TEXT NOT NULL, entities TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_url ON documents(url);
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(id UNINDEXED, title, text, entities);
CREATE TABLE IF NOT EXISTS run_documents (
 run_id TEXT REFERENCES runs(id), document_id TEXT REFERENCES documents(id),
 PRIMARY KEY(run_id, document_id)
);
CREATE TABLE IF NOT EXISTS claims (
 id TEXT PRIMARY KEY, question_key TEXT NOT NULL, run_id TEXT REFERENCES runs(id),
 task_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claim_question ON claims(question_key, created_at);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, question_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
 last_run_id TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), purpose TEXT NOT NULL,
 reserved_usd REAL NOT NULL, actual_usd REAL, credits INTEGER NOT NULL,
 status TEXT NOT NULL, usage_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vectors (
 document_id TEXT PRIMARY KEY REFERENCES documents(id), model TEXT NOT NULL, vector TEXT NOT NULL
);
PRAGMA user_version=1;
"""


class Store:
    """Durable research, leases and reservations behind one transactional interface."""

    def __init__(self, path: Path, timezone: str = "UTC"):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.timezone = timezone
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "local_day" not in {r[1] for r in self.db.execute("PRAGMA table_info(usage)")}:
            self.db.execute("ALTER TABLE usage ADD COLUMN local_day TEXT")
            self.db.execute("UPDATE usage SET local_day=(SELECT local_day FROM runs WHERE runs.id=usage.run_id)")
            self.db.commit()

    def billing_day(self) -> str:
        return datetime.now(ZoneInfo(self.timezone)).date().isoformat()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def acquire(self, name: str, owner: str, ttl: int = 180):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE name=?", (name,)).fetchone()
            if row and row["expires"] > time.time() and row["owner"] != owner:
                raise RunBusy("Another research run owns the execution lease")
            db.execute("INSERT OR REPLACE INTO leases VALUES (?,?,?)", (name, owner, time.time() + ttl))

    def renew(self, name: str, owner: str, ttl: int = 180):
        with self.transaction() as db:
            cur = db.execute("UPDATE leases SET expires=? WHERE name=? AND owner=?",
                             (time.time() + ttl, name, owner))
            if not cur.rowcount:
                raise RunBusy("Execution lease lost")

    def release(self, name: str, owner: str):
        with self.transaction() as db:
            db.execute("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))

    def create_run(self, state: RunState, daily_key: str | None = None):
        with self.transaction() as db:
            db.execute("INSERT INTO runs(id,daily_key,local_day,mode,status,started_at,updated_at,checkpoint) "
                       "VALUES (?,?,?,?,?,?,?,?)", (state.run_id, daily_key, state.local_day, state.mode,
                                                  "running", state.started_at, utcnow(), state.model_dump_json()))

    def checkpoint(self, state: RunState):
        with self.transaction() as db:
            db.execute("UPDATE runs SET checkpoint=?, updated_at=? WHERE id=?",
                       (state.model_dump_json(), utcnow(), state.run_id))

    def finish(self, state: RunState, status: str, report_path: str = "", error: str = ""):
        with self.transaction() as db:
            db.execute("UPDATE runs SET status=?,checkpoint=?,updated_at=?,report_path=?,error=? WHERE id=?",
                       (status, state.model_dump_json(), utcnow(), report_path, error[:500], state.run_id))

    def get_run(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def daily_run(self, key: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE daily_key=?", (key,)).fetchone()
            return dict(row) if row else None

    def runs(self, limit: int = 30) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id,mode,local_day,status,started_at,updated_at,report_path,error FROM runs "
                "ORDER BY started_at DESC LIMIT ?", (min(limit, 100),))]

    def report_catalog(self, *, page=1, page_size=12, query="", status="", kind="", start="", end=""):
        kind_sql = ("CASE WHEN mode='demo' THEN 'demo' WHEN id GLOB 'eval_*' THEN 'evaluation' "
                    "WHEN id GLOB 'accept_*' THEN 'acceptance' WHEN id GLOB 'source-health*' "
                    "THEN 'diagnostic' ELSE 'research' END")
        clauses, args = ["1=1"], []
        for column, value, operator in (("status", status, "="), (kind_sql, kind, "="),
                                        ("local_day", start, ">="), ("local_day", end, "<=")):
            if value:
                clauses.append(f"{column} {operator} ?")
                args.append(value)
        if query:
            literal = query[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(id LIKE ? ESCAPE '\\' OR COALESCE(json_extract(checkpoint,'$.draft.headline'),'') LIKE ? ESCAPE '\\')")
            args += [f"%{literal}%"] * 2
        where = " AND ".join(clauses)
        page, page_size = max(1, int(page)), max(1, min(int(page_size), 100))
        with self.lock:
            total = self.db.execute(f"SELECT COUNT(*) FROM runs WHERE {where}", args).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = self.db.execute(f"SELECT *, {kind_sql} AS kind FROM runs WHERE {where} "
                                   "ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?",
                                   [*args, page_size, (page - 1) * page_size]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "page": page, "pages": pages,
                "page_size": page_size}

    def all_runs(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY started_at DESC,id DESC")]

    def attach_report(self, run_id: str, path: str):
        with self.transaction() as db:
            db.execute("UPDATE runs SET report_path=? WHERE id=?", (path, run_id))

    def run_documents(self, run_id: str) -> list[Document]:
        with self.lock:
            ids = [r[0] for r in self.db.execute("SELECT document_id FROM run_documents WHERE run_id=?", (run_id,))]
        return self.documents(ids)

    def overview_counts(self):
        with self.lock:
            return {name: self.db.execute(sql).fetchone()[0] for name, sql in {
                "runs": "SELECT COUNT(*) FROM runs",
                "reports": "SELECT COUNT(*) FROM runs WHERE status NOT IN ('running')",
                "documents": "SELECT COUNT(*) FROM documents",
                "claims": "SELECT COUNT(*) FROM claims",
                "verified_claims": "SELECT COUNT(*) FROM claims WHERE json_extract(payload,'$.verdict')<>'unverified'",
                "vectors": "SELECT COUNT(*) FROM vectors",
            }.items()}

    def query_claims(self, *, query="", verdict="", page=1, page_size=12):
        clauses, args = ["1=1"], []
        if query:
            clauses.append("(json_extract(payload,'$.claim') LIKE ? OR json_extract(payload,'$.entity') LIKE ?)")
            args += [f"%{query[:200]}%"] * 2
        if verdict:
            clauses.append("json_extract(payload,'$.verdict')=?")
            args.append(verdict)
        where = " AND ".join(clauses)
        with self.lock:
            total = self.db.execute(f"SELECT COUNT(*) FROM claims WHERE {where}", args).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            rows = self.db.execute(f"SELECT * FROM claims WHERE {where} ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?",
                                   [*args, page_size, (page - 1) * page_size]).fetchall()
        return {"items": [{**dict(r), "payload": json.loads(r["payload"])} for r in rows],
                "total": total, "page": page, "pages": pages}

    def event(self, run_id: str, kind: str, payload: Any):
        with self.transaction() as db:
            db.execute("INSERT INTO events(run_id,kind,payload,created_at) VALUES (?,?,?,?)",
                       (run_id, kind, json.dumps(payload, ensure_ascii=False, default=str), utcnow()))

    def events(self, run_id: str, after: int = 0, limit: int = 300) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                                   (run_id, after, min(limit, 1000))).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def save_document(self, run_id: str, doc: Document) -> Document:
        doc = doc.identified()
        with self.transaction() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
                (doc.id, doc.url, doc.title, doc.text, doc.source, doc.published_at,
                 doc.fetched_at, doc.kind, doc.dimension, json.dumps(doc.entities, ensure_ascii=False)),
            ).rowcount
            if inserted:
                db.execute("INSERT INTO document_fts VALUES (?,?,?,?)",
                           (doc.id, doc.title, doc.text, " ".join(doc.entities)))
            db.execute("INSERT OR IGNORE INTO run_documents VALUES (?,?)", (run_id, doc.id))
        return doc

    def documents(self, ids: list[str]) -> list[Document]:
        if not ids:
            return []
        with self.lock:
            rows = self.db.execute("SELECT * FROM documents WHERE id IN (" + ",".join("?" * len(ids)) + ")",
                                   ids).fetchall()
        by_id = {r["id"]: Document(**{**dict(r), "entities": json.loads(r["entities"])}) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def is_novel(self, document_id: str, run_id: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT 1 FROM run_documents WHERE document_id=? AND run_id<>? LIMIT 1",
                                  (document_id, run_id)).fetchone()
        return row is None

    def search_documents(self, terms: list[str], limit: int = 8) -> list[Document]:
        terms = [t.strip()[:80] for t in terms if t.strip()][:12]
        if not terms:
            return []
        query = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        with self.lock:
            ids = [r[0] for r in self.db.execute(
                "SELECT id FROM document_fts WHERE document_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit))]
        return self.documents(ids)

    def save_claim(self, run_id: str, task_id: str, claim: ClaimAssessment) -> str:
        question_key = digest([claim.entity.casefold(), claim.claim.casefold()])
        cid = "claim_" + uuid.uuid4().hex[:16]
        with self.transaction() as db:
            db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?)",
                       (cid, question_key, run_id, task_id, claim.model_dump_json(), utcnow()))
        return cid

    def claims(self, ids: list[str] | None = None, limit: int = 40) -> list[dict]:
        with self.lock:
            if ids is not None:
                if not ids:
                    return []
                rows = self.db.execute("SELECT * FROM claims WHERE id IN (" + ",".join("?" * len(ids)) + ")",
                                       ids).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM claims ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        result = [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
        if ids is not None:
            indexed = {r["id"]: r for r in result}
            return [indexed[i] for i in ids if i in indexed]
        return result

    def latest_claims(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY question_key "
                "ORDER BY created_at DESC, rowid DESC) AS revision_rank FROM claims) "
                "WHERE revision_rank=1 ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def save_task(self, run_id: str, task: ResearchTask):
        key = digest([task.dimension, task.entity.casefold(), task.goal.casefold()])
        with self.transaction() as db:
            existing = db.execute("SELECT id FROM tasks WHERE question_key=?", (key,)).fetchone()
            if existing:
                task.id = existing[0]
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "payload=excluded.payload,last_run_id=excluded.last_run_id,updated_at=excluded.updated_at",
                       (task.id, key, task.model_dump_json(), run_id, utcnow()))

    def recall_tasks(self, limit: int = 8, include_not_due: bool = False) -> list[ResearchTask]:
        with self.lock:
            rows = self.db.execute("SELECT payload FROM tasks ORDER BY updated_at DESC LIMIT 100").fetchall()
        tasks = [ResearchTask.model_validate_json(r[0]) for r in rows]
        return [t for t in tasks if t.status not in {"done", "archived"} and (
            include_not_due or not t.retry_after or t.retry_after <= utcnow())][:limit]

    def defer_task(self, run_id: str, task: ResearchTask, hours: int):
        task.status = "deferred"
        task.retry_after = (datetime.now(UTC) + timedelta(hours=hours)).isoformat()
        self.save_task(run_id, task)

    def archive_task(self, task_id: str, reason: str):
        if not reason.strip():
            raise ValueError("Archival requires a reason")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown task")
            task = ResearchTask.model_validate_json(row["payload"])
            before = task.status
            task.status = "archived"
            task.reason = ("Administrative archival: " + reason)[:300]
            db.execute("UPDATE tasks SET payload=?,updated_at=? WHERE id=?", (task.model_dump_json(), utcnow(), task_id))
            db.execute("INSERT INTO events(run_id,kind,payload,created_at) VALUES (?,?,?,?)", (
                row["last_run_id"], "task_archived", json.dumps({"task_id": task_id, "before": before, "reason": reason}), utcnow()))

    def usage(self, run_id: str | None = None, day: str | None = None, mode: str = "live") -> dict:
        sql = "SELECT COALESCE(SUM(COALESCE(u.actual_usd,u.reserved_usd)),0), " \
              "COALESCE(SUM(u.credits),0),COUNT(*) FROM usage u JOIN runs r ON r.id=u.run_id WHERE r.mode=?"
        args: list[Any] = [mode]
        if run_id:
            sql += " AND u.run_id=?"
            args.append(run_id)
        if day:
            sql += " AND u.local_day=?"
            args.append(day)
        with self.lock:
            r = self.db.execute(sql, args).fetchone()
        return {"usd": round(r[0], 8), "search_credits": r[1], "requests": r[2]}

    def reserve(self, state: RunState, purpose: str, usd: float, credits: int,
                limits: BudgetSettings, report: bool = False) -> str:
        if usd < 0 or credits < 0:
            raise ValueError("negative reservation")
        with self.transaction() as db:
            def total(where, value):
                r = db.execute("SELECT COALESCE(SUM(COALESCE(u.actual_usd,u.reserved_usd)),0),"
                               "COALESCE(SUM(u.credits),0) FROM usage u JOIN runs r ON r.id=u.run_id "
                               f"WHERE r.mode=? AND {where}=?", (state.mode, value)).fetchone()
                return r[0], r[1]
            run_usd, run_credits = total("u.run_id", state.run_id)
            billing_day = self.billing_day()
            day_usd, day_credits = total("u.local_day", billing_day)
            reserve = 0 if report else limits.report_reserve_usd
            if run_usd + usd > limits.per_run_usd - reserve + 1e-9:
                raise BudgetExceeded("Run model estimate budget reached; report reserve protected")
            if day_usd + usd > limits.per_day_usd - reserve + 1e-9:
                raise BudgetExceeded("Daily model estimate budget reached")
            if run_credits + credits > limits.search_credits_per_run or day_credits + credits > limits.search_credits_per_day:
                raise BudgetExceeded("Search credit budget reached")
            rid = uuid.uuid4().hex
            db.execute("INSERT INTO usage(id,run_id,purpose,reserved_usd,credits,status,created_at,local_day) "
                       "VALUES (?,?,?,?,?,?,?,?)", (rid, state.run_id, purpose, usd, credits, "held", utcnow(), billing_day))
            return rid

    def settle(self, reservation: str, actual_usd: float, usage: dict | None = None,
               credits: int | None = None):
        with self.transaction() as db:
            if credits is None:
                db.execute("UPDATE usage SET actual_usd=?,status='settled',usage_json=? WHERE id=?",
                           (max(0, actual_usd), json.dumps(usage or {}), reservation))
            else:
                db.execute("UPDATE usage SET actual_usd=?,credits=?,status='settled',usage_json=? WHERE id=?",
                           (max(0, actual_usd), max(0, credits), json.dumps(usage or {}), reservation))

    def usage_rows(self, run_id: str) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM usage WHERE run_id=?", (run_id,))]

    def backup(self, target: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, sqlite3.connect(target) as out:
            self.db.backup(out)
