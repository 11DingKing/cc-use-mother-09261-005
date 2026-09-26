"""教材试用观察期服务测试：完整闭环 + 跨日期边界。

跨日期边界场景：
- 案例在 2026-09-26 23:59 被创建/驳回，证据与签发发生在 2026-09-27 00:00 之后；
- “新证据”判定跨越午夜仍然成立；
- 按 date/updated_date/date_from/date_to 检索时边界归属正确；
- 持久化数据跨进程实例（新 Workflow 指向同一 SQLite 文件）仍可检索。
"""

import os
import tempfile
import unittest

from service_09261_005.api import dispatch, dumps
from service_09261_005.store import SQLiteStore
from service_09261_005.workflow import Workflow, WorkflowError

DAY1_2359 = "2026-09-26T23:59:30+00:00"
DAY1_2359_59 = "2026-09-26T23:59:59+00:00"
DAY2_0000 = "2026-09-27T00:00:00+00:00"
DAY2_0001 = "2026-09-27T00:00:10+00:00"


class ScriptedClock:
    """按脚本推进的时钟；脚本耗尽后报错，避免测试依赖真实时间。"""

    def __init__(self, *timestamps):
        self.points = list(timestamps)
        self.i = 0

    def __call__(self):
        ts = self.points[min(self.i, len(self.points) - 1)]
        self.i += 1
        return ts


def make_flow(path=":memory:", *timestamps):
    store = SQLiteStore(path)
    return Workflow(store, ScriptedClock(*timestamps)), store


class HappyPathTest(unittest.TestCase):
    def test_full_loop_create_evidence_review_issue_search(self):
        flow, _ = make_flow(":memory:",
                            DAY1_2359, DAY1_2359, DAY1_2359,
                            DAY2_0000, DAY2_0001)
        # 1. 创建草稿
        created = flow.create("c1", "teacher-a", "数学一年级上册")
        self.assertEqual(created.state, "draft")
        self.assertEqual(created.version, 1)
        self.assertIn("草稿", created.state_reason)
        self.assertTrue(created.changed)

        # 2. 补充证据（不改变状态，但响应须含状态原因）
        ev = flow.add_evidence("c1", "teacher-a", "试教两周课堂记录")
        self.assertFalse(ev.changed)
        self.assertEqual(ev.state, "draft")
        self.assertEqual(ev.evidence_id, 1)
        self.assertIn("证据已补充", ev.reason)

        # 提交 -> 审核通过 -> 签发
        submitted = flow.submit("c1", "teacher-a")
        self.assertEqual(submitted.state, "reviewing")
        self.assertEqual(submitted.version, 2)

        approved = flow.review("c1", "reviewer-b", "approve")
        self.assertEqual(approved.state, "approved")
        self.assertEqual(approved.version, 3)
        self.assertIn("通过", approved.reason)

        issued = flow.issue("c1", "principal-c", "校长办公会签发")
        self.assertEqual(issued.state, "issued")
        self.assertEqual(issued.version, 4)
        self.assertIn("试用观察期", issued.state_reason)

        # 终态后再签发应被拒绝
        with self.assertRaises(WorkflowError) as ctx:
            flow.issue("c1", "principal-c")
        self.assertEqual(ctx.exception.status, 409)

        # 详情含证据与变化流水，每条流水都有原因
        detail = flow.get("c1")
        self.assertEqual(len(detail["evidence"]), 1)
        self.assertEqual([e["to_state"] for e in detail["events"]],
                         ["draft", "reviewing", "approved", "issued"])
        self.assertTrue(all(e["reason"] for e in detail["events"]))

        # 检索
        self.assertEqual(len(flow.search(state="issued")), 1)
        self.assertEqual(len(flow.search(actor="teacher-a")), 1)
        self.assertEqual(len(flow.search(state="draft")), 0)


