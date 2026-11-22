from dataclasses import replace
from unittest import TestCase

from app.documents import evidence_from_text
from app.graphs import build_graph
from app.retrieval import _neighbor_lines
from app.tasks import initial_state, report_from_state


class CompoundQuestionModel:
    parts = ["如何分级？", "应该多久响应？"]

    def complete_json(self, _instructions, data):
        if "cited_ids" in data:
            return {"status": "supported", "note": "表面上已回答。", "checks": [
                {"status": "supported", "answer": "工单不可用为最高级别。", "evidence_ids": ["L2"], "query": "故障级别"},
                {"status": "supported", "answer": "两小时内响应。", "evidence_ids": ["L1"]},
            ]}
        if "evidence" in data:
            ids = [row["id"] for row in data["evidence"]]
            answer = "工单不可用为最高级别。两小时内响应。" if "L2" in ids else "两小时内响应。"
            return {"claim": answer, "evidence_ids": ids}
        return {"query": "响应时限", "parts": self.parts}


class CompoundQuestionTest(TestCase):
    def run_case(self, text):
        graph = build_graph(CompoundQuestionModel())
        return graph.invoke(initial_state(
            "故障如何分级、应该多久响应？", evidence_from_text(text, "样例合同"),
            mode="live", hybrid=False, project=None,
        ))

    def test_partial_answer_rejects_uncited_part_and_preserves_verified_part(self):
        state = self.run_case("响应时限为两小时。")
        self.assertEqual(state["review"].status, "insufficient")
        self.assertEqual([item.status for item in state["review"].items], ["insufficient", "supported"])
        report = report_from_state(state)
        self.assertIn("两小时内响应", report)
        self.assertIn("如何分级？ · 证据不足", report)
        self.assertNotIn("工单不可用为最高级别", report)

    def test_queries_only_missing_part_and_rechecks_merged_evidence(self):
        state = self.run_case("响应时限为两小时。\n故障级别：工单不可用为最高级别。")
        self.assertTrue(state["retried_gaps"])
        self.assertEqual(state["review"].status, "supported")
        self.assertEqual({item.evidence_id for item in state["candidates"]}, {"L1", "L2"})

    def test_neighbors_keep_source_version_and_line_boundaries(self):
        rows = evidence_from_text("定义\n时限\n\n空行之后", "合同 A")
        other = replace(rows[1], source="合同 B", line=3, evidence_id="D2-L3")
        revision = replace(rows[1], version="other", line=3, evidence_id="S1-L3")
        self.assertEqual(_neighbor_lines((rows[1],), (*rows, other, revision)), (rows[0],))
