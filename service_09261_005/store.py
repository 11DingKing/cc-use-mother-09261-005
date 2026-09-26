"""SQLite 状态仓储：仅依赖标准库 sqlite3。

表结构：
- cases        案例当前状态（版本号 + 驳回时间，用于补充证据判定）
- evidence     证据明细（created_at 晚于 rejected_at 才算“新证据”）
- events       状态变化流水（含变化原因 reason）
- idempotency  幂等键映射，保证重复提交不产生重复副作用
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    actor       TEXT NOT NULL,
    state       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    rejected_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL REFERENCES cases(id),
    actor      TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL REFERENCES cases(id),
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    version    INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    idem_key    TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    command     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    evidence_id INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence(case_id, id);
CREATE INDEX IF NOT EXISTS idx_events_case   ON events(case_id, id);
CREATE INDEX IF NOT EXISTS idx_cases_state   ON cases(state);
"""


class SQLiteStore:
    """案例聚合的持久化仓储。命令在 BEGIN IMMEDIATE 事务内串行执行。"""

    def __init__(self, path=":memory:"):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # -- 事务边界 ---------------------------------------------------------
    def begin(self):
        self.db.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.db.execute("COMMIT")

    def rollback(self):
        self.db.execute("ROLLBACK")

    def close(self):
        self.db.close()

    # -- cases ------------------------------------------------------------
    def get_case(self, case_id):
        row = self.db.execute(
            "SELECT * FROM cases WHERE id=?", (case_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_case(self, case):
        self.db.execute(
            "INSERT INTO cases(id,title,actor,state,version,rejected_at,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (case["id"], case["title"], case["actor"], case["state"],
             case["version"], case.get("rejected_at"),
             case["created_at"], case["updated_at"]),
        )

    def update_case(self, case):
        self.db.execute(
            "UPDATE cases SET title=?,actor=?,state=?,version=?,"
            "rejected_at=?,created_at=?,updated_at=? WHERE id=?",
            (case["title"], case["actor"], case["state"], case["version"],
             case.get("rejected_at"), case["created_at"],
             case["updated_at"], case["id"]),
        )

    # -- evidence ---------------------------------------------------------
    def insert_evidence(self, case_id, actor, content, created_at):
        cur = self.db.execute(
            "INSERT INTO evidence(case_id,actor,content,created_at) "
            "VALUES(?,?,?,?)",
            (case_id, actor, content, created_at),
        )
        return cur.lastrowid

    def list_evidence(self, case_id):
        rows = self.db.execute(
            "SELECT id,actor,content,created_at FROM evidence "
            "WHERE case_id=? ORDER BY id",
            (case_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_evidence(self, case_id, since=None):
        if since is None:
            row = self.db.execute(
                "SELECT COUNT(*) FROM evidence WHERE case_id=?", (case_id,)
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT COUNT(*) FROM evidence WHERE case_id=? AND created_at>?",
                (case_id, since),
            ).fetchone()
        return row[0]

    # -- events -----------------------------------------------------------
    def insert_event(self, case_id, from_state, to_state, reason, actor,
                     version, created_at):
        self.db.execute(
            "INSERT INTO events(case_id,from_state,to_state,reason,actor,"
            "version,created_at) VALUES(?,?,?,?,?,?,?)",
            (case_id, from_state, to_state, reason, actor, version,
             created_at),
        )

    def list_events(self, case_id):
        rows = self.db.execute(
            "SELECT id,from_state,to_state,reason,actor,version,created_at "
            "FROM events WHERE case_id=? ORDER BY id",
            (case_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- idempotency ------------------------------------------------------
    def get_idempotency(self, key):
        row = self.db.execute(
            "SELECT * FROM idempotency WHERE idem_key=?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def insert_idempotency(self, key, case_id, command, reason, version,
                           created_at, evidence_id=None):
        self.db.execute(
            "INSERT INTO idempotency(idem_key,case_id,command,reason,"
            "version,evidence_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (key, case_id, command, reason, version, evidence_id,
             created_at),
        )

    # -- retrieval --------------------------------------------------------
    def search(self, state=None, actor=None, q=None, date=None,
               updated_date=None, date_from=None, date_to=None):
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM evidence e "
               "WHERE e.case_id=c.id) AS evidence_count "
               "FROM cases c WHERE 1=1")
        args = []
        if state:
            sql += " AND c.state=?"
            args.append(state)
        if actor:
            sql += " AND c.actor=?"
            args.append(actor)
        if q:
            sql += (" AND (c.title LIKE ? OR EXISTS(SELECT 1 FROM evidence e "
                    "WHERE e.case_id=c.id AND e.content LIKE ?))")
            args.extend(("%" + q + "%",) * 2)
        if date:
            sql += " AND date(c.created_at)=?"
            args.append(date)
        if updated_date:
            sql += " AND date(c.updated_at)=?"
            args.append(updated_date)
        if date_from:
            sql += " AND date(c.created_at)>=?"
            args.append(date_from)
        if date_to:
            sql += " AND date(c.created_at)<=?"
            args.append(date_to)
        sql += " ORDER BY c.created_at, c.id"
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]
