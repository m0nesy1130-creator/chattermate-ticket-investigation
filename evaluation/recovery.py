"""注入 DeepSeek 传输故障，验收自动重试和 MySQL 检查点续跑。"""

import argparse
import json
from contextlib import nullcontext
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError, URLError
from unittest.mock import patch
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context

from app.api import retry_task
from app.checkpoints import checkpoint_serializer, mysql_checkpointer
from app.contracts import PlanStep
from app.deepseek_client import DeepSeekClient, ModelRequestError
from app.documents import evidence_from_text
from app.graphs import build_graph
from app.observability import collect_events
from app.planning import latest_results
from app.tasks import initial_state, load_snapshot


PLAN = (
    PlanStep("s1", "清洁费用是多少？", (), ("清洁费用",)),
    PlanStep("s2", "服务有效期是多久？", (), ("服务有效期",)),
)
_PAGES = {
    PLAN[0].question: ("L1", "清洁费用为 800 元。"),
    PLAN[1].question: ("L2", "服务有效期为 6 个月。"),
}


class ScriptedTransport:
    def __init__(self, fault, failures):
        self.fault = fault
        self.failures = failures
        self.review_calls = {step.question: 0 for step in PLAN}
        self.compact_seen = False

    def __call__(self, request, *, timeout):
        payload = json.loads(request.data)
        data = json.loads(payload["messages"][1]["content"])
        question = data["question"]
        ref, text = _PAGES[question]
        if "cited_ids" not in data:
            return self.response({"claim": text, "evidence_ids": [ref]})

        self.review_calls[question] += 1
        if question == PLAN[1].question and self.failures:
            self.failures -= 1
            if self.fault == "timeout":
                raise URLError(TimeoutError("合成超时"))
            if self.fault == "length":
                return self.response(None, "length")
            if self.fault == "rate_limit":
                headers = Message()
                headers["Retry-After"] = "1"
                raise HTTPError(request.full_url, 429, "mock", headers, BytesIO())
            raise HTTPError(request.full_url, 401, "mock", Message(), BytesIO())

        if question == PLAN[1].question:
            self.compact_seen = "上一次输出触及生成限制" in payload["messages"][0]["content"]
        return self.response({"status": "supported", "note": "附原文。", "checks": [
            {"question": question, "status": "supported", "answer": text, "evidence_ids": [ref]},
        ]})

    @staticmethod
    def response(content, finish_reason="stop"):
        return BytesIO(json.dumps({
            "choices": [{"finish_reason": finish_reason, "message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
        }).encode("utf-8"))


def recovery_case(fault, failures, *, mysql=False):
    thread_id = uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    saver = InMemorySaver(serde=checkpoint_serializer()) if not mysql else None

    def open_saver(task_id):
        return mysql_checkpointer(task_id) if mysql else nullcontext(saver)

    client = DeepSeekClient("synthetic", "test-model")
    transport = ScriptedTransport(fault, failures)
    before, prior_plan, failed_at, main_error, calls_at_failure = None, (), (), None, None
    with tracing_context(enabled=False), collect_events() as events, (
        patch("app.deepseek_client.urlopen", transport)
    ), patch("app.deepseek_client.sleep") as wait:
        with open_saver(thread_id) as active:
            graph = build_graph(client, checkpointer=active, preset_plan=PLAN)
            try:
                graph.invoke(initial_state(
                    "核对清洁费用与服务有效期", evidence_from_text("\n".join(text for _, text in _PAGES.values()), "合成条款"),
                    mode="live", hybrid=False, project=None, multistep=True,
                ), config, durability="sync")
            except ModelRequestError as error:
                failed = load_snapshot(graph, config)
                main_error, failed_at = error.reason, failed.next
                before = latest_results(failed.values["step_results"])["s1"]
                prior_plan = tuple(failed.values["plan_steps"])
                calls_at_failure = transport.review_calls[PLAN[1].question]

        if main_error is not None and fault != "auth":
            with patch("app.api.mysql_checkpointer", open_saver), patch(
                "app.deepseek_client.DeepSeekClient.from_environment", return_value=client,
            ):
                retry_task(UUID(thread_id))
        with open_saver(thread_id) as active:
            done = load_snapshot(build_graph(client, checkpointer=active), config)
        delays = [call.args[0] for call in wait.call_args_list]

    results = latest_results(done.values["step_results"])
    first_count = transport.review_calls[PLAN[0].question]
    second_count = transport.review_calls[PLAN[1].question]
    second_events = [event for event in events if event["node"] == "review_step" and event["step_id"] == "s2"]
    reasons = [attempt["reason"] for event in second_events for attempt in event["model_attempts"]]
    complete = not done.next and done.values.get("review") is not None and done.values["review"].status == "supported"
    prior_reused = before is not None and results["s1"] == before and tuple(done.values["plan_steps"]) == prior_plan
    if fault == "auth":
        passed = main_error == "http_error" and first_count == second_count == 1 and not delays and not complete
    else:
        passed = complete and first_count == 1 and second_count == failures + 1 and reasons == [fault] * failures + ["ok"]
        if failures == 2:
            passed = passed and failed_at == ("review_step",) and prior_reused and calls_at_failure == 2
        if fault == "length":
            passed = passed and transport.compact_seen and not delays
        if fault == "rate_limit":
            passed = passed and delays == [1.0]
    return {
        "fault": fault, "injections": failures, "passed": passed, "completed": complete, "failed_at": failed_at,
        "s1_review_attempts": first_count, "s2_review_attempts": second_count, "reused_prior_step": prior_reused,
        "compact_response_requested": transport.compact_seen, "trace": events, "thread_id": thread_id,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysql", action="store_true", help="使用真实 MySQL 检查点")
    args = parser.parse_args()
    scenarios = (("timeout", 1), ("rate_limit", 1), ("length", 1), ("timeout", 2), ("auth", 1))
    outcomes = [recovery_case(*scenario, mysql=args.mysql) for scenario in scenarios]
    print(json.dumps(outcomes, ensure_ascii=False, indent=2))
    if not all(row["passed"] for row in outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