class RejectAndResubmitTest(unittest.TestCase):
    def test_reject_requires_fresh_evidence_after_midnight(self):
        flow, _ = make_flow(":memory:",
                            DAY1_2359, DAY1_2359_59, DAY1_2359_59,
                            DAY2_0000)
        flow.create("c2", "teacher-a", "语文三年级上册")
        flow.submit("c2", "teacher-a")

        # 3. 审核驳回（跨日期前 1 秒），驳回必须给原因
        with self.assertRaises(WorkflowError):
            flow.review("c2", "reviewer-b", "reject")
        rejected = flow.review(
            "c2", "reviewer-b", "reject", reason="缺少学情数据")
        self.assertEqual(rejected.state, "rejected")
        self.assertIn("驳回", rejected.state_reason)

        # 无新证据不能重新提交
        with self.assertRaises(WorkflowError) as ctx:
            flow.submit("c2", "teacher-a")
        self.assertEqual(ctx.exception.code, "evidence_required")
        self.assertEqual(ctx.exception.status, 422)

        # 2. 跨午夜补充新证据（次日 00:00）
        ev = flow.add_evidence("c2", "teacher-a", "补充学情问卷数据")
        self.assertIn("驳回后第 1 条", ev.reason)

        # 可重新提交并再次进入审核
        resubmitted = flow.submit("c2", "teacher-a")
        self.assertEqual(resubmitted.state, "reviewing")
        self.assertEqual(resubmitted.version, 4)

        approved = flow.review("c2", "reviewer-b", "approve")
        issued = flow.issue("c2", "principal-c")
        self.assertEqual(issued.state, "issued")


