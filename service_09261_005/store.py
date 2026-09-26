"""SQLite 持久化仓储，只依赖标准库。

时间统一存为 UTC 的 ISO-8601 字符串（定长、带相同时区后缀），
因此日期范围可以直接用字符串比较，天然覆盖跨日期边界场景。
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  applicant   TEXT NOT NULL,
  textbook    TEXT NOT NULL DEFAULT '',
  grade       TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL,
  version     INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id     TEXT NOT NULL REFERENCES cases(id),
  kind        TEXT NOT NULL,
  title       TEXT NOT NULL,
  url         TEXT NOT NULL DEFAULT '',
  note        TEXT NOT NULL DEFAULT '',
  actor       TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id     TEXT NOT NULL REFERENCES cases(id),
  from_state  TEXT,
  to_state    TEXT NOT NULL,
  reason      TEXT NOT NULL,
  actor       TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issuances(
  case_id        TEXT PRIMARY KEY REFERENCES cases(id),
  certificate_no TEXT NOT NULL UNIQUE,
  issuer         TEXT NOT NULL,
  note           TEXT NOT NULL DEFAULT '',
  issued_at      TEXT NOT NULL,
  valid_until    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency(
  key        TEXT PRIMARY KEY,
  case_id    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.init()

    def init(self):
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    @contextmanager
    def tx(self):
        """串行化的写事务，失败自动回滚。"""
        with self._lock:
            try:
                yield self.db
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    # ---- cases -------------------------------------------------------
    def get_case(self, conn, case_id):
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        return dict(row) if row else None

    def insert_case(self, conn, case):
        conn.execute(
            "INSERT INTO cases(id,title,applicant,textbook,grade,state,version,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (case["id"], case["title"], case["applicant"], case["textbook"],
             case["grade"], case["state"], case["version"],
             case["created_at"], case["updated_at"]),
        )

    def update_state(self, conn, case_id, to_state, new_version, ts, expected_version=None):
        """带乐观版本号的状态更新，返回是否命中唯一一行。"""
        if expected_version is None:
            cur = conn.execute(
                "UPDATE cases SET state=?,version=?,updated_at=? WHERE id=?",
                (to_state, new_version, ts, case_id),
            )
        else:
            cur = conn.execute(
                "UPDATE cases SET state=?,version=?,updated_at=? "
                "WHERE id=? AND version=?",
                (to_state, new_version, ts, case_id, expected_version),
            )
        return cur.rowcount == 1

    def touch(self, conn, case_id, ts):
        conn.execute("UPDATE cases SET updated_at=? WHERE id=?", (ts, case_id))

    # ---- evidence ----------------------------------------------------
    def insert_evidence(self, conn, ev):
        cur = conn.execute(
            "INSERT INTO evidence(case_id,kind,title,url,note,actor,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (ev["case_id"], ev["kind"], ev["title"], ev["url"], ev["note"],
             ev["actor"], ev["created_at"]),
        )
        ev = dict(ev)
        ev["id"] = cur.lastrowid
        return ev

    def list_evidence(self, conn, case_id):
        rows = conn.execute(
            "SELECT * FROM evidence WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- events ------------------------------------------------------
    def insert_event(self, conn, event):
        cur = conn.execute(
            "INSERT INTO events(case_id,from_state,to_state,reason,actor,detail,"
            "occurred_at) VALUES(?,?,?,?,?,?,?)",
            (event["case_id"], event["from_state"], event["to_state"],
             event["reason"], event["actor"],
             json.dumps(event.get("detail", {}), ensure_ascii=False),
             event["occurred_at"]),
        )
        event = dict(event)
        event["seq"] = cur.lastrowid
        return event

    def list_events(self, conn, case_id):
        rows = conn.execute(
            "SELECT * FROM events WHERE case_id=? ORDER BY seq", (case_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out

    # ---- issuance ----------------------------------------------------
    def insert_issuance(self, conn, issuance):
        conn.execute(
            "INSERT INTO issuances(case_id,certificate_no,issuer,note,issued_at,"
            "valid_until) VALUES(?,?,?,?,?,?)",
            (issuance["case_id"], issuance["certificate_no"], issuance["issuer"],
             issuance["note"], issuance["issued_at"], issuance["valid_until"]),
        )

    def get_issuance(self, conn, case_id):
        row = conn.execute(
            "SELECT * FROM issuances WHERE case_id=?", (case_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- idempotency -------------------------------------------------
    def get_idempotency(self, conn, key):
        row = conn.execute(
            "SELECT case_id FROM idempotency WHERE key=?", (key,)
        ).fetchone()
        return row["case_id"] if row else None

    def insert_idempotency(self, conn, key, case_id, ts):
        conn.execute(
            "INSERT INTO idempotency(key,case_id,created_at) VALUES(?,?,?)",
            (key, case_id, ts),
        )

    # ---- search ------------------------------------------------------
    def search_cases(self, conn, state=None, applicant=None, textbook=None, q=None,
                     date_from=None, date_to=None, date_field="created_at",
                     limit=100):
        """按条件检索；date_from/date_to 为 YYYY-MM-DD，按 UTC 日历日闭区间过滤。"""
        if date_field not in ("created_at", "updated_at"):
            raise ValueError("date_field must be created_at or updated_at")
        where, args = [], []
        if state:
            where.append("state=?")
            args.append(state)
        if applicant:
            where.append("applicant=?")
            args.append(applicant)
        if textbook:
            where.append("textbook LIKE ?")
            args.append(f"%{textbook}%")
        if q:
            where.append("(title LIKE ? OR textbook LIKE ?)")
            args.extend([f"%{q}%", f"%{q}%"])
        if date_from:
            date.fromisoformat(date_from)  # 校验格式
            where.append(f"{date_field}>=?")
            args.append(f"{date_from}T00:00:00")
        if date_to:
            day_after = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
            where.append(f"{date_field}<?")
            args.append(f"{day_after}T00:00:00")
        sql = "SELECT * FROM cases"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, id LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
