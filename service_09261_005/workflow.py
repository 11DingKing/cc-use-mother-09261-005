"""版本化业务工作流：教材试用观察期案例状态机。

状态：draft（草稿）-> reviewing（待审）-> approved（通过）-> issued（已签发）
reviewing 可 reject 为 rejected；rejected 补充新证据后可重新提交回 reviewing。

每次状态变化都写入带 reason（变化原因）的事件，命令响应始终带回
state_reason；证据追加不改变状态，但同样返回当前状态的原因。
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from .store import SQLiteStore

# 允许的状态迁移
TRANSITIONS = {
    "draft": {"submit": "reviewing"},
    "reviewing": {"approve": "approved", "reject": "rejected"},
    "rejected": {"resubmit": "reviewing"},
    "approved": {"issue": "issued"},
    "issued": {},
}

# 各状态对外解释的“变化原因”
STATE_REASONS = {
    "draft": "草稿创建，尚未提交审核",
    "reviewing": "已提交审核，等待审核结论",
    "approved": "审核通过，等待签发",
    "rejected": "审核驳回，需补充证据后重新提交",
    "issued": "已签发，教材进入试用观察期",
}

TERMINAL_STATES = ("issued", "cancelled")


class WorkflowError(Exception):
    """业务规则违例，HTTP 层映射为 4xx。"""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CommandResult:
    id: str
    state: str
    version: int
    reason: str          # 本次动作给出的变化原因
    state_reason: str    # 当前状态的标准解释
    actor: str
    changed: bool
    timestamp: str
    evidence_id: int = None


class Workflow:
    def __init__(self, store=None, clock=None):
        self.store = store or SQLiteStore(":memory:")
        self.clock = clock or now_iso

    # -- 内部工具 ---------------------------------------------------------
    def _require_case(self, case_id):
        case = self.store.get_case(case_id)
        if case is None:
            raise WorkflowError("case_not_found",
                                "案例不存在: %s" % case_id, status=404)
        return case

    def _idempotent(self, key, case_id, command):
        """命中幂等键时返回历史结果（不重复执行副作用）。"""
        rec = self.store.get_idempotency(key)
        if rec is None:
            return None
        if rec["case_id"] != case_id or rec["command"] != command:
            raise WorkflowError(
                "idempotency_conflict",
                "幂等键已用于其他命令", status=409)
        case = self._require_case(case_id)
        return CommandResult(
            id=case_id, state=case["state"], version=case["version"],
            reason=rec["reason"], state_reason=STATE_REASONS[case["state"]],
            actor=case["actor"], changed=False,
            timestamp=rec["created_at"],
            evidence_id=rec["evidence_id"])

    def _finish(self, case_id, key, command, reason, changed, evidence_id=None):
        case = self.store.get_case(case_id)
        if key:
            self.store.insert_idempotency(
                key, case_id, command, reason, case["version"],
                case["updated_at"], evidence_id=evidence_id)
        self.store.commit()
        return CommandResult(
            id=case_id, state=case["state"], version=case["version"],
            reason=reason, state_reason=STATE_REASONS[case["state"]],
            actor=case["actor"], changed=changed,
            timestamp=case["updated_at"], evidence_id=evidence_id)

    # -- 命令 -------------------------------------------------------------
    def create(self, case_id, actor, title="", idempotency_key=None):
        """1. 创建草稿。"""
        if not case_id or not actor:
            raise WorkflowError("invalid_request", "id 与 actor 必填")
        self.store.begin()
        try:
            if idempotency_key:
                hit = self._idempotent(idempotency_key, case_id, "create")
                if hit:
                    self.store.commit()
                    return hit
            existing = self.store.get_case(case_id)
            if existing is not None:
                raise WorkflowError("duplicate_case",
                                    "案例已存在: %s" % case_id, status=409)
            ts = self.clock()
            case = {"id": case_id, "title": title or case_id,
                    "actor": actor, "state": "draft", "version": 1,
                    "rejected_at": None, "created_at": ts,
                    "updated_at": ts}
            self.store.insert_case(case)
            self.store.insert_event(
                case_id, None, "draft",
                "创建教材试用观察期案例草稿", actor, 1, ts)
            return self._finish(
                case_id, idempotency_key, "create",
                "草稿已创建", changed=True)
        except Exception:
            self.store.rollback()
            raise

    def add_evidence(self, case_id, actor, content, idempotency_key=None):
        """2. 补充证据。被驳回后，只有晚于驳回时刻的证据才算新证据。"""
        if not content or not str(content).strip():
            raise WorkflowError("invalid_request", "证据内容不能为空")
        self.store.begin()
        try:
            if idempotency_key:
                hit = self._idempotent(idempotency_key, case_id,
                                       "add_evidence")
                if hit:
                    self.store.commit()
                    return hit
            case = self._require_case(case_id)
            ts = self.clock()
            evidence_id = self.store.insert_evidence(
                case_id, actor, content.strip(), ts)
            new_after_reject = (
                case["state"] == "rejected"
                and (case["rejected_at"] is None or ts > case["rejected_at"]))
            count = self.store.count_evidence(
                case_id, since=case["rejected_at"]) if case["rejected_at"] \
                else self.store.count_evidence(case_id)
            if new_after_reject:
                reason = ("已补充新证据（驳回后第 %d 条），可重新提交审核"
                          % count)
            elif case["state"] == "rejected":
                reason = "证据已记录，但时间不晚于驳回，需补充新证据"
            else:
                reason = "证据已补充，共 %d 条" % count
            case["updated_at"] = ts
            self.store.update_case(case)
            return self._finish(
                case_id, idempotency_key, "add_evidence", reason,
                changed=False, evidence_id=evidence_id)
        except Exception:
            self.store.rollback()
            raise

    def submit(self, case_id, actor, reason=None, idempotency_key=None):
        """提交审核；驳回后重新提交要求至少一条新证据。"""
        self.store.begin()
        try:
            if idempotency_key:
                hit = self._idempotent(idempotency_key, case_id, "submit")
                if hit:
                    self.store.commit()
                    return hit
            case = self._require_case(case_id)
            action = "resubmit" if case["state"] == "rejected" else "submit"
            if action == "resubmit":
                fresh = self.store.count_evidence(
                    case_id, since=case["rejected_at"])
                if fresh < 1:
                    raise WorkflowError(
                        "evidence_required",
                        "驳回后必须补充至少一条新证据才能重新提交",
                        status=422)
            from_state = case["state"]
            r = reason or ("补充证据后重新提交审核"
                           if action == "resubmit" else "提交审核")
            case = self._apply_transition(
                case, action, actor, r, from_state)
            return self._finish(case_id, idempotency_key, "submit", r,
                                changed=True)
        except Exception:
            self.store.rollback()
            raise

    def review(self, case_id, actor, decision, reason=None,
               idempotency_key=None):
        """3. 审核：approve / reject。"""
        if decision not in ("approve", "reject"):
            raise WorkflowError("invalid_request",
                                "decision 必须为 approve 或 reject")
        if decision == "reject" and not reason:
            raise WorkflowError("invalid_request", "驳回必须填写原因")
        command = "review:" + decision
        self.store.begin()
        try:
            if idempotency_key:
                hit = self._idempotent(idempotency_key, case_id, command)
                if hit:
                    self.store.commit()
                    return hit
            case = self._require_case(case_id)
            from_state = case["state"]
            r = reason or ("审核通过" if decision == "approve"
                           else "审核驳回")
            case = self._apply_transition(case, decision, actor, r,
                                          from_state)
            return self._finish(case_id, idempotency_key, command, r,
                                changed=True)
        except Exception:
            self.store.rollback()
            raise

    def issue(self, case_id, actor, reason=None, idempotency_key=None):
        """4. 签发。"""
        self.store.begin()
        try:
            if idempotency_key:
                hit = self._idempotent(idempotency_key, case_id, "issue")
                if hit:
                    self.store.commit()
                    return hit
            case = self._require_case(case_id)
            from_state = case["state"]
            r = reason or "审核通过后签发，教材进入试用观察期"
            case = self._apply_transition(case, "issue", actor, r,
                                          from_state)
            return self._finish(case_id, idempotency_key, "issue", r,
                                changed=True)
        except Exception:
            self.store.rollback()
            raise

    def _apply_transition(self, case, action, actor, reason, from_state):
        allowed = TRANSITIONS.get(case["state"], {})
        if action not in allowed:
            raise WorkflowError(
                "invalid_transition",
                "状态 %s 不允许执行 %s" % (case["state"], action),
                status=409)
        new_state = allowed[action]
        ts = self.clock()
        case["state"] = new_state
        case["version"] += 1
        case["updated_at"] = ts
        if new_state == "rejected":
            case["rejected_at"] = ts
        self.store.update_case(case)
        self.store.insert_event(
            case["id"], from_state, new_state, reason, actor,
            case["version"], ts)
        return case

    # -- 查询 -------------------------------------------------------------
    def get(self, case_id):
        case = self._require_case(case_id)
        return self._detail(case)

    def _detail(self, case):
        d = dict(case)
        d["state_reason"] = STATE_REASONS[case["state"]]
        d["evidence"] = self.store.list_evidence(case["id"])
        d["events"] = self.store.list_events(case["id"])
        return d

    def search(self, **filters):
        """5. 按条件检索：state / actor / q / 日期边界。"""
        rows = self.store.search(**filters)
        for r in rows:
            r["state_reason"] = STATE_REASONS[r["state"]]
        return rows

    def history(self, case_id):
        self._require_case(case_id)
        return self.store.list_events(case_id)