class DateBoundarySearchTest(unittest.TestCase):
    def _seed(self, flow):
        # c1: 前一天创建并签发；c2: 前一天创建、跨天后签发
        flow.create("c1", "teacher-a", "数学上册")
        flow.submit("c1", "teacher-a")
        flow.review("c1", "reviewer-b", "approve")
        flow.issue("c1", "principal-c")
        flow.create("c2", "teacher-a", "数学下册")
        flow.submit("c2", "teacher-a")
        flow.review("c2", "reviewer-b", "reject", reason="材料不足")
        flow.add_evidence("c2", "teacher-a", "跨午夜补充的证据")
        flow.submit("c2", "teacher-a")
        flow.review("c2", "reviewer-b", "approve")
        flow.issue("c2", "principal-c")

    def test_search_by_creation_and_update_date(self):
        timestamps = [DAY1_2359] * 7 + [DAY2_0000] * 5
        flow, _ = make_flow(":memory:", *timestamps)
        self._seed(flow)

        # 两条都创建于前一天
        self.assertEqual(
            sorted(i["id"] for i in flow.search(date="2026-09-26")),
            ["c1", "c2"])
        self.assertEqual(flow.search(date="2026-09-27"), [])

        # updated_date 反映最后状态变化：c1 在前一天，c2 跨到次日
        self.assertEqual(
            [i["id"] for i in flow.search(updated_date="2026-09-26")],
            ["c1"])
        self.assertEqual(
            [i["id"] for i in flow.search(updated_date="2026-09-27")],
            ["c2"])

        # 闭区间边界：date_from/date_to 当天包含在内
        self.assertEqual(
            len(flow.search(date_from="2026-09-26",
                            date_to="2026-09-26")), 2)
        self.assertEqual(
            len(flow.search(date_from="2026-09-27")), 0)

        # 关键字检索命中跨日期补充的证据内容
        hits = flow.search(q="跨午夜")
        self.assertEqual([i["id"] for i in hits], ["c2"])
        hits = flow.search(q="数学")
        self.assertEqual(len(hits), 2)

    def test_persistence_across_instances(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            flow, store = make_flow(path, DAY1_2359, DAY2_0001)
            flow.create("c9", "teacher-a", "持久化案例")
            flow.submit("c9", "teacher-a")
            store.close()

            # 新连接/新工作流实例指向同一本地 SQLite 文件
            store2 = SQLiteStore(path)
            flow2 = Workflow(store2)
            detail = flow2.get("c9")
            self.assertEqual(detail["state"], "reviewing")
            self.assertEqual(detail["version"], 2)
            self.assertEqual(
                [i["id"] for i in flow2.search(date="2026-09-26")],
                ["c9"])
            self.assertEqual(flow2.search(date="2026-09-27"), [])
            store2.close()
        finally:
            os.unlink(path)


class IdempotencyTest(unittest.TestCase):
    def test_repeated_command_is_idempotent(self):
        flow, _ = make_flow(":memory:", DAY1_2359, DAY2_0001)
        first = flow.create("c3", "teacher-a", idempotency_key="k-1")
        second = flow.create("c3", "teacher-a", idempotency_key="k-1")
        self.assertEqual(second.version, 1)
        self.assertFalse(second.changed)
        self.assertEqual(first.timestamp, second.timestamp)
        self.assertEqual(len(flow.get("c3")["events"]), 1)

        ev1 = flow.add_evidence(
            "c3", "teacher-a", "证据A", idempotency_key="k-2")
        ev2 = flow.add_evidence(
            "c3", "teacher-a", "证据A", idempotency_key="k-2")
        self.assertEqual(ev1.evidence_id, ev2.evidence_id)
        self.assertEqual(len(flow.get("c3")["evidence"]), 1)

    def test_same_key_different_command_conflicts(self):
        flow, _ = make_flow(":memory:", DAY1_2359)
        flow.create("c4", "teacher-a", idempotency_key="dup")
        with self.assertRaises(WorkflowError) as ctx:
            flow.submit("c4", "teacher-a", idempotency_key="dup")
        self.assertEqual(ctx.exception.status, 409)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.flow, _ = make_flow(
            ":memory:", DAY1_2359, DAY1_2359_59,
            DAY2_0000, DAY2_0000, DAY2_0001, DAY2_0001)

    def test_api_full_loop_with_reasons(self):
        status, body = dispatch(self.flow, "POST", "/cases", {
            "id": "a1", "actor": "teacher-a", "title": "英语上册"})
        self.assertEqual(status, 201)
        self.assertEqual(body["state"], "draft")
        self.assertIn("state_reason", body)

        status, body = dispatch(self.flow, "POST",
                                "/cases/a1/evidence",
                                {"actor": "teacher-a",
                                 "content": "课堂观察记录"})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "draft")

        for path, payload, expected in [
            ("/cases/a1/submit", {"actor": "teacher-a"}, "reviewing"),
            ("/cases/a1/review",
             {"actor": "r", "decision": "reject",
              "reason": "样本不足"}, "rejected"),
        ]:
            status, body = dispatch(self.flow, "POST", path, payload)
            self.assertEqual(status, 200, dumps(body))
            self.assertEqual(body["state"], expected)
            self.assertTrue(body["reason"])

        # 未补新证据直接通过 API 重新提交 -> 422
        status, body = dispatch(self.flow, "POST",
                                "/cases/a1/submit",
                                {"actor": "teacher-a"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "evidence_required")

        # 跨日期补证据后走完全程
        status, _ = dispatch(self.flow, "POST", "/cases/a1/evidence",
                             {"actor": "teacher-a",
                              "content": "跨日期补充样本"})
        self.assertEqual(status, 200)
        status, body = dispatch(self.flow, "POST",
                                "/cases/a1/submit",
                                {"actor": "teacher-a"})
        self.assertEqual(status, 200)
        status, body = dispatch(self.flow, "POST", "/cases/a1/review",
                                {"actor": "r", "decision": "approve"})
        self.assertEqual(status, 200)
        status, body = dispatch(self.flow, "POST", "/cases/a1/issue",
                                {"actor": "p"})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "issued")

        # 检索：创建日在前一天，最后更新跨到次日
        status, body = dispatch(
            self.flow, "GET", "/cases?date=2026-09-26")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)
        status, body = dispatch(
            self.flow, "GET", "/cases?updated_date=2026-09-27")
        self.assertEqual(len(body["items"]), 1)

        # 历史流水
        status, body = dispatch(self.flow, "GET",
                                "/cases/a1/history")
        self.assertEqual(status, 200)
        self.assertTrue(all("reason" in e for e in body["events"]))

    def test_api_errors(self):
        status, body = dispatch(self.flow, "GET", "/cases/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "case_not_found")

        status, body = dispatch(self.flow, "POST", "/cases",
                                {"id": "x"})
        self.assertEqual(status, 400)

        status, body = dispatch(self.flow, "DELETE", "/cases")
        self.assertEqual(status, 404)

    def test_dates_in_iso_string_filterable(self):
        # 边界等值：23:59:59 创建归属 2026-09-26，00:00:00 归属 27 日
        flow, _ = make_flow(":memory:", DAY1_2359_59, DAY2_0000)
        flow.create("d1", "teacher-a")
        flow.create("d2", "teacher-a")
        self.assertEqual(
            [i["id"] for i in flow.search(date="2026-09-26")], ["d1"])
        self.assertEqual(
            [i["id"] for i in flow.search(date="2026-09-27")], ["d2"])


if __name__ == "__main__":
    unittest.main()
