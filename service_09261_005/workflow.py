"""教材试用观察期业务工作流。

状态：draft -> reviewing -> approved -> issued -> archived
      reviewing -> rejected -> draft（补证据后重新送审）
另有 draft -> cancelled。

每次状态变化都会：
1. 校验来源状态与业务门槛；
2. 写入 cases.version（乐观锁）与 events 审计事件；
3. 在返回中带回 state_change，说明“从什么状态、因为什么原因、由谁、在何时”变化。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .store import SQLiteStore

DRAFT = "draft"
REVIEWING = "reviewing"
APPROVED = "approved"
ISSUED = "issued"
REJECTED = "rejected"
ARCHIVED = "archived"
CANCELLED = "cancelled"

STATES = {DRAFT, REVIEWING, APPROVED, ISSUED, REJECTED, ARCHIVED, CANCELLED}

# 送审所需的证据类别：课标契合度材料 + 试用反馈，缺一不可。
REQUIRED_EVIDENCE_KINDS = ("curriculum_alignment", "trial_feedback")
EVIDENCE_KINDS = set(REQUIRED_EVIDENCE_KINDS) | {"sample_chapter", "other"}

# 签发证书有效期（天）。
ISSUANCE_VALID_DAYS = 180

TRANSITIONS = {
    DRAFT: {REVIEWING, CANCELLED},
    REJECTED: {REVIEWING, CANCELLED},
    REVIEWING: {APPROVED, REJECTED},
    APPROVED: {ISSUED, ARCHIVED},
    ISSUED: {ARCHIVED},
}


class WorkflowError(Exception):
    """业务规则错误，message 即面向调用方的状态变化原因。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt):
    """定长 ISO-8601（UTC, 秒级），保证 SQLite 字符串比较即时间比较。"""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Clock:
    """可注入时钟，测试跨日期边界时用固定/推进时钟替换。"""

    now_fn: object = utcnow

    def now(self):
        return self.now_fn()


