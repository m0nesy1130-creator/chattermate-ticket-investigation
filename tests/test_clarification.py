from unittest import TestCase

from langgraph.checkpoint.memory import InMemorySaver

from app.checkpoints import checkpoint_serializer
from app.documents import evidence_from_text
from app.graphs import build_graph
from app.tasks import (
    TaskConflictError,
    TaskInputError,
    initial_state,
    input_receipt,
    load_snapshot,
    report_from_state,
    resume_with_clarification,
    resume_with_document,
)


class ClarifyingModel:
    scope = "合同中维护服务的年费是多少？"

    def __init__(self):
        self.calls = []

    def complete_json(self, _instructions, data):
        if "turns" in data:
            self.calls.append("assess")
            if not data["turns"]:
                return {"prompt": "要分析合同的哪一部分？", "directions": ["维护费用", "服务范围"], "scope": None}
            assert data["turns"][0]["direction"] == "维护费用"
            return {"prompt": "请确认分析范围。", "directions": [], "scope": self.scope}
        if "cited_ids" in data:
            self.calls.append("review")
            return {"status": "supported", "note": "报价直接支持。", "checks": [
                {"question": data["question"], "status": "supported", "answer": data["claim"],
                 "evidence_ids": data["cited_ids"]},
            ]}
        if "evidence" in data:
            self.calls.append("resolve")
            return {"claim": "每年 12,000 元。", "evidence_ids": ["L2"]}
        self.calls.append("plan")
        assert data["question"] == self.scope
        return {"query": "维护服务"}


class ClarificationTest(TestCase):
    def test_only_current_confirmation_starts_plan(self):
        model = ClarifyingModel()
        saver = InMemorySaver(serde=checkpoint_serializer())
        config = {"configurable": {"thread_id": "clarification-test"}}
        graph = build_graph(model, checkpointer=saver)
        document = evidence_from_text("# 虚构合同\n维护服务每年收费 12,000 元。", "验收样例")
        graph.invoke(initial_state("分析合同", document, mode="live", hybrid=False, project=None, clarify=True), config)
        first = load_snapshot(graph, config)
        first_receipt = input_receipt(first)

        with self.assertRaises(TaskInputError):
            resume_with_clarification(
                graph, config, first, first_receipt, action="confirm", answer=None, direction=None
            )
        with self.assertRaises(TaskConflictError):
            resume_with_document(graph, config, first, first_receipt, lambda: document)
        resume_with_clarification(
            graph, config, first, first_receipt, action="answer", answer="只看年费", direction=0
        )
        graph = build_graph(model, checkpointer=saver)
        second = load_snapshot(graph, config)
        self.assertEqual(model.calls, ["assess", "assess"])
        self.assertNotIn("plan", second.values)
        self.assertEqual(second.values["clarification"]["scope"], model.scope)
        with self.assertRaises(TaskConflictError):
            resume_with_clarification(
                graph, config, second, first_receipt, action="confirm", answer=None, direction=None
            )

        state = resume_with_clarification(
            graph, config, second, input_receipt(second), action="confirm", answer=None, direction=None
        )
        self.assertEqual(model.calls, ["assess", "assess", "plan", "resolve", "review"])
        self.assertEqual(state["request"].question, model.scope)
        self.assertIn("## 已确认分析范围", report_from_state(state))
        self.assertIn("引用：L2", report_from_state(state))
