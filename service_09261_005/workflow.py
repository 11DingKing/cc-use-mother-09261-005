"""版本化业务工作流。"""
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
@dataclass(frozen=True)
class Case:
 id:str; actor:str; state:str; version:int=1
 def move(self,state,actor):
  allowed={"draft":{"reviewing","cancelled"},"reviewing":{"approved","rejected"},"rejected":{"draft"},"approved":{"archived"}}
  if state not in allowed.get(self.state,set()): raise ValueError("invalid transition")
  return Case(self.id,actor,state,self.version+1)
class Workflow:
 def __init__(self): self.rows={}; self.keys={}
 def create(self,id,actor,key=None):
  if key in self.keys: return self.rows[self.keys[key]]
  if id in self.rows: raise ValueError("duplicate")
  row=Case(id,actor,"draft"); self.rows[id]=row
  if key: self.keys[key]=id
  return row
 def move(self,id,state,actor): self.rows[id]=self.rows[id].move(state,actor); return self.rows[id]
 def snapshot(self): return [asdict(self.rows[k]) for k in sorted(self.rows)]
