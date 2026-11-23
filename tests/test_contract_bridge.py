import os
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.api import app as console_app
from app.checkpoints import checkpoint_serializer, mysql_checkpointer
from app.contract_bridge import app
from app.tasks import task_graph


FILE = ("contract.txt", "合同编号 MT-2026-001\n计费周期 2026\n合同年费 CNY 12000.00\n".encode())
PAYMENT = ("PAY-001", "MT-2026-001", "2026", Decimal("5000.00"), "CNY")
SOURCE = {
    "id": "test", "label": "Test ledger", "database": "test", "table": "payments",
    "user": "read_only", "port": 3306,
}


class ContractModel:
    def __init__(self):
        self.calls = 0

    def complete_json(self, _instructions, data):
        self.calls += 1
        if "cited_ids" in data:
            return {"status": "supported", "note": "Verified", "checks": [
                {"question": data["question"], "status": "supported", "answer": data["claim"],
                 "evidence_ids": data["cited_ids"]},
            ]}
        return {"amount": "12000.00", "currency": "CNY", "evidence_ids": ["L1", "L2", "L3"]}


class ContractBridgeTest(TestCase):
    def test_general_options_are_persisted_and_project_keys_are_organization_scoped(self):
        storage = InMemorySaver(serde=checkpoint_serializer())
        organizations = [uuid4(), uuid4()]
        with patch.dict(os.environ, {"ANALYSIS_AGENT_BRIDGE_TOKEN": "test-only"}), (
            patch("app.api.mysql_checkpointer", lambda _: nullcontext(storage))
        ), patch("app.deepseek_client.DeepSeekClient.from_environment") as model:
            model.return_value.complete_json.return_value = {"prompt": "请确认范围", "directions": [], "scope": "核验材料"}
            client = TestClient(app, headers={"Authorization": "Bearer test-only"})
            states = []
            for org in organizations:
                run = uuid4()
                data = {"question": "请核验材料", "project": " 试点 ", "hybrid": "true", "organization_id": str(org)}
                self.assertEqual(client.post(f"/analysis/{run}", data=data).json()["status"], "clarifying")
                self.assertEqual(client.get(f"/investigations/{run}/report").status_code, 409)
                self.assertEqual(client.post(f"/analysis/{run}", data={**data, "hybrid": "false"}).status_code, 409)
                state = task_graph("offline", storage).get_state({"configurable": {"thread_id": run.hex}}).values
                self.assertTrue(state["hybrid"])
                states.append(state["project"])
            self.assertNotEqual(*states)

    def test_failure_resume_and_repeated_start_use_same_checkpoint(self):
        run_id = uuid4()
        route = f"/investigations/{run_id}"
        model = ContractModel()
        storage = InMemorySaver(serde=checkpoint_serializer())
        persisted = os.environ.get("ANALYSIS_AGENT_BRIDGE_TEST_MYSQL") == "1"
        saver = mysql_checkpointer if persisted else lambda _: nullcontext(storage)
        with TemporaryDirectory() as temp, (
            patch.dict(os.environ, {"ANALYSIS_AGENT_BRIDGE_TOKEN": "test-only", "ANALYSIS_AGENT_BRIDGE_SOURCE": "test"})
        ), patch("app.api.mysql_checkpointer", saver), patch("app.api.UPLOAD_DIR", Path(temp)), (
            patch("app.observability.TRACE_DIR", Path(temp) / "traces")
        ), patch("app.deepseek_client.DeepSeekClient.from_environment", return_value=model), (
            patch("app.payments.payment_sources", return_value={"test": SOURCE})
        ), patch("app.payments._payment_rows", side_effect=[RuntimeError("injected"), (PAYMENT,)]) as payment_rows:
            self.assertEqual(TestClient(app).get(route).status_code, 401)
            client = TestClient(app, headers={"Authorization": "Bearer test-only"})
            request = {
                "data": {"contract_id": "MT-2026-001", "billing_period": "2026"},
                "files": {"file": FILE},
            }
            self.assertEqual(client.post(route, **request).status_code, 503)
            before = client.get(route).json()
            self.assertEqual(before["task_id"], run_id.hex)
            self.assertEqual(before["status"], "incomplete")
            self.assertEqual(client.get(f"{route}/report").status_code, 409)
            self.assertEqual(before["active_step"], "s2")
            self.assertEqual(before["next_nodes"], ["retrieve_step"])
            self.assertEqual(before["steps"][0]["status"], "supported")
            verified_call_count = model.calls
            self.assertGreater(verified_call_count, 0)
            self.assertEqual(client.post(route, **request).json(), before)
            self.assertEqual(payment_rows.call_count, 1)
            self.assertEqual(model.calls, verified_call_count)

            done = client.post(f"{route}/retry").json()
            self.assertEqual(done["status"], "completed")
            self.assertEqual(done["review_status"], "supported")
            self.assertEqual(done["balance"], {"amount": "7000.00", "currency": "CNY"})
            self.assertEqual(done["steps"][0], before["steps"][0])
            self.assertEqual(client.post(f"{route}/retry").json(), done)
            self.assertEqual(model.calls, verified_call_count)
            self.assertEqual(payment_rows.call_count, 2)
            report = client.get(f"{route}/report")
            self.assertEqual(report.status_code, 200)
            self.assertIn("7000", report.text.replace(",", ""))
            self.assertEqual(TestClient(app).get(f"{route}/report").status_code, 401)
            self.assertEqual(TestClient(console_app, base_url="http://127.0.0.1").get(f"/tasks/{run_id}").status_code, 404)
