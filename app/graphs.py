"""LangGraph 依次运行规划、检索、分析和复核。"""

from datetime import datetime, timezone
from typing import Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .adaptive import ToolObservation, choose_action, execute_action, unique_evidence
from .clarification import Clarification, ClarificationTurn, assess_request
from .contracts import Claim, Evidence, Plan, PlanStep, RejectedDirection, Review, TaskRequest
from .database_query import QuerySource
from .deepseek_client import DeepSeekClient
from .directories import DirectorySource
from .documents import search_documents, supplement_evidence
from .evidence import resolve_claim, review_claim, supplement_question
from .execution import PlanExecutionState, add_execution_nodes
from .memory import recall_evidence, remember_supported
from .observability import observed_node
from .payments import PaymentScope, payment_plan
from .planning import make_adaptive_plan, make_plan


class AnalysisState(PlanExecutionState, TypedDict, total=False):
    bridge_fingerprint: str
    request: TaskRequest
    document: tuple[Evidence, ...]
    directory: DirectorySource
    payment: PaymentScope
    plan: Plan
    question_parts: tuple[str, ...]
    retried_gaps: bool
    candidates: tuple[Evidence, ...]
    claim: Claim | None
    review: Review
    supplemented: bool
    mode: Literal["offline", "live"]
    hybrid: bool
    project: str
    reviewed_at: str
    clarify: bool
    clarification: Clarification
    clarification_turns: list[ClarificationTurn]
    original_question: str
    adaptive: bool
    web_enabled: bool
    query_source: QuerySource
    planning_evidence: tuple[Evidence, ...]
    planning_rejected: tuple[RejectedDirection, ...]
    tool_observations: list[ToolObservation]
    planning_action: dict
    planning_round_start: int


