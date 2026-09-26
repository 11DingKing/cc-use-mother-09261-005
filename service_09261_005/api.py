"""JSON API 适配器。

以纯函数 dispatch(flow, method, path, body, query) 为边界，
既可用 unittest 直接驱动，也由 app.py 的标准库 HTTP 服务挂载。

所有写操作的成功响应都包含 state_change，说明状态变化原因；
业务错误统一返回 {"error": code, "message": 原因}。
"""
import json

from .workflow import WorkflowError

STATUS_FOR_CODE = {
    "case_not_found": 404,
    "invalid_id": 400,
    "invalid_applicant": 400,
    "invalid_title": 400,
    "invalid_evidence_kind": 400,
    "invalid_evidence_title": 400,
    "reason_required": 400,
    "certificate_no_required": 400,
    "evidence_incomplete": 422,
    "invalid_transition": 409,
    "evidence_not_allowed": 409,
    "duplicate_case": 409,
    "already_issued": 409,
    "certificate_no_taken": 409,
    "conflict": 409,
}


def _err(exc):
    return STATUS_FOR_CODE.get(exc.code, 400), {
        "error": exc.code, "message": exc.message}


def _require(body, *fields):
    for f in fields:
        if body.get(f) in (None, ""):
            raise WorkflowError("invalid_request", f"缺少必填字段：{f}")


def dispatch(flow, method, path, body=None, query=None):
    """返回 (http_status, response_dict)。path 不含查询串。"""
    body = body or {}
    query = query or {}
    try:
        return _route(flow, method, path, body, query)
    except WorkflowError as exc:
        return _err(exc)
    except (KeyError, TypeError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _route(flow, method, path, body, query):
    parts = [p for p in path.strip("/").split("/") if p != ""]

    if method == "POST" and parts == ["cases"]:
        _require(body, "id", "applicant", "title")
        view, created = flow.create_draft(
            body["id"], body["applicant"], body["title"],
            textbook=body.get("textbook", ""), grade=body.get("grade", ""),
            idempotency_key=body.get("idempotency_key"))
        return (201 if created else 200), view

    if method == "GET" and parts == ["cases"]:
        filters = {}
        for k in ("state", "applicant", "textbook", "q", "date_from",
                  "date_to", "date_field"):
            if query.get(k):
                filters[k] = query[k]
        if query.get("limit"):
            filters["limit"] = int(query["limit"])
        return 200, {"items": flow.search(**filters)}

    if len(parts) == 3 and parts[0] == "cases" and parts[2] == "evidence" \
            and method == "POST":
        _require(body, "kind", "title", "actor")
        return 201, flow.add_evidence(
            parts[1], body["kind"], body["title"], body["actor"],
            url=body.get("url", ""), note=body.get("note", ""))

    if len(parts) == 3 and parts[0] == "cases" and method == "POST":
        case_id, action = parts[1], parts[2]
        if action == "submit":
            _require(body, "actor")
            return 200, flow.submit_for_review(
                case_id, body["actor"], note=body.get("note", ""))[0]
        if action == "reviews":
            _require(body, "actor", "approved", "reason")
            return 200, flow.review(
                case_id, body["actor"], bool(body["approved"]),
                body["reason"], note=body.get("note", ""))[0]
        if action == "issuance":
            _require(body, "issuer", "certificate_no")
            return 201, flow.issue(
                case_id, body["issuer"], body["certificate_no"],
                note=body.get("note", ""),
                valid_days=body.get("valid_days"))
        if action == "archive":
            _require(body, "actor", "reason")
            return 200, flow.archive(
                case_id, body["actor"], body["reason"],
                note=body.get("note", ""))[0]
        if action == "cancel":
            _require(body, "actor", "reason")
            return 200, flow.cancel(
                case_id, body["actor"], body["reason"],
                note=body.get("note", ""))[0]
        return 404, {"error": "not_found", "message": f"未知动作：{action}"}

    if method == "GET" and len(parts) == 2 and parts[0] == "cases":
        return 200, flow.get(parts[1])

    return 404, {"error": "not_found", "message": "未匹配到资源或方法"}
