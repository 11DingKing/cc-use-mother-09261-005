import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from service_09261_005.api import dispatch
from service_09261_005.store import SQLiteStore
from service_09261_005.workflow import (
    DRAFT, REVIEWING, APPROVED, ISSUED, REJECTED, ARCHIVED, CANCELLED,
    Workflow, WorkflowError,
)


class MutableClock:
    """可推进的固定时钟，用于构造跨午夜场景。"""

    def __init__(self, dt):
        self.dt = dt

    def now(self):
        return self.dt

    def advance(self, seconds=0, **kw):
        self.dt += timedelta(seconds=seconds, **kw)
        return self.dt


def make_flow(clock=None, path=":memory:"):
    return Workflow(SQLiteStore(path), clock=clock)


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.flow = make_flow()

    def _to_issued(self):
        view, _ = self.flow.create_draft("c1", "张老师", "数学七年级上册",
                                        textbook="数学", idempotency_key="k1")
        self.assertEqual(view["case"]["state"], DRAFT)
        self.flow.add_evidence("c1", "curriculum_alignment", "课标对照表", "张老师")
        self.flow.add_evidence("c1", "trial_feedback", "试用班级反馈记录", "张老师")
        view, _ = self.flow.submit_for_review("c1", "张老师")
        self.assertEqual(view["case"]["state"], REVIEWING)
        view, _ = self.flow.review("c1", "李审核", True, "材料齐全，试用效果良好")
        self.assertEqual(view["case"]["state"], APPROVED)
        view = self.flow.issue("c1", "王签发", "CERT-2026-0001")
        return view

    def test_full_loop_and_state_change_reasons(self):
        view = self._to_issued()
        case = view["case"]
        self.assertEqual(case["state"], ISSUED)
        self.assertEqual(case["version"], 4)  # draft/reviewing/approved/issued
        chg = view["state_change"]
        self.assertEqual((chg["from_state"], chg["to_state"]), (APPROVED, ISSUED))
        self.assertIn("CERT-2026-0001", chg["reason"])
        self.assertEqual(chg["actor"], "王签发")
        self.assertTrue(chg["occurred_at"])
        self.assertEqual(view["issuance"]["certificate_no"], "CERT-2026-0001")
        self.assertEqual(len(view["evidence"]), 2)
        # history 完整记录每一次状态变化及其原因
        reasons = [e["reason"] for e in view["history"]]
        self.assertTrue(any("创建" in r for r in reasons))
        self.assertTrue(any("提交审核" in r for r in reasons))
        self.assertTrue(any("审核通过" in r for r in reasons))
        self.assertTrue(any("签发" in r for r in reasons))

    def test_archive_after_issuance(self):
        self._to_issued()
        view, _ = self.flow.archive("c1", "教务员", "试用周期结束")
        self.assertEqual(view["case"]["state"], ARCHIVED)
        self.assertIn("归档", view["state_change"]["reason"])

    def test_idempotent_create(self):
        v1, created1 = self.flow.create_draft("c9", "a", "t", idempotency_key="dup")
        v2, created2 = self.flow.create_draft("c9", "a", "t", idempotency_key="dup")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(v1["case"]["id"], v2["case"]["id"])

    def test_duplicate_id_without_key_rejected(self):
        self.flow.create_draft("c9", "a", "t")
        with self.assertRaises(WorkflowError) as cm:
            self.flow.create_draft("c9", "a", "t")
        self.assertEqual(cm.exception.code, "duplicate_case")

    def test_certificate_no_unique_rolls_back(self):
        self._to_issued()
        # 再造一个案例到 approved，用同一证书号签发，必须整体回滚
        self.flow.create_draft("c2", "赵老师", "英语八年级", textbook="英语")
        self.flow.add_evidence("c2", "curriculum_alignment", "课标", "赵老师")
        self.flow.add_evidence("c2", "trial_feedback", "反馈", "赵老师")
        self.flow.submit_for_review("c2", "赵老师")
        self.flow.review("c2", "李审核", True, "通过")
        with self.assertRaises(WorkflowError) as cm:
            self.flow.issue("c2", "王签发", "CERT-2026-0001")
        self.assertEqual(cm.exception.code, "certificate_no_taken")
        view = self.flow.get("c2")
        self.assertEqual(view["case"]["state"], APPROVED)  # 状态未被污染
        self.assertIsNone(view["issuance"])


