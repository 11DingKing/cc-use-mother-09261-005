"""JSON API 适配器（零第三方依赖，可由任意 HTTP 外壳承载）。

路由：
  POST /cases                       创建草稿
  POST /cases/{id}/evidence         补充证据
  POST /cases/{id}/submit           提交/重新提交审核
  POST /cases/{id}/review           审核 {decision: approve|reject}
  POST /cases/{id}/issue            签发
  GET  /cases/{id}                  案例详情（含证据与变化流水）
  GET  /cases/{id}/history          状态变化历史
  GET  /cases                       按条件检索
       ?state=&actor=&q=&date=&updated_date=&date_from=&date_to=

所有命令响应都包含 reason（本次变化原因）与 state_reason（当前状态原因）。
"""

import json
from urllib.parse import urlparse, parse_qs

from .workflow import WorkflowError


def _err(code, message, status):
    return status, {"error": {"code": code, "message": message}}


def dispatch(flow, method, path, body=None, query=None):
    """返回 (status, payload)。payload 为可 JSON 序列化的 dict/list。"""
    parsed = urlparse(path)
    clean_path = parsed.path.rstrip("/") or "/"
    params = {}
    if query is not None:
        params.update({k: v[-1] for k, v in parse_qs(query).items()})
    elif parsed.query:
        params.update({k: v[-1] for k, v in
                       parse_qs(parsed.query).items()})
    body = body or {}

    try:
        return _route(flow, method, clean_path, body, params)
    except WorkflowError as e:
        return _err(e.code, str(e), e.status)
    except KeyError as e:
        return _err("invalid_request", "缺少必填字段: %s" % e.args[0], 400)


def _route(flow, method, path, body, params):
    if method == "POST" and path == "/cases":
        result = flow.create(
            body["id"], body["actor"],
            title=body.get("title", ""),
            idempotency_key=body.get("idempotency_key"))
        return 201, _cmd_body(result)

    segments = [s for s in path.split("/") if s]

    if (method == "GET" and len(segments) == 3
            and segments[0] == "cases" and segments[2] == "history"):
        return 200, {"events": flow.history(segments[1])}

    if len(segments) == 3 and segments[0] == "cases":
        case_id, action = segments[1], segments[2]
        if method != "POST":
            return _err("method_not_allowed", "仅支持 POST", 405)
        if action == "evidence":
            result = flow.add_evidence(
                case_id, body["actor"], body["content"],
                idempotency_key=body.get("idempotency_key"))
            return 200, _cmd_body(result)
        if action == "submit":
            result = flow.submit(
                case_id, body["actor"], reason=body.get("reason"),
                idempotency_key=body.get("idempotency_key"))
            return 200, _cmd_body(result)
        if action == "review":
            result = flow.review(
                case_id, body["actor"], body["decision"],
                reason=body.get("reason"),
                idempotency_key=body.get("idempotency_key"))
            return 200, _cmd_body(result)
        if action == "issue":
            result = flow.issue(
                case_id, body["actor"], reason=body.get("reason"),
                idempotency_key=body.get("idempotency_key"))
            return 200, _cmd_body(result)
        return _err("unknown_action", "未知操作: %s" % action, 404)

    if method == "GET" and len(segments) == 2 and segments[0] == "cases":
        return 200, flow.get(segments[1])

    if (method == "GET" and len(segments) == 3
            and segments[0] == "cases" and segments[2] == "history"):
        return 200, {"events": flow.history(segments[1])}

    if method == "GET" and path == "/cases":
        filters = {k: params[k] for k in
                   ("state", "actor", "q", "date", "updated_date",
                    "date_from", "date_to") if k in params}
        return 200, {"items": flow.search(**filters)}

    return _err("not_found", "路由不存在: %s %s" % (method, path), 404)


def _cmd_body(result):
    return {
        "id": result.id,
        "state": result.state,
        "version": result.version,
        "reason": result.reason,
        "state_reason": result.state_reason,
        "actor": result.actor,
        "changed": result.changed,
        "timestamp": result.timestamp,
        **({"evidence_id": result.evidence_id}
           if result.evidence_id is not None else {}),
    }


def dumps(payload):
    return json.dumps(payload, ensure_ascii=False)
