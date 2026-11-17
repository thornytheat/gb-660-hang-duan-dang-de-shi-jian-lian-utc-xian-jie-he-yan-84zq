"""索赔域 SQLite 仓储。

设计要点：
- 独立数据库文件，与既有 runs 库互不影响；写操作使用 IMMEDIATE 事务串行化。
- 索赔带 version 乐观锁，修订/结算必须带期望版本，旧版本一律拒绝。
- request_keys 记录所有写请求的幂等键：同键同载荷返回原回执，同键异载荷拒绝。
- 凭证与分录只追加、不更新、不删除，保证已入账凭证原样留存。
"""
import json
import os
import sqlite3
import threading
from pathlib import Path

from .ledger import voucher_balanced

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

DEFAULT_DB_PATH = Path(os.getenv("M7_DATABASE_PATH", "data/claims.sqlite3"))


def _lock_for(path: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[path] = lock
        return lock


class ClaimRepository:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = str(Path(db_path) if db_path else DEFAULT_DB_PATH)
        self._lock = _lock_for(self.db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def init_db(self) -> None:
        with self._lock, self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    claim_id TEXT PRIMARY KEY,
                    natural_key TEXT NOT NULL UNIQUE,
                    itinerary_id TEXT NOT NULL,
                    itinerary_json TEXT NOT NULL,
                    point_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    evidence_complete INTEGER NOT NULL,
                    delay_minutes INTEGER NOT NULL,
                    due_fen INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    denial_reason TEXT,
                    batch_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS claim_revisions (
                    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                    version_after INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    itinerary_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    delay_minutes INTEGER NOT NULL,
                    due_fen INTEGER NOT NULL,
                    evidence_complete INTEGER NOT NULL,
                    denial_reason TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS vouchers (
                    voucher_no TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                    voucher_type TEXT NOT NULL,
                    linked_voucher_no TEXT,
                    summary TEXT NOT NULL,
                    amount_fen INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS voucher_entries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    voucher_no TEXT NOT NULL REFERENCES vouchers(voucher_no),
                    account TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('DR','CR')),
                    amount_fen INTEGER NOT NULL CHECK(amount_fen > 0),
                    line_no INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_keys (
                    request_key TEXT PRIMARY KEY,
                    request_kind TEXT NOT NULL,
                    claim_id TEXT,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS registration_batches (
                    batch_id TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    PRIMARY KEY (batch_id, claim_id)
                );
                """
            )

    # ---------- 内部工具 ----------

    @staticmethod
    def _claim_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["itinerary"] = json.loads(data.pop("itinerary_json"))
        data["point"] = json.loads(data.pop("point_json"))
        data["evidence"] = json.loads(data.pop("evidence_json"))
        data["evidence_complete"] = bool(data["evidence_complete"])
        return data

    def get_claim_by_key(self, db: sqlite3.Connection, natural_key: str) -> dict | None:
        row = db.execute("SELECT * FROM claims WHERE natural_key = ?", (natural_key,)).fetchone()
        return self._claim_dict(row) if row else None

    def get_claim(self, claim_id: str) -> dict:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
            if row is None:
                raise LookupError("CLAIM_NOT_FOUND")
            return self._claim_dict(row)

    def list_claims(self) -> list[dict]:
        with self._lock, self.connect() as db:
            rows = db.execute("SELECT * FROM claims ORDER BY created_at, claim_id").fetchall()
            return [self._claim_dict(row) for row in rows]

    # ---------- 幂等键 ----------

    def load_request(self, request_key: str) -> dict | None:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM request_keys WHERE request_key = ?", (request_key,)).fetchone()
            return dict(row) if row else None

    def save_request(self, db: sqlite3.Connection, request_key: str, kind: str,
                     claim_id: str | None, request_hash: str, response: dict) -> None:
        db.execute(
            "INSERT INTO request_keys(request_key, request_kind, claim_id, request_hash, response_json) VALUES (?, ?, ?, ?, ?)",
            (request_key, kind, claim_id, request_hash, json.dumps(response, ensure_ascii=False, sort_keys=True)),
        )

    # ---------- 索赔写操作（须在 IMMEDIATE 事务内调用） ----------

    def insert_claim(self, db: sqlite3.Connection, claim: dict, batch_id: str) -> None:
        db.execute(
            """INSERT INTO claims(claim_id, natural_key, itinerary_id, itinerary_json, point_json,
               evidence_json, evidence_complete, delay_minutes, due_fen, status, version,
               denial_reason, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (claim["claim_id"], claim["natural_key"], claim["itinerary_id"],
             json.dumps(claim["itinerary"], ensure_ascii=False, sort_keys=True),
             json.dumps(claim["point"], ensure_ascii=False, sort_keys=True),
             json.dumps(claim["evidence"], ensure_ascii=False, sort_keys=True),
             int(claim["evidence_complete"]), claim["delay_minutes"], claim["due_fen"],
             claim["status"], claim["version"], claim.get("denial_reason"), batch_id),
        )
        db.execute("INSERT INTO registration_batches(batch_id, claim_id, created) VALUES (?, ?, 1)",
                   (batch_id, claim["claim_id"]))
        db.execute(
            """INSERT INTO claim_revisions(claim_id, version_after, reason, itinerary_json, evidence_json,
               delay_minutes, due_fen, evidence_complete, denial_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (claim["claim_id"], claim["version"], "REGISTER",
             json.dumps(claim["itinerary"], ensure_ascii=False, sort_keys=True),
             json.dumps(claim["evidence"], ensure_ascii=False, sort_keys=True),
             claim["delay_minutes"], claim["due_fen"], int(claim["evidence_complete"]),
             claim.get("denial_reason")),
        )

    def update_claim_revision(self, db: sqlite3.Connection, claim_id: str, expected_version: int,
                              itinerary: dict, evidence: list[dict], evidence_complete: bool,
                              delay_minutes: int, due_fen: int, reason: str,
                              denial_reason: str | None) -> dict:
        cursor = db.execute(
            """UPDATE claims SET itinerary_json = ?, evidence_json = ?, evidence_complete = ?,
               delay_minutes = ?, due_fen = ?, denial_reason = ?, version = version + 1,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE claim_id = ? AND version = ?""",
            (json.dumps(itinerary, ensure_ascii=False, sort_keys=True),
             json.dumps(evidence, ensure_ascii=False, sort_keys=True), int(evidence_complete),
             delay_minutes, due_fen, denial_reason, claim_id, expected_version),
        )
        if cursor.rowcount == 0:
            raise ValueError("STALE_VERSION")
        row = db.execute("SELECT version FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
        new_version = row["version"]
        db.execute(
            """INSERT INTO claim_revisions(claim_id, version_after, reason, itinerary_json, evidence_json,
               delay_minutes, due_fen, evidence_complete, denial_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (claim_id, new_version, reason,
             json.dumps(itinerary, ensure_ascii=False, sort_keys=True),
             json.dumps(evidence, ensure_ascii=False, sort_keys=True),
             delay_minutes, due_fen, int(evidence_complete), denial_reason),
        )
        return self._claim_dict(db.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone())

    def mark_settled(self, db: sqlite3.Connection, claim_id: str, expected_version: int) -> dict:
        cursor = db.execute(
            """UPDATE claims SET status = 'SETTLED', version = version + 1,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE claim_id = ? AND version = ? AND status = 'OPEN'""",
            (claim_id, expected_version),
        )
        if cursor.rowcount == 0:
            raise ValueError("STALE_VERSION_OR_NOT_OPEN")
        return self._claim_dict(db.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone())

    def mark_withdrawn(self, db: sqlite3.Connection, claim_id: str, expected_version: int) -> dict:
        cursor = db.execute(
            """UPDATE claims SET status = 'WITHDRAWN', due_fen = 0, version = version + 1,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE claim_id = ? AND version = ? AND status IN ('OPEN','SETTLED')""",
            (claim_id, expected_version),
        )
        if cursor.rowcount == 0:
            raise ValueError("STALE_VERSION_OR_NOT_OPEN")
        return self._claim_dict(db.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone())

    # ---------- 凭证 ----------

    def next_voucher_no(self, db: sqlite3.Connection, claim_id: str) -> str:
        row = db.execute("SELECT COUNT(*) AS n FROM vouchers WHERE claim_id = ?", (claim_id,)).fetchone()
        return f"V-{claim_id}-{row['n'] + 1:03d}"

    def insert_voucher(self, db: sqlite3.Connection, claim_id: str, voucher_no: str, spec) -> None:
        if not voucher_balanced(spec.entries):
            raise ValueError("VOUCHER_NOT_BALANCED")
        db.execute(
            "INSERT INTO vouchers(voucher_no, claim_id, voucher_type, linked_voucher_no, summary, amount_fen) VALUES (?, ?, ?, ?, ?, ?)",
            (voucher_no, claim_id, spec.voucher_type, spec.linked_voucher_no, spec.summary,
             sum(e.amount_fen for e in spec.entries if e.direction == "DR")),
        )
        for line_no, entry in enumerate(spec.entries, start=1):
            db.execute(
                "INSERT INTO voucher_entries(voucher_no, account, direction, amount_fen, line_no) VALUES (?, ?, ?, ?, ?)",
                (voucher_no, entry.account, entry.direction, entry.amount_fen, line_no),
            )

    def list_voucher_chain(self, claim_id: str) -> list[dict]:
        with self._lock, self.connect() as db:
            vouchers = db.execute(
                "SELECT * FROM vouchers WHERE claim_id = ? ORDER BY created_at, voucher_no", (claim_id,)
            ).fetchall()
            chain = []
            for voucher in vouchers:
                entries = db.execute(
                    "SELECT account, direction, amount_fen, line_no FROM voucher_entries WHERE voucher_no = ? ORDER BY line_no",
                    (voucher["voucher_no"],),
                ).fetchall()
                item = dict(voucher)
                item["entries"] = [dict(entry) for entry in entries]
                chain.append(item)
            return chain

    def list_revisions(self, claim_id: str) -> list[dict]:
        with self._lock, self.connect() as db:
            rows = db.execute(
                "SELECT revision_id, version_after, reason, evidence_json, delay_minutes, due_fen, evidence_complete, created_at FROM claim_revisions WHERE claim_id = ? ORDER BY revision_id",
                (claim_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["evidence"] = json.loads(item.pop("evidence_json"))
                item["evidence_complete"] = bool(item["evidence_complete"])
                result.append(item)
            return result

    def mark_batch_seen(self, db: sqlite3.Connection, batch_id: str, claim_id: str) -> None:
        db.execute("INSERT OR IGNORE INTO registration_batches(batch_id, claim_id, created) VALUES (?, ?, 0)",
                   (batch_id, claim_id))

    def batch_claims(self, batch_id: str) -> list[dict]:
        with self._lock, self.connect() as db:
            rows = db.execute(
                """SELECT c.* FROM claims c JOIN registration_batches b ON c.claim_id = b.claim_id
                   WHERE b.batch_id = ? ORDER BY c.created_at, c.claim_id""", (batch_id,),
            ).fetchall()
            return [self._claim_dict(row) for row in rows]

    def transaction(self):
        """返回已开启 IMMEDIATE 事务的连接；配合 with 使用，异常回滚。"""
        return _ImmediateTx(self)


class _ImmediateTx:
    def __init__(self, repo: ClaimRepository):
        self.repo = repo
        self.db: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.repo._lock.acquire()
        self.db = self.repo.connect()
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.db.execute("COMMIT")
            else:
                self.db.execute("ROLLBACK")
        finally:
            self.db.close()
            self.repo._lock.release()
