"""执行已固定的计划，将检索、判断和复核写入独立检查点。"""

from dataclasses import replace
from datetime import datetime, timezone
from graphlib import TopologicalSorter
from hashlib import sha256
from typing import Literal, TypedDict

from langgraph.graph import END
from langgraph.types import Command, interrupt

from .adaptive import choose_action, execute_action
from .contracts import Claim, Evidence, Money, PlanStep, Review, StepResult
from .documents import search_documents, supplement_evidence
from .evidence import resolve_claim, review_claim
from .memory import recall_evidence, remember_supported
from .observability import observed_node
from .payments import calculate_balance, contract_anchors, query_payments, resolve_contract_fee
from .planning import latest_results


class PlanExecutionState(TypedDict, total=False):
    multistep: bool
    plan_steps: tuple[PlanStep, ...]
    step_results: list[StepResult]
    document_revision: int
    active_step: str
    query_index: int
    step_candidates: tuple[Evidence, ...]
    step_claim: Claim | None
    step_review: Review | None
    step_money: Money | None
    step_feedback: str
    step_stopped: bool
    shared_evidence: tuple[Evidence, ...]
    latest_supplement: tuple[Evidence, ...]
    prior_step_claims: dict[str, str]


def _step(state: PlanExecutionState) -> PlanStep:
    return next(step for step in state["plan_steps"] if step.id == state["active_step"])


def _dependency_results(state: PlanExecutionState, step: PlanStep) -> tuple[StepResult, ...]:
    results = latest_results(state.get("step_results", []))
    return tuple(results[dependency] for dependency in step.depends_on)


