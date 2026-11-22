from unittest import TestCase

from langgraph.checkpoint.memory import InMemorySaver

from app.checkpoints import checkpoint_serializer
from app.documents import evidence_from_text
from app.graphs import build_graph
from app.planning import latest_results, make_plan, validate_plan
from app.tasks import (
    finish_with_current_evidence,
    initial_state,
    input_receipt,
    load_snapshot,
    report_from_state,
    resume_with_document,
)


QUESTION = "核对金额、服务清单，以及金额与服务是否对应"


class DocumentModel:
    def __init__(self):
        self.calls = []

    def complete_json(self, _instructions, data):
        question = data["question"]
        if "cited_ids" in data:
            self.calls.append(("review", question))
            return {"status": "supported", "note": "测试原文直接支持。", "checks": [
                {"question": question, "status": "supported", "answer": data["claim"],
                 "evidence_ids": data["cited_ids"]},
            ]}
        if "evidence" in data:
            self.calls.append(("resolve", question))
            if question == "金额与服务是否对应？":
                assert len(data["dependencies"]) == 2
            return {
                "claim": "；".join(item["text"] for item in data["evidence"]),
                "evidence_ids": [item["id"] for item in data["evidence"]],
            }
        self.calls.append(("plan", question))
        return {"steps": [
            {"id": "s3", "question": "金额与服务是否对应？", "target": "金额与服务是否对应",
             "depends_on": ["s1", "s2"], "queries": ["关联条款"]},
            {"id": "s2", "question": "包含哪些服务？", "target": "服务清单",
             "depends_on": [], "queries": ["服务清单", "巡检"]},
            {"id": "s1", "question": "每年收费多少？", "target": "金额",
             "depends_on": [], "queries": ["年费", "每年收费"]},
        ]}


class ExecutionTest(TestCase):
    def setUp(self):
        self.model = DocumentModel()
        self.saver = InMemorySaver(serde=checkpoint_serializer())
        self.config = {"configurable": {"thread_id": "execution-test"}}
        self.graph = build_graph(self.model, checkpointer=self.saver)
        document = evidence_from_text("# 虚构报价\n维护每年收费 12,000 元。", "报价")
        self.graph.invoke(
            initial_state(QUESTION, document, mode="live", hybrid=False, project=None, multistep=True),
            self.config,
        )

    def test_resume_preserves_plan_and_verified_step(self):
        snapshot = load_snapshot(self.graph, self.config)
        plan = snapshot.values["plan_steps"]
        before = latest_results(snapshot.values["step_results"])
        self.assertEqual(before["s1"].attempted_queries, ("年费", "每年收费"))
        self.assertEqual(before["s2"].review.status, "insufficient")
        self.assertNotIn("s3", before)
        self.assertEqual(self.model.calls, [
            ("plan", QUESTION), ("resolve", "每年收费多少？"), ("review", "每年收费多少？"),
        ])

        new_graph = build_graph(self.model, checkpointer=self.saver)
        state = resume_with_document(
            new_graph, self.config, snapshot, input_receipt(snapshot),
            lambda: evidence_from_text("服务包含定期巡检。", "补充报价"),
        )
        self.assertEqual(state["plan_steps"], plan)
        self.assertEqual(latest_results(state["step_results"])["s1"], before["s1"])
        self.assertEqual(self.model.calls[-4:], [
            ("resolve", "包含哪些服务？"), ("review", "包含哪些服务？"),
            ("resolve", "金额与服务是否对应？"), ("review", "金额与服务是否对应？"),
        ])
        self.assertEqual(self.model.calls.count(("plan", QUESTION)), 1)
        self.assertEqual(self.model.calls.count(("review", "每年收费多少？")), 1)
        report = report_from_state(state)
        self.assertIn("引用：L2", report)
        self.assertIn("引用：S1-L1", report)
        self.assertIn("全部分析步骤已获得原文直接支持", report)

    def test_finish_retains_supported_result_and_marks_blocked_dependency(self):
        snapshot = load_snapshot(self.graph, self.config)
        state = finish_with_current_evidence(self.graph, self.config, snapshot, input_receipt(snapshot))
        report = report_from_state(state)
        self.assertIn("1/3 个分析步骤获得原文直接支持", report)
        self.assertIn("依赖尚未获得原文直接支持", report)
        self.assertIn("12,000 元", report)


class PlanValidationTest(TestCase):
    def test_merges_repeated_target_and_redirects_dependencies(self):
        class Model:
            def complete_json(self, _instructions, _data):
                return {"steps": [
                    {"id": "s1", "question": "包含哪些服务？", "target": "服务范围",
                     "depends_on": [], "queries": ["服务清单"]},
                    {"id": "s2", "question": "逐项确认服务", "target": "服务范围",
                     "depends_on": ["s1"], "queries": ["服务"]},
                    {"id": "s3", "question": "范围内的巡检频率？", "target": "巡检频率",
                     "depends_on": ["s2"], "queries": ["巡检"]},
                ]}
        first, second = make_plan("列出服务范围及巡检频率。", Model())
        self.assertEqual((first.id, second.id), ("s1", "s3"))
        self.assertEqual(second.depends_on, ("s1",))

    def test_rejects_missing_dependencies_duplicate_ids_and_cycles(self):
        a = {"id": "s1", "question": "收费金额", "depends_on": ["s2"], "queries": ["收费"]}
        b = {"id": "s2", "question": "费用条款", "depends_on": ["s1"], "queries": ["条款"]}
        for steps, message in [([a], "不存在"), ([a, a], "重复"), ([a, b], "循环")]:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_plan(steps)
