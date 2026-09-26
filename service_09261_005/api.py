"""JSON API 适配器。"""
def dispatch(flow,method,path,body=None):
 body=body or {}
 if method=="POST" and path=="/cases": return 201,flow.create(body["id"],body["actor"],body.get("idempotency_key")).__dict__
 if method=="POST" and path.endswith("/move"): return 200,flow.move(path.split("/")[2],body["state"],body["actor"]).__dict__
 if method=="GET" and path=="/cases": return 200,flow.snapshot()
 return 404,{"error":"not_found"}