class EvidenceAndReviewTests(unittest.TestCase):
    def setUp(self):
        self.flow = make_flow()
        self.flow.create_draft("c1", "张老师", "数学", textbook="数学")

    def test_submit_requires_evidence(self):
        with self.assertRaises(WorkflowError) as cm:
            self.flow.submit_for_review("c1", "张老师")
        self.assertEqual(cm.exception.code, "evidence_incomplete")
        self.assertIn("trial_feedback", cm.exception.message)
        self.assertEqual(self.flow.get("c1")["case"]["state"], DRAFT)

    def test_partial_evidence_still_blocked(self):
        self.flow.add_evidence("c1", "curriculum_alignment", "课标", "张老师")
        with self.assertRaises(WorkflowError) as cm:
            self.flow.submit_for_review("c1", "张老师")
        self.assertEqual(cm.exception.code, "evidence_incomplete")

    def test_evidence_only_in_draft_or_rejected(self):
        self.flow.add_evidence("c1", "curriculum_alignment", "课标", "张老师")
        self.flow.add_evidence("c1", "trial_feedback", "反馈", "张老师")
        self.flow.submit_for_review("c1", "张老师")
        with self.assertRaises(WorkflowError) as cm:
            self.flow.add_evidence("c1", "other", "审核中补料", "张老师")
        self.assertEqual(cm.exception.code, "evidence_not_allowed")

    def test_reject_resubmit_loop(self):
        self.flow.add_evidence("c1", "curriculum_alignment", "课标", "张老师")
        self.flow.add_evidence("c1", "trial_feedback", "反馈", "张老师")
        self.flow.submit_for_review("c1", "张老师")
        view, _ = self.flow.review("c1", "李审核", False, "样本章节缺失，需补正")
        self.assertEqual(view["case"]["state"], REJECTED)
        self.assertIn("审核驳回", view["state_change"]["reason"])
        # 驳回后补证据，再送审，再通过
        self.flow.add_evidence("c1", "sample_chapter", "样本章节", "张老师")
        view, _ = self.flow.submit_for_review("c1", "张老师")
        self.assertEqual(view["case"]["state"], REVIEWING)
        view, _ = self.flow.review("c1", "李审核", True, "补正充分")
        self.assertEqual(view["case"]["state"], APPROVED)

    def test_review_requires_reason(self):
        self.flow.add_evidence("c1", "curriculum_alignment", "x", "a")
        self.flow.add_evidence("c1", "trial_feedback", "x", "a")
        self.flow.submit_for_review("c1", "a")
        with self.assertRaises(WorkflowError) as cm:
            self.flow.review("c1", "a", True, "  ")
        self.assertEqual(cm.exception.code, "reason_required")

    def test_invalid_transitions(self):
        # draft 不能直接签发
        with self.assertRaises(WorkflowError) as cm:
            self.flow.issue("c1", "a", "X1")
        self.assertEqual(cm.exception.code, "invalid_transition")
        # 取消后进入终态，不能再送审
        self.flow.cancel("c1", "张老师", "信息填错，作废重建")
        self.assertEqual(self.flow.get("c1")["case"]["state"], CANCELLED)
        with self.assertRaises(WorkflowError) as cm:
            self.flow.submit_for_review("c1", "张老师")
        self.assertEqual(cm.exception.code, "invalid_transition")

    def test_case_not_found(self):
        with self.assertRaises(WorkflowError) as cm:
            self.flow.get("nope")
        self.assertEqual(cm.exception.code, "case_not_found")