class Workflow:
    def __init__(self, store=None, clock=None):
        self.store = store or SQLiteStore(":memory:")
        self.clock = clock or Clock()

    # ------------------------------------------------------------------
    def _ts(self):
        return _iso(self.clock.now())

    def _event(self, conn, case_id, frm, to, reason, actor, detail=None):
        return self.store.insert_event(conn, {
            "case_id": case_id, "from_state": frm, "to_state": to,
            "reason": reason, "actor": actor, "detail": detail or {},
            "occurred_at": self._ts(),
        })

    @staticmethod
    def _state_change(event):
        return {
            "from_state": event["from_state"],
            "to_state": event["to_state"],
            "reason": event["reason"],
            "actor": event["actor"],
            "detail": event["detail"],
            "occurred_at": event["occurred_at"],
        }

    def _view(self, conn, case, state_change=None):
        issuance = self.store.get_issuance(conn, case["id"])
        body = {
            "case": case,
            "evidence": self.store.list_evidence(conn, case["id"]),
            "issuance": issuance,
            "history": [
                {k: e[k] for k in
                 ("seq", "from_state", "to_state", "reason", "actor", "detail", "occurred_at")}
                for e in self.store.list_events(conn, case["id"])
            ],
        }
        if state_change is not None:
            body["state_change"] = state_change
        return body

    # ---- 创建草稿 ----------------------------------------------------
    def create_draft(self, case_id, applicant, title, textbook="", grade="",
                     idempotency_key=None):
        if not case_id or not str(case_id).strip():
            raise WorkflowError("invalid_id", "案例 ID 不能为空")
        if not applicant or not str(applicant).strip():
            raise WorkflowError("invalid_applicant", "申请人不能为空")
        if not title or not str(title).strip():
            raise WorkflowError("invalid_title", "教材名称/标题不能为空")
        ts = self._ts()
        with self.store.tx() as conn:
            if idempotency_key:
                existing = self.store.get_idempotency(conn, idempotency_key)
                if existing:
                    case = self.store.get_case(conn, existing)
                    return self._view(conn, case), False
            case = self.store.get_case(conn, case_id)
            if case is not None:
                raise WorkflowError("duplicate_case", f"案例 {case_id} 已存在")
            case = {
                "id": case_id, "title": title, "applicant": applicant,
                "textbook": textbook, "grade": grade, "state": DRAFT,
                "version": 1, "created_at": ts, "updated_at": ts,
            }
            self.store.insert_case(conn, case)
            if idempotency_key:
                self.store.insert_idempotency(conn, idempotency_key, case_id, ts)
            event = self._event(conn, case_id, None, DRAFT,
                                "创建教材试用观察期草稿", applicant)
            return self._view(conn, case, self._state_change(event)), True

    # ---- 补充证据 ----------------------------------------------------
    def add_evidence(self, case_id, kind, title, actor, url="", note=""):
        if kind not in EVIDENCE_KINDS:
            raise WorkflowError(
                "invalid_evidence_kind",
                f"证据类别必须是 {sorted(EVIDENCE_KINDS)} 之一")
        if not title or not str(title).strip():
            raise WorkflowError("invalid_evidence_title", "证据标题不能为空")
        ts = self._ts()
        with self.store.tx() as conn:
            case = self.store.get_case(conn, case_id)
            if case is None:
                raise WorkflowError("case_not_found", f"案例 {case_id} 不存在")
            if case["state"] not in (DRAFT, REJECTED):
                raise WorkflowError(
                    "evidence_not_allowed",
                    f"当前状态 {case['state']} 不允许补充证据，"
                    "仅草稿(draft)或驳回(rejected)状态可补充")
            ev = self.store.insert_evidence(conn, {
                "case_id": case_id, "kind": kind, "title": title,
                "url": url, "note": note, "actor": actor, "created_at": ts,
            })
            self.store.touch(conn, case_id, ts)
            event = self._event(
                conn, case_id, case["state"], case["state"],
                f"补充证据：{kind}", actor, detail={"evidence_id": ev["id"]})
            view = self._view(conn, self.store.get_case(conn, case_id),
                              self._state_change(event))
            view["evidence_added"] = {k: ev[k] for k in
                                      ("id", "kind", "title", "url", "note", "actor", "created_at")}
            return view

    def _missing_evidence(self, conn, case_id):
        have = {e["kind"] for e in self.store.list_evidence(conn, case_id)}
        return [k for k in REQUIRED_EVIDENCE_KINDS if k not in have]

    # ---- 送审 / 审核 / 签发 / 归档 ----------------------------------
    def submit_for_review(self, case_id, actor, note=""):
        return self._transition(
            case_id, REVIEWING, actor,
            reason="提交审核", note=note,
            guard=self._guard_submit)

    def _guard_submit(self, conn, case):
        missing = self._missing_evidence(conn, case["id"])
        if missing:
            kinds = "、".join(missing)
            raise WorkflowError(
                "evidence_incomplete",
                f"证据不完整，缺少必需证据类别：{kinds}，无法提交审核")

    def review(self, case_id, actor, approved, reason, note=""):
        if not reason or not str(reason).strip():
            raise WorkflowError("reason_required", "审核必须给出结论原因")
        to = APPROVED if approved else REJECTED
        label = "审核通过" if approved else "审核驳回"
        return self._transition(
            case_id, to, actor, reason=f"{label}：{reason}", note=note)

    def issue(self, case_id, issuer, certificate_no, note="", valid_days=None):
        if not certificate_no or not str(certificate_no).strip():
            raise WorkflowError("certificate_no_required", "签发必须提供证书编号")
        days = ISSUANCE_VALID_DAYS if valid_days is None else int(valid_days)
        ts = self._ts()
        with self.store.tx() as conn:
            case = self.store.get_case(conn, case_id)
            if case is None:
                raise WorkflowError("case_not_found", f"案例 {case_id} 不存在")
            if ISSUED not in TRANSITIONS.get(case["state"], set()):
                raise WorkflowError(
                    "invalid_transition",
                    f"不允许从 {case['state']} 变更为 {ISSUED}，仅审核通过可签发")
            if self.store.get_issuance(conn, case_id) is not None:
                raise WorkflowError("already_issued", "该案例已签发，不能重复签发")
            if not self.store.update_state(
                    conn, case_id, ISSUED, case["version"] + 1, ts,
                    expected_version=case["version"]):
                raise WorkflowError("conflict", "案例版本已变化，请重试")
            issued_at = self.clock.now()
            issuance = {
                "case_id": case_id,
                "certificate_no": certificate_no,
                "issuer": issuer,
                "note": note,
                "issued_at": _iso(issued_at),
                "valid_until": _iso(issued_at + timedelta(days=days)),
            }
            try:
                self.store.insert_issuance(conn, issuance)
            except Exception:
                # 与状态变更同一事务：证书编号冲突时整笔回滚
                raise WorkflowError(
                    "certificate_no_taken",
                    f"证书编号 {certificate_no} 已被使用")
            event = self._event(
                conn, case_id, case["state"], ISSUED,
                f"签发试用证书 {certificate_no}，有效期至 {issuance['valid_until']}",
                issuer, detail={"certificate_no": certificate_no, "note": note} if note else
                {"certificate_no": certificate_no})
            return self._view(conn, self.store.get_case(conn, case_id),
                              self._state_change(event))

    def archive(self, case_id, actor, reason, note=""):
        if not reason or not str(reason).strip():
            raise WorkflowError("reason_required", "归档必须说明原因")
        return self._transition(
            case_id, ARCHIVED, actor, reason=f"归档：{reason}", note=note)

    def cancel(self, case_id, actor, reason, note=""):
        if not reason or not str(reason).strip():
            raise WorkflowError("reason_required", "撤销必须说明原因")
        return self._transition(
            case_id, CANCELLED, actor, reason=f"撤销草稿：{reason}", note=note)

    # ---- 内部：通用状态迁移 -----------------------------------------
    def _transition(self, case_id, to_state, actor, reason, note="", guard=None):
        with self.store.tx() as conn:
            case = self.store.get_case(conn, case_id)
            if case is None:
                raise WorkflowError("case_not_found", f"案例 {case_id} 不存在")
            frm = case["state"]
            if to_state not in TRANSITIONS.get(frm, set()):
                raise WorkflowError(
                    "invalid_transition",
                    f"不允许从 {frm} 变更为 {to_state}")
            if guard is not None:
                guard(conn, case)
            ts = self._ts()
            if not self.store.update_state(
                    conn, case_id, to_state, case["version"] + 1, ts,
                    expected_version=case["version"]):
                raise WorkflowError("conflict", "案例版本已变化，请重试")
            detail = {"note": note} if note else {}
            event = self._event(conn, case_id, frm, to_state, reason, actor, detail)
            return self._view(conn, self.store.get_case(conn, case_id),
                              self._state_change(event)), event

    # ---- 查询 --------------------------------------------------------
    def get(self, case_id):
        with self.store.tx() as conn:
            case = self.store.get_case(conn, case_id)
            if case is None:
                raise WorkflowError("case_not_found", f"案例 {case_id} 不存在")
            return self._view(conn, case)

    def search(self, **filters):
        with self.store.tx() as conn:
            rows = self.store.search_cases(conn, **filters)
            return [self._view(conn, c) for c in rows]
