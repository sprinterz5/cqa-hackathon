"""SQLite storage. JSON documents in a few tables; no ORM.

Tables
  checklists       every saved version of every checklist (never overwritten)
  dialogs          raw records in the organizers' format
  evaluations      one AI evaluation run of a dialog with a checklist version
  criterion_cache  per-criterion results keyed by everything that affects them
  appeals          an operator's dispute of one criterion of an evaluation
  decisions        the reviewer's decision on an appeal
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS checklists (
  id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (id, version));
CREATE TABLE IF NOT EXISTS dialogs (
  conversation_id TEXT PRIMARY KEY, group_id TEXT, channel TEXT, language TEXT,
  raw TEXT NOT NULL, source TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, checklist_id TEXT NOT NULL,
  checklist_version INTEGER NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_eval_dialog ON evaluations (conversation_id, checklist_id);
CREATE TABLE IF NOT EXISTS criterion_cache (key TEXT PRIMARY KEY, result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS appeals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id INTEGER NOT NULL, criterion_id TEXT NOT NULL,
  comment TEXT NOT NULL, author TEXT, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, appeal_id INTEGER NOT NULL, decision TEXT NOT NULL,
  answer TEXT, value REAL, reason TEXT NOT NULL, reviewer TEXT, created_at TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    def _insert(self, sql: str, args: tuple) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    # --- checklists ---
    def save_checklist(self, data: dict) -> int:
        """Store a new version; returns the version number."""
        rows = self._q("SELECT MAX(version) AS v FROM checklists WHERE id = ?", (data["id"],))
        version = (rows[0]["v"] or 0) + 1
        data = {**data, "version": version}
        self._insert("INSERT INTO checklists VALUES (?, ?, ?, ?)",
                     (data["id"], version, json.dumps(data, ensure_ascii=False), now()))
        return version

    def get_checklist(self, cid: str, version: int | None = None) -> dict | None:
        if version is None:
            rows = self._q("SELECT data FROM checklists WHERE id = ? ORDER BY version DESC LIMIT 1", (cid,))
        else:
            rows = self._q("SELECT data FROM checklists WHERE id = ? AND version = ?", (cid, version))
        return json.loads(rows[0]["data"]) if rows else None

    def list_checklists(self) -> list[dict]:
        rows = self._q("""SELECT c.data, c.created_at FROM checklists c
                          JOIN (SELECT id, MAX(version) v FROM checklists GROUP BY id) m
                          ON c.id = m.id AND c.version = m.v ORDER BY c.id""")
        return [{**json.loads(r["data"]), "updated_at": r["created_at"]} for r in rows]

    def checklist_versions(self, cid: str) -> list[dict]:
        rows = self._q("SELECT version, created_at FROM checklists WHERE id = ? ORDER BY version DESC", (cid,))
        return [dict(r) for r in rows]

    # --- dialogs ---
    def upsert_dialog(self, record: dict, group_id: str | None, source: str) -> None:
        self._q("""INSERT INTO dialogs VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET group_id=excluded.group_id,
                   channel=excluded.channel, language=excluded.language, raw=excluded.raw,
                   source=excluded.source""",
                (record["conversation_id"], group_id, record.get("channel"), record.get("language"),
                 json.dumps(record, ensure_ascii=False), source, now()))

    def get_dialog(self, conversation_id: str) -> dict | None:
        rows = self._q("SELECT * FROM dialogs WHERE conversation_id = ?", (conversation_id,))
        if not rows:
            return None
        r = dict(rows[0])
        r["raw"] = json.loads(r["raw"])
        return r

    def list_dialogs(self, group_id: str | None = None) -> list[dict]:
        if group_id:
            rows = self._q("SELECT conversation_id, group_id, channel, language, source, created_at, raw "
                           "FROM dialogs WHERE group_id = ? ORDER BY rowid", (group_id,))
        else:
            rows = self._q("SELECT conversation_id, group_id, channel, language, source, created_at, raw "
                           "FROM dialogs ORDER BY rowid")
        out = []
        for r in rows:
            d = dict(r)
            d["messages"] = len(json.loads(d.pop("raw")).get("transcript") or [])
            out.append(d)
        return out

    def groups(self) -> list[str]:
        return [r["group_id"] for r in self._q(
            "SELECT DISTINCT group_id FROM dialogs WHERE group_id IS NOT NULL ORDER BY group_id")]

    # --- evaluations ---
    def add_evaluation(self, result: dict) -> int:
        return self._insert("INSERT INTO evaluations (conversation_id, checklist_id, checklist_version, result, "
                            "created_at) VALUES (?, ?, ?, ?, ?)",
                            (result["conversation_id"], result["checklist_id"], result["checklist_version"],
                             json.dumps(result, ensure_ascii=False), now()))

    def get_evaluation(self, eid: int) -> dict | None:
        rows = self._q("SELECT * FROM evaluations WHERE id = ?", (eid,))
        return self._eval_row(rows[0]) if rows else None

    def latest_evaluation(self, conversation_id: str, checklist_id: str | None = None,
                          version: int | None = None) -> dict | None:
        if checklist_id and version is not None:
            rows = self._q("SELECT * FROM evaluations WHERE conversation_id = ? AND checklist_id = ? "
                           "AND checklist_version = ? ORDER BY id DESC LIMIT 1", (conversation_id, checklist_id, version))
        elif checklist_id:
            rows = self._q("SELECT * FROM evaluations WHERE conversation_id = ? AND checklist_id = ? "
                           "ORDER BY id DESC LIMIT 1", (conversation_id, checklist_id))
        else:
            rows = self._q("SELECT * FROM evaluations WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                           (conversation_id,))
        return self._eval_row(rows[0]) if rows else None

    def latest_evaluations(self, checklist_id: str | None = None) -> dict[str, dict]:
        rows = self._q("""SELECT e.* FROM evaluations e JOIN (SELECT conversation_id, MAX(id) mid
                          FROM evaluations WHERE (? IS NULL OR checklist_id = ?) GROUP BY conversation_id) m
                          ON e.id = m.mid""", (checklist_id, checklist_id))
        return {r["conversation_id"]: self._eval_row(r) for r in rows}

    def evaluation_history(self, conversation_id: str) -> list[dict]:
        rows = self._q("SELECT id, checklist_id, checklist_version, created_at FROM evaluations "
                       "WHERE conversation_id = ? ORDER BY id DESC", (conversation_id,))
        return [dict(r) for r in rows]

    @staticmethod
    def _eval_row(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "created_at": r["created_at"], **json.loads(r["result"])}

    # --- cache ---
    def cache_get(self, key: str) -> dict | None:
        rows = self._q("SELECT result FROM criterion_cache WHERE key = ?", (key,))
        return json.loads(rows[0]["result"]) if rows else None

    def cache_put(self, key: str, result: dict) -> None:
        self._q("INSERT OR REPLACE INTO criterion_cache VALUES (?, ?, ?)",
                (key, json.dumps(result, ensure_ascii=False), now()))

    # --- appeals & decisions ---
    def add_appeal(self, evaluation_id: int, criterion_id: str, comment: str, author: str | None) -> int:
        return self._insert("INSERT INTO appeals (evaluation_id, criterion_id, comment, author, created_at) "
                            "VALUES (?, ?, ?, ?, ?)", (evaluation_id, criterion_id, comment, author, now()))

    def get_appeal(self, aid: int) -> dict | None:
        rows = self._q("SELECT * FROM appeals WHERE id = ?", (aid,))
        return dict(rows[0]) if rows else None

    def list_appeals(self, status: str | None = None, evaluation_id: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM appeals WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if evaluation_id is not None:
            sql += " AND evaluation_id = ?"
            args.append(evaluation_id)
        return [dict(r) for r in self._q(sql + " ORDER BY id DESC", tuple(args))]

    def add_decision(self, appeal_id: int, decision: str, answer: str | None, value: float | None,
                     reason: str, reviewer: str | None) -> int:
        did = self._insert("INSERT INTO decisions (appeal_id, decision, answer, value, reason, reviewer, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", (appeal_id, decision, answer, value, reason, reviewer, now()))
        self._q("UPDATE appeals SET status = 'resolved' WHERE id = ?", (appeal_id,))
        return did

    def decisions_for_evaluation(self, evaluation_id: int) -> list[dict]:
        rows = self._q("""SELECT d.*, a.criterion_id, a.comment, a.author FROM decisions d
                          JOIN appeals a ON a.id = d.appeal_id WHERE a.evaluation_id = ? ORDER BY d.id""",
                       (evaluation_id,))
        return [dict(r) for r in rows]

    def all_decisions(self) -> list[dict]:
        rows = self._q("""SELECT d.*, a.criterion_id, a.evaluation_id, e.conversation_id FROM decisions d
                          JOIN appeals a ON a.id = d.appeal_id JOIN evaluations e ON e.id = a.evaluation_id
                          ORDER BY d.id""")
        return [dict(r) for r in rows]