class CrossDateBoundaryTests(unittest.TestCase):
    """跨日期边界：23:59 创建，午夜后推进流程与检索。"""

    def setUp(self):
        self.before_midnight = datetime(2026, 9, 26, 23, 59, 50, tzinfo=timezone.utc)
        self.clock = MutableClock(self.before_midnight)
        self.flow = make_flow(self.clock)

    def test_workflow_across_midnight(self):
        view, _ = self.flow.create_draft("night1", "张老师", "深夜创建案例",
                                         textbook="物理")
        self.assertEqual(view["case"]["created_at"], "2026-09-26T23:59:50+00:00")

        self.clock.advance(seconds=20)  # -> 2026-09-27 00:00:10
        self.flow.add_evidence("night1", "curriculum_alignment", "课标", "张老师")
        self.flow.add_evidence("night1", "trial_feedback", "反馈", "张老师")
        view, _ = self.flow.submit_for_review("night1", "张老师")
        self.assertEqual(view["state_change"]["occurred_at"],
                         "2026-09-27T00:00:10+00:00")
        self.assertEqual(view["case"]["created_at"][:10], "2026-09-26")
        self.assertEqual(view["case"]["updated_at"][:10], "2026-09-27")

        self.clock.advance(seconds=86380)  # 当天 23:59:50，审核+签发
        view, _ = self.flow.review("night1", "李审核", True, "通过")
        view = self.flow.issue("night1", "王签发", "CERT-NIGHT-1")
        # 证书有效期从 27 日签发算起
        self.assertEqual(view["issuance"]["issued_at"][:10], "2026-09-27")
        self.assertEqual(view["issuance"]["valid_until"][:10], "2027-03-26")

    def test_search_date_ranges_across_boundary(self):
        # 26 日深夜一个
        self.flow.create_draft("night1", "张老师", "深夜案例", textbook="物理")
        self.clock.advance(seconds=20)
        # 27 日凌晨一个
        self.flow.create_draft("dawn1", "张老师", "凌晨案例", textbook="化学")

        day26 = self.flow.search(date_from="2026-09-26", date_to="2026-09-26")
        day27 = self.flow.search(date_from="2026-09-27", date_to="2026-09-27")
        both = self.flow.search(date_from="2026-09-26", date_to="2026-09-27")
        self.assertEqual([v["case"]["id"] for v in day26], ["night1"])
        self.assertEqual([v["case"]["id"] for v in day27], ["dawn1"])
        self.assertEqual([v["case"]["id"] for v in both], ["night1", "dawn1"])

        # 仅按下界过滤
        self.assertEqual(
            [v["case"]["id"] for v in self.flow.search(date_from="2026-09-27")],
            ["dawn1"])

        # 27 日对 night1 做过更新后，按 updated_at 能在 27 日检到
        self.flow.add_evidence("night1", "curriculum_alignment", "课标", "张老师")
        self.flow.add_evidence("night1", "trial_feedback", "反馈", "张老师")
        self.flow.submit_for_review("night1", "张老师")
        updated_on_27 = self.flow.search(
            date_from="2026-09-27", date_to="2026-09-27", date_field="updated_at")
        self.assertIn("night1", [v["case"]["id"] for v in updated_on_27])
        # 但按创建日期它仍属于 26 日
        created_on_27 = self.flow.search(
            date_from="2026-09-27", date_to="2026-09-27", date_field="created_at")
        self.assertNotIn("night1", [v["case"]["id"] for v in created_on_27])

    def test_issued_validity_crosses_years(self):
        # 180 天有效期跨年的日期算术正确
        self.clock.dt = datetime(2026, 12, 31, 23, 59, 55, tzinfo=timezone.utc)
        self.flow.create_draft("y1", "a", "t")
        self.clock.advance(seconds=10)  # 2027-01-01 00:00:05
        self.flow.add_evidence("y1", "curriculum_alignment", "x", "a")
        self.flow.add_evidence("y1", "trial_feedback", "x", "a")
        self.flow.submit_for_review("y1", "a")
        self.flow.review("y1", "r", True, "ok")
        view = self.flow.issue("y1", "i", "CERT-Y1")
        self.assertEqual(view["issuance"]["issued_at"][:10], "2027-01-01")
        self.assertEqual(view["issuance"]["valid_until"][:10], "2027-06-30")


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.flow = make_flow()
        self.flow.create_draft("c1", "张老师", "数学七年级上册", textbook="数学", grade="7")
        self.flow.create_draft("c2", "李老师", "数学八年级上册", textbook="数学", grade="8")
        self.flow.create_draft("c3", "张老师", "语文七年级上册", textbook="语文", grade="7")

    def test_filter_by_state_applicant_textbook_and_keyword(self):
        # 把 c1 推到 reviewing
        self.flow.add_evidence("c1", "curriculum_alignment", "x", "张老师")
        self.flow.add_evidence("c1", "trial_feedback", "x", "张老师")
        self.flow.submit_for_review("c1", "张老师")

        self.assertEqual(
            sorted(v["case"]["id"] for v in self.flow.search(state=DRAFT)),
            ["c2", "c3"])
        self.assertEqual(
            [v["case"]["id"] for v in self.flow.search(state=REVIEWING)], ["c1"])
        self.assertEqual(
            sorted(v["case"]["id"] for v in self.flow.search(applicant="张老师")),
            ["c1", "c3"])
        self.assertEqual(
            sorted(v["case"]["id"] for v in self.flow.search(textbook="数学")),
            ["c1", "c2"])
        self.assertEqual(
            [v["case"]["id"] for v in self.flow.search(q="八年级")], ["c2"])
        # 组合条件
        self.assertEqual(
            [v["case"]["id"]
             for v in self.flow.search(applicant="张老师", textbook="语文")],
            ["c3"])
        self.assertEqual(self.flow.search(state=ISSUED), [])

    def test_limit(self):
        self.assertEqual(len(self.flow.search(limit=2)), 2)

    def test_invalid_date_rejected(self):
        with self.assertRaises(ValueError):
            self.flow.search(date_from="09/26/2026")