def _unique_evidence(*groups: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    return tuple({item.evidence_id: item for group in groups for item in group}.values())


def add_execution_nodes(graph, client, *, can_pause: bool) -> None:
    def dispatch_step(state) -> Command[Literal["retrieve_step", "step_supplement", "summarize_steps"]]:
        results = latest_results(state.get("step_results", []))
        verified = {key for key, result in results.items() if result.review.status == "supported"}
        revision = state.get("document_revision", 0)
        for step in state["plan_steps"]:
            if step.id in verified or not verified.issuperset(step.depends_on):
                continue
            result = results.get(step.id)
            if result is None or result.revision < revision:
                return Command(
                    update={"active_step": step.id, "query_index": 0, "step_candidates": (),
                            "step_claim": None, "step_review": None, "step_money": None,
                            "step_feedback": "", "step_stopped": False},
                    goto="retrieve_step",
                )
        if can_pause:
            for step in state["plan_steps"]:
                if step.id in results and step.id not in verified:
                    return Command(update={"active_step": step.id}, goto="step_supplement")
        return Command(update={"active_step": ""}, goto="summarize_steps")

    def retrieve_step(state) -> dict:
        step = _step(state)
        if step.kind == "adaptive":
            inherited = tuple(item for result in _dependency_results(state, step) for item in result.evidence)
            supplement = state.get("latest_supplement", ())
            new_evidence = search_documents(
                step.question, step.question, supplement, hybrid=state.get("hybrid", False),
            ) if supplement else ()
            candidates = _unique_evidence(state["step_candidates"], state.get("planning_evidence", ()),
                                          state.get("shared_evidence", ()), inherited, new_evidence or supplement[:12])
            action = choose_action(client, state, phase="resolve", question=step.question, evidence=candidates,
                                   feedback=state.get("step_feedback", ""), preferred=step.preferred_tool, hints=step.queries)
            if action["tool"] == "finish":
                return {"step_candidates": candidates, "step_stopped": True}
            observation, evidence = execute_action(state, action, step.question, phase="resolve", step_id=step.id)
            return {"step_candidates": _unique_evidence(candidates, evidence),
                    "shared_evidence": _unique_evidence(state.get("shared_evidence", ()), evidence),
                    "tool_observations": [*state.get("tool_observations", []), observation]}
        if step.kind == "mysql_payments":
            evidence, claim, review, money = query_payments(state["payment"])
            return {"step_candidates": evidence, "step_claim": claim, "step_review": review, "step_money": money}
        if step.kind == "balance":
            fee, paid = _dependency_results(state, step)
            evidence = _unique_evidence(fee.evidence, paid.evidence)
            if fee.money is None or paid.money is None:
                raise RuntimeError("缺少可用于计算余额的已核验金额。")
            claim, review, money = calculate_balance(fee.money, paid.money, tuple(item.evidence_id for item in evidence))
            return {"step_candidates": evidence, "step_claim": claim, "step_review": review, "step_money": money}
        keyword = step.queries[state["query_index"]]

        def search(items: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
            return search_documents(keyword, step.question, items, hybrid=state.get("hybrid", False))

        found = search(state["document"])
        if project := state.get("project"):
            recalled = recall_evidence(project, state["document"], search)
            for item in recalled:
                # 同一原文在不同查询或进程中的引用 ID 保持一致。
                key = f"{item.source}\0{item.version}\0{item.line}".encode("utf-8")
                found = (*found, replace(item, evidence_id=f"M-{sha256(key).hexdigest()[:20]}"))
        dependency_evidence = tuple(
            item for result in _dependency_results(state, step) for item in result.evidence
        )
        anchors = contract_anchors(state["payment"], state["document"]) if step.kind == "contract_fee" else ()
        return {"step_candidates": _unique_evidence(state["step_candidates"], found, dependency_evidence, anchors)}

    def resolve_step(state) -> dict:
        step = _step(state)
        if state.get("step_review") is not None:
            return {}
        if step.kind == "contract_fee":
            if client is None:
                raise RuntimeError("合同年费抽取需要 DeepSeek。")
            claim, money = resolve_contract_fee(state["payment"], step.question, state["step_candidates"], client)
            return {"step_claim": claim, "step_money": money}
        claims = tuple(result.claim for result in _dependency_results(state, step) if result.claim is not None)
        return {"step_claim": resolve_claim(step.question, state["step_candidates"], client, claims)}

    def review_step(state) -> Command[Literal["retrieve_step", "dispatch_step"]]:
        step = _step(state)
        claim, candidates = state["step_claim"], state["step_candidates"]
        review = state.get("step_review")
        if review is None:
            review = review_claim(step.question, claim, candidates, client, check_rejections=step.kind == "adaptive")
        attempt_count = state["query_index"] + 1
        attempts = 3 if step.kind == "adaptive" else len(step.queries)
        if review.status != "supported" and attempt_count < attempts and not state.get("step_stopped"):
            return Command(update={"query_index": attempt_count, "step_feedback": review.note}, goto="retrieve_step")
        if claim is not None and review.status in {"supported", "inference"}:
            references = {*claim.evidence_ids, *(item.evidence_id for item in review.rejected)}
            candidates = tuple(item for item in candidates if item.evidence_id in references)
        queries = tuple(step.queries[:attempt_count]) if step.kind != "adaptive" else ()
        result = StepResult(
            step.id, state.get("document_revision", 0), claim, review, candidates,
            queries, datetime.now(timezone.utc).isoformat(),
            state.get("step_money") if review.status == "supported" else None,
        )
        return Command(
            update={"step_results": [*state.get("step_results", []), result]},
            goto="dispatch_step",
        )

    def step_supplement(state) -> Command[Literal["dispatch_step", "summarize_steps"]]:
        step = _step(state)
        result = latest_results(state["step_results"])[step.id]
        if step.kind in {"mysql_payments", "balance"}:
            instruction = "请核对台账或范围后新建任务，也可用已有证据生成部分报告。"
        elif step.kind == "contract_fee":
            instruction = "请补交同一份合同中能核对合同编号、计费周期、年费及币种的原文。"
        else:
            instruction = "需要补充原文。"
        reply = interrupt(f"步骤 {step.id}「{step.question}」{instruction}{result.review.note}")
        if not reply:
            return Command(update={"active_step": ""}, goto="summarize_steps")
        revision = state.get("document_revision", 0) + 1
        additions = supplement_evidence(state["document"], reply, revision)
        # New material can change any document-backed result and its dependents.
        # Keep independent ledger results on the original checkpoint.
        affected = {
            item.id for item in state["plan_steps"]
            if item.kind in {"contract_fee", "adaptive"} or (
                item.kind == "document" and any(
                    search_documents(query, item.question, additions, hybrid=state.get("hybrid", False))
                    for query in item.queries
                )
            )
        }
        dependencies = {item.id: item.depends_on for item in state["plan_steps"]}
        for step_id in TopologicalSorter(dependencies).static_order():
            if affected.intersection(dependencies[step_id]):
                affected.add(step_id)
        retained = [
            item for item in state["step_results"] if item.step_id not in affected
        ]
        return Command(
            update={
                "document": (*state["document"], *additions),
                "document_revision": revision,
                "latest_supplement": additions,
                "step_results": retained,
                "prior_step_claims": {
                    step_id: item.claim.text
                    for step_id, item in latest_results(state["step_results"]).items()
                    if step_id in affected and item.claim is not None
                },
            },
            goto="dispatch_step",
        )

    def summarize_steps(state) -> Command[Literal["remember_steps", "__end__"]]:
        results = latest_results(state.get("step_results", []))
        verified = sum(result.review.status == "supported" for result in results.values())
        total = len(state["plan_steps"])
        if verified == total:
            review = Review("supported", "全部分析步骤已获得原文直接支持。")
        else:
            status = "conflicting" if any(result.review.status == "conflicting" for result in results.values()) else "insufficient"
            review = Review(status, f"{verified}/{total} 个分析步骤获得原文直接支持，其余范围见各步骤说明。")
        return Command(update={"review": review}, goto="remember_steps" if verified and state.get("project") else END)

    def remember_steps(state) -> dict:
        results = latest_results(state.get("step_results", []))
        for step in state["plan_steps"]:
            result = results.get(step.id)
            if (
                step.kind in {"document", "contract_fee", "adaptive"} and result is not None
                and result.review.status == "supported" and result.claim is not None
            ):
                cited = tuple(item for item in result.evidence if item.evidence_id in result.claim.evidence_ids)
                remember_supported(
                    state["project"], step.question, result.claim, cited,
                    state["document"], result.reviewed_at,
                )
        return {}

    graph.add_node("dispatch_step", dispatch_step)
    graph.add_node("retrieve_step", observed_node(retrieve_step, "retrieve"))
    graph.add_node("resolve_step", observed_node(resolve_step, "resolve"))
    graph.add_node("review_step", observed_node(review_step, "review"))
    graph.add_node("step_supplement", step_supplement)
    graph.add_node("summarize_steps", summarize_steps)
    graph.add_node("remember_steps", remember_steps)
    graph.add_edge("retrieve_step", "resolve_step")
    graph.add_edge("resolve_step", "review_step")
    graph.add_edge("remember_steps", END)
