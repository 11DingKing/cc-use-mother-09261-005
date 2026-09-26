"""SQLite 状态仓储。"""
import sqlite3,json
class SQLiteStore:
 def __init__(self,path=":memory:"):
  self.db=sqlite3.connect(path); self.db.execute("CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL)"); self.db.commit()
 def save(self,value): self.db.execute("INSERT INTO snapshots(body) VALUES(?)",(json.dumps(value,ensure_ascii=False),)); self.db.commit()
 def latest(self):
  row=self.db.execute("SELECT body FROM snapshots ORDER BY id DESC LIMIT 1").fetchone(); return json.loads(row[0]) if row else []