class PersistenceTests(unittest.TestCase):
    def test_state_survives_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "trial.db")
            flow = make_flow(path=path)
            flow.create_draft("p1", "张老师", "持久化案例", textbook="数学")
            flow.add_evidence("p1", "curriculum_alignment", "课标", "张老师")
            flow.add_evidence("p1", "trial_feedback", "反馈", "张老师")
            flow.submit_for_review("p1", "张老师")
            flow.review("p1", "李审核", True, "通过")
            flow.issue("p1", "王签发", "CERT-PERSIST-1")
            flow.store.close()

            flow2 = make_flow(path=path)
            view = flow2.get("p1")
            self.assertEqual(view["case"]["state"], ISSUED)
            self.assertEqual(view["case"]["version"], 4)
            self.assertEqual(len(view["evidence"]), 2)
            self.assertEqual(view["issuance"]["certificate_no"], "CERT-PERSIST-1")
            # 创建 + 两次补证据 + 送审 + 审核 + 签发，共 6 条审计事件
            self.assertEqual(len(view["history"]), 6)
            flow2.store.close()

    def test_idempotency_survives_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "trial2.db")
            flow = make_flow(path=path)
            flow.create_draft("p2", "a", "t", idempotency_key="stable-key")
            flow.store.close()
            flow2 = make_flow(path=path)
            view, created = flow2.create_draft(
                "p2-other-id", "a", "t", idempotency_key="stable-key")
            self.assertFalse(created)
            self.assertEqual(view["case"]["id"], "p2")
            flow2.store.close()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.flow = make_flow()

    def call(self, method, path, body=None, query=None):
        return dispatch(self.flow, method, path, body, query)

    def test_api_full_loop(self):
        status, v = self.call("POST", "/cases", {
            "id": "a1", "applicant": "张老师", "title": "数学教材",
            "textbook": "数学", "idempotency_key": "api-k"})
        self.assertEqual(status, 201)
        self.assertEqual(v["state_change"]["to_state"], DRAFT)

        # 幂等重放返回 200
        status, v = self.call("POST", "/cases", {
            "id": "a1", "applicant": "张老师", "title": "数学教材",
            "idempotency_key": "api-k"})
        self.assertEqual(status, 200)

        status, v = self.call("POST", "/cases/a1/evidence",
                              {"kind": "curriculum_alignment", "title": "课标",
                               "actor": "张老师"})
        self.assertEqual(status, 201)
        self.assertIn("补充证据", v["state_change"]["reason"])

        status, _ = self.call("POST", "/cases/a1/evidence",
                              {"kind": "trial_feedback", "title": "反馈",
                               "actor": "张老师"})
        self.assertEqual(status, 201)

        status, v = self.call("POST", "/cases/a1/submit", {"actor": "张老师"})
        self.assertEqual(status, 200)
        self.assertEqual(v["case"]["state"], REVIEWING)

        status, v = self.call("POST", "/cases/a1/reviews",
                              {"actor": "李审核", "approved": True,
                               "reason": "材料齐全"})
        self.assertEqual(status, 200)
        self.assertEqual(v["case"]["state"], APPROVED)

        status, v = self.call("POST", "/cases/a1/issuance",
                              {"issuer": "王签发", "certificate_no": "CERT-API-1"})
        self.assertEqual(status, 201)
        self.assertEqual(v["case"]["state"], ISSUED)
        self.assertIn("CERT-API-1", v["state_change"]["reason"])

        status, v = self.call("GET", "/cases/a1")
        self.assertEqual(status, 200)
        self.assertEqual(v["issuance"]["certificate_no"], "CERT-API-1")

    def test_api_search_and_404(self):
        self.call("POST", "/cases",
                  {"id": "a1", "applicant": "张老师", "title": "数学",
                   "textbook": "数学"})
        status, v = self.call("GET", "/cases", query={"state": DRAFT, "q": "数学"})
        self.assertEqual(status, 200)
        self.assertEqual(len(v["items"]), 1)

        status, v = self.call("GET", "/cases/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(v["error"], "case_not_found")
        self.assertTrue(v["message"])  # 错误响应同样包含原因

        status, v = self.call("POST", "/nope", {})
        self.assertEqual(status, 404)

    def test_api_validation_and_business_errors(self):
        status, v = self.call("POST", "/cases", {"id": "x"})
        self.assertEqual(status, 400)
        self.assertIn("applicant", v["message"])

        self.call("POST", "/cases",
                  {"id": "x", "applicant": "a", "title": "t"})
        status, v = self.call("POST", "/cases/x/submit", {"actor": "a"})
        self.assertEqual(status, 422)
        self.assertEqual(v["error"], "evidence_incomplete")

        status, v = self.call("POST", "/cases/x/evidence",
                              {"kind": "bad_kind", "title": "t", "actor": "a"})
        self.assertEqual(status, 400)


class HttpServerTests(unittest.TestCase):
    """对标准库 HTTP 入口做一次真实端口冒烟测试。"""

    def test_http_roundtrip(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from service_09261_005.app import Handler

        flow = make_flow()
        import service_09261_005.app as app_module
        old = app_module.FLOW
        app_module.FLOW = flow
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()

            def req(method, path, payload=None):
                data = json.dumps(payload).encode() if payload is not None else None
                r = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}", data=data, method=method,
                    headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(r) as resp:
                        return resp.status, json.loads(resp.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())

            try:
                status, body = req("GET", "/health")
                self.assertEqual((status, body["status"]), (200, "ok"))
                status, body = req("POST", "/cases",
                                   {"id": "h1", "applicant": "a", "title": "t"})
                self.assertEqual(status, 201)
                self.assertEqual(body["state_change"]["to_state"], DRAFT)
                status, body = req("GET", "/cases?state=draft")
                self.assertEqual(status, 200)
                self.assertEqual(len(body["items"]), 1)
            finally:
                server.shutdown()
                server.server_close()
        finally:
            app_module.FLOW = old


if __name__ == "__main__":
    unittest.main()