def build_graph(
    client: DeepSeekClient | None = None, *,
    checkpointer: BaseCheckpointSaver | None = None,
    preset_plan: tuple[PlanStep, ...] = (),
):
    """提供检查点时，可暂停等待范围确认或接收补充文档。"""
    def first_planning_node(state: AnalysisState) -> Literal["plan_probe", "plan"]:
        return "plan_probe" if state.get("adaptive") and not state.get("payment") else "plan"

    def entry(state: AnalysisState) -> Literal["assess", "plan_probe", "plan"]:
        return "assess" if state.get("clarify") else first_planning_node(state)

    def assess(state: AnalysisState) -> dict:
        if payment := state.get("payment"):
            scope = f"核对合同 {payment['contract_id']} 在计费周期 {payment['billing_period']} 的年费、已付款和未付余额。"
            return {"clarification": {
                "prompt": "请核对所选台账、合同编号和计费周期。范围需要调整时，可在新建任务中修改这些字段。",
                "directions": [], "scope": scope,
            }}
        clarification = assess_request(
            state["request"].question,
            state.get("clarification_turns", []),
            state.get("clarification", {}).get("scope"),
            has_document=bool(state["document"]),
            has_project=bool(state.get("project")),
            web_enabled=state.get("web_enabled", False),
            database_enabled=bool(state.get("query_source")),
            client=client,
        )
        return {"clarification": clarification}

    def clarify_input(state: AnalysisState) -> Command[Literal["assess", "plan_probe", "plan"]]:
        proposal = state["clarification"]
        reply = interrupt({"kind": "clarification", **proposal})
        if reply["action"] == "confirm":
            scope = proposal["scope"]
            assert scope is not None
            return Command(
                update={"original_question": state["request"].question, "request": TaskRequest(scope)},
                goto=first_planning_node(state),
            )
        turn = ClarificationTurn(prompt=proposal["prompt"], direction=reply["direction"], answer=reply["answer"])
        return Command(
            update={"clarification_turns": [*state.get("clarification_turns", []), turn]},
            goto="assess",
        )

    def plan_probe(state: AnalysisState) -> Command[Literal["plan_observe", "plan_input", "plan"]]:
        observations = state.get("tool_observations", [])
        if len(observations) - state.get("planning_round_start", 0) >= 2:
            return Command(goto="plan")
        if client is None:
            raise RuntimeError("按证据规划需要 DeepSeek。")
        action = choose_action(
            client, state, phase="plan", question=state["request"].question,
            evidence=state.get("planning_evidence", ()),
            feedback=observations[-1]["result"] if observations else "",
        )
        if action["tool"] == "finish":
            return Command(goto="plan")
        if action["tool"] == "ask":
            if checkpointer is None:
                raise ValueError(action["prompt"])
            proposal = Clarification(prompt=action["prompt"], directions=[], scope=None)
            return Command(update={"clarification": proposal}, goto="plan_input")
        return Command(update={"planning_action": action}, goto="plan_observe")

    def plan_observe(state: AnalysisState) -> dict:
        observation, evidence = execute_action(state, state["planning_action"], state["request"].question, phase="plan")
        return {
            "tool_observations": [*state.get("tool_observations", []), observation],
            "planning_evidence": unique_evidence(state.get("planning_evidence", ()), evidence),
        }

    def plan_input(state: AnalysisState) -> Command[Literal["plan_probe"]]:
        proposal = state["clarification"]
        reply = interrupt({"kind": "clarification", **proposal})
        turn = ClarificationTurn(prompt=proposal["prompt"], direction=None, answer=reply["answer"])
        return Command(update={
            "clarification_turns": [*state.get("clarification_turns", []), turn],
            "planning_round_start": len(state.get("tool_observations", [])),
        }, goto="plan_probe")

    def plan(state: AnalysisState) -> dict:
        question = state["request"].question
        if preset_plan:
            return {"plan_steps": preset_plan}
        if payment := state.get("payment"):
            return {"plan_steps": payment_plan(payment)}
        if state.get("adaptive"):
            if client is None:
                raise RuntimeError("按证据规划需要 DeepSeek。")
            steps, rejected = make_adaptive_plan(question, state, client)
            return {"plan_steps": steps, "planning_rejected": rejected}
        if state.get("multistep"):
            if client is None:
                raise ValueError("多步分析需要 DeepSeek 生成计划。")
            return {"plan_steps": make_plan(question, client)}
        if client is None:
            return {"plan": Plan(query=question)}
        result = client.complete_json(
            '将问题转成一个短的原文检索词 query，并把用户要求回答的独立事实列在 parts。'
            '每项必须保留对象、时间等约束。不把查找、核对或报告要求单列。'
            '单一问题只列一项，最多六项。例：'
            '{"query":"培训通知","parts":["培训地点在哪里？","报到时间是几点？"]}。',
            {"question": question},
        )
        query = result.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("模型未生成有效的检索词。")
        raw_parts = result.get("parts")
        if not isinstance(raw_parts, list) or not 1 <= len(raw_parts) <= 6 or any(
            not isinstance(part, str) or not part.strip() for part in raw_parts
        ):
            parts = ()
        else:
            parts = tuple(dict.fromkeys(part.strip() for part in raw_parts))
        return {"plan": Plan(query=query.strip()), "question_parts": parts}

    def after_plan(state: AnalysisState) -> Literal["retrieve", "dispatch_step"]:
        return "dispatch_step" if state.get("plan_steps") else "retrieve"

    def search(state: AnalysisState, document: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
        hybrid = state.get("hybrid", False)
        if hybrid and client is None:
            raise ValueError("混合检索需要 DeepSeek 复核，请使用 --live。")
        return search_documents(state["plan"].query, state["request"].question, document, hybrid=hybrid)

    def retrieve(state: AnalysisState) -> dict:
        return {"candidates": search(state, state["document"])}

    def resolve(state: AnalysisState) -> dict:
        candidates = state["candidates"]
        if project := state.get("project"):
            current = tuple(item for item in candidates if not item.evidence_id.startswith("M-"))
            recalled = recall_evidence(project, state["document"], lambda items: search(state, items))
            candidates = (*current, *recalled)
        claim = resolve_claim(state["request"].question, candidates, client, parts=state.get("question_parts", ()))
        return {"candidates": candidates, "claim": claim}

    def review(state: AnalysisState) -> dict:
        result = review_claim(
            state["request"].question, state["claim"], state["candidates"], client,
            parts=state.get("question_parts", ()),
        )
        if state.get("project") and result.status == "supported":
            return {"review": result, "reviewed_at": datetime.now(timezone.utc).isoformat()}
        return {"review": result}

    def remember(state: AnalysisState) -> dict:
        claim = state["claim"]
        assert claim is not None
        cited = tuple(item for item in state["candidates"] if item.evidence_id in claim.evidence_ids)
        remember_supported(
            state["project"], state["request"].question, claim, cited, state["document"], state["reviewed_at"]
        )
        return {}

    def after_review(state: AnalysisState) -> Literal["remember", "retrieve_gaps", "supplement", "__end__"]:
        if state.get("project") and state["review"].status == "supported":
            return "remember"
        if (
            client is not None and state["review"].status == "insufficient"
            and len(state["review"].items) > 1 and not state.get("retried_gaps")
        ):
            return "retrieve_gaps"
        if (
            checkpointer is not None
            and state["review"].status == "insufficient"
            and not state.get("supplemented")
        ):
            return "supplement"
        return END

    def search_gaps(state: AnalysisState, document: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
        return unique_evidence(*(
            search_documents(item.query or item.question, item.question, document, hybrid=state.get("hybrid", False))
            for item in state["review"].items if item.status == "insufficient"
        ))

    def retrieve_gaps(state: AnalysisState) -> Command[Literal["resolve", "supplement", "__end__"]]:
        candidates = unique_evidence(state["candidates"], search_gaps(state, state["document"]))
        if len(candidates) > len(state["candidates"]):
            return Command(update={"retried_gaps": True, "candidates": candidates}, goto="resolve")
        next_node = "supplement" if checkpointer is not None and not state.get("supplemented") else END
        return Command(update={"retried_gaps": True}, goto=next_node)

    def supplement(state: AnalysisState) -> Command[Literal["resolve", "__end__"]]:
        review = state["review"]
        reply = interrupt(supplement_question(state["request"].question, review if len(review.items) > 1 else None))
        additions = supplement_evidence(state["document"], reply, 1)
        matches = search(state, additions)
        if len(review.items) > 1:
            matches = unique_evidence(matches, search_gaps(state, additions))
        current = tuple(item for item in state["candidates"] if not item.evidence_id.startswith("M-"))
        return Command(
            update={
                "document": (*state["document"], *additions),
                "candidates": (*current, *matches),
                "supplemented": True,
                "retried_gaps": False,
            },
            goto="resolve" if matches else END,
        )

    graph = StateGraph(AnalysisState)
    graph.add_node("assess", observed_node(assess, "clarify"))
    graph.add_node("clarify_input", clarify_input)
    graph.add_node("plan", observed_node(plan, "plan"))
    graph.add_node("plan_probe", observed_node(plan_probe, "plan"))
    graph.add_node("plan_observe", observed_node(plan_observe, "retrieve"))
    graph.add_node("plan_input", plan_input)
    graph.add_node("retrieve", observed_node(retrieve, "retrieve"))
    graph.add_node("resolve", observed_node(resolve, "resolve"))
    graph.add_node("review", observed_node(review, "review"))
    graph.add_node("retrieve_gaps", observed_node(retrieve_gaps, "retrieve"))
    graph.add_node("remember", remember)
    graph.add_node("supplement", supplement)
    add_execution_nodes(graph, client, can_pause=checkpointer is not None)
    graph.add_conditional_edges(START, entry)
    graph.add_edge("assess", "clarify_input")
    graph.add_edge("plan_observe", "plan_probe")
    graph.add_conditional_edges("plan", after_plan)
    graph.add_edge("retrieve", "resolve")
    graph.add_edge("resolve", "review")
    graph.add_conditional_edges("review", after_review)
    graph.add_edge("remember", END)
    return graph.compile(checkpointer=checkpointer)
