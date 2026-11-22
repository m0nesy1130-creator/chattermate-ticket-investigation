from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from app.contracts import Money
from app.documents import evidence_from_text
from app.graphs import build_graph
from app.payments import calculate_balance, payment_scope, query_payments
from app.tasks import initial_state, report_from_state


SOURCE = {
    "id": "test", "label": "测试付款台账", "database": "test_payments", "table": "posted_payments",
    "user": "test_reader", "port": 3306,
}
SCOPE = {"source": SOURCE, "contract_id": "MT-2026-001", "billing_period": "2026"}
ROWS = (
    ("P-01", "MT-2026-001", "2026", Decimal("3200.25"), "CNY"),
    ("P-02", "MT-2026-001", "2026", Decimal("1799.75"), "CNY"),
)


class ContractModel:
    def complete_json(self, _instructions, data):
        if "cited_ids" in data:
            return {"status": "supported", "note": "费用及合同周期有原文支持。", "checks": [
                {"question": data["question"], "status": "supported", "answer": data["claim"],
                 "evidence_ids": data["cited_ids"]},
            ]}
        return {"amount": "12000.00", "currency": "CNY", "evidence_ids": ["L1", "L2", "L3"]}


class PaymentTest(TestCase):
    def test_plan_calculates_balance_from_contract_and_matching_rows(self):
        document = evidence_from_text("合同编号：MT-2026-001\n计费周期：2026\n每年收费人民币 12,000.00 元。", "虚构合同")
        graph = build_graph(ContractModel())
        with patch("app.payments._payment_rows", return_value=ROWS):
            state = graph.invoke(initial_state(
                "核对付款", document, mode="live", hybrid=False, project=None, multistep=True, payment=SCOPE,
            ))
        self.assertEqual([step.money.amount for step in state["step_results"]], ["12000.00", "5000.00", "7000.00"])
        report = report_from_state(state)
        for text in ("剩余未付 CNY 7,000.00", "记录 P-01", "记录 P-02", "billing_period = 2026", "虚构合同，第 3 行"):
            self.assertIn(text, report)

    def test_empty_results_and_mismatched_currencies_do_not_produce_a_balance(self):
        with patch("app.payments._payment_rows", return_value=()):
            _, claim, review, money = query_payments(SCOPE)
        self.assertEqual(review.status, "insufficient")
        self.assertIsNone(claim)
        self.assertIsNone(money)
        claim, review, money = calculate_balance(Money("12000.00", "CNY"), Money("5000.00", "USD"), ())
        self.assertEqual(review.status, "conflicting")
        self.assertIsNone(claim)
        self.assertIsNone(money)

    def test_missing_scope_is_requested_before_query(self):
        with patch("app.payments.payment_sources", return_value={"test": SOURCE}):
            with self.assertRaisesRegex(ValueError, "补充合同编号和计费周期"):
                payment_scope("test", None, None)
