"""生成并验证可以由当前文档检索执行的计划。"""

import re
from dataclasses import replace
from graphlib import CycleError, TopologicalSorter
from unicodedata import normalize

from .adaptive import REJECTION_INSTRUCTIONS, allowed_tools, describe_source, evidence_rows, validate_rejections
from .contracts import PlanStep, RejectedDirection, StepResult
from .deepseek_client import DeepSeekClient


_PLAN_SCOPE = (
    '覆盖已确认需求中的每个独立交付项，每项只生成一步。'
    'target 必须逐字摘录已确认问题或用户补充回答中的最短连续片段，明确该步要交付的事实。'
    '同一交付项只用一个 target。不要单列逐项核对、确认已列出的内容、取证动作和汇总。'
    '例如「培训地点在哪里？报到时间是几点？请分别核对。」只生成两步，'
    'target 分别为「培训地点」及「报到时间」，均无前置依赖。'
    '只有回答后一个明确交付项确实需要前项答案时，才设置依赖。'
)


def make_plan(question: str, client: DeepSeekClient) -> tuple[PlanStep, ...]:
    steps, _ = _generate_plan(
        client,
        '根据已确认的分析需求生成文档分析计划。每个步骤只问一个能靠文档原文或依赖结果核实的问题。'
        '为每步给出 1 至 3 个短检索词，覆盖不同可能的原文表述。只规划用户要求的内容，'
        '范围明确且只含一个问题时可只用一步。请勿输出结论或调用外部工具。'
        '返回 JSON，形如 {"steps":[{"id":"s1","question":"每年收费多少？","target":"收费",'
        '"depends_on":[],"queries":["维护费","每年收费"]}]}。最多 6 个步骤，ID 使用 s1、s2 等。'
        + _PLAN_SCOPE,
        {"question": question},
    )
    return steps


def make_adaptive_plan(question: str, state, client: DeepSeekClient) -> tuple[tuple[PlanStep, ...], tuple[RejectedDirection, ...]]:
    evidence = state.get("planning_evidence", ())
    tools = allowed_tools(state) or ["document"]
    steps, result = _generate_plan(
        client,
        '将已确认需求拆成 1 至 6 个可核对步骤，规划前的观察仅为证据线索。'
        '各步骤保留完整、单独可回答的问题，使用 s1、s2 等 ID，只在需要前项结果时设置 depends_on。'
        '同时需要本地、公开网页和数据库时分别设置可核对的步骤，保留实际查询条件。'
        '搜索与打开网页属于回答同一事实问题的动作，合并为一个步骤。'
        '不要单列搜索线索、读取文件、查看结构、排除候选说法等步骤，直接回答用户提出的事实问题。'
        '每步给出首选 tool 和 1 至 3 个关键词 queries。tool 必须来自 allowed_tools。汇总由程序处理。'
        '只输出 JSON，例如 {"steps":[{"id":"s1","question":"哪种异常被抛出？","target":"异常", "depends_on":[],'
        '"tool":"web","queries":["Python pathlib resolve strict"]}],"rejected":[]}。'
        + _PLAN_SCOPE + REJECTION_INSTRUCTIONS,
        {"question": question, "allowed_tools": tools, "database": describe_source(state.get("query_source")),
         "observations": state.get("tool_observations", []), "evidence": evidence_rows(evidence),
         "turns": state.get("clarification_turns", [])},
    )
    raw = {row["id"]: row for row in result["steps"]}
    validated = []
    for step in steps:
        preferred = raw[step.id].get("tool", tools[0])
        if preferred not in tools:
            raise ValueError(f"步骤 {step.id} 选择了未启用的来源。")
        validated.append(replace(step, kind="adaptive", preferred_tool=preferred))
    return tuple(validated), validate_rejections(result.get("rejected"), evidence)


def _generate_plan(client, instructions, data):
    scope = [data["question"], *(turn["answer"] for turn in data.get("turns", ()) if turn.get("answer"))]
    result = client.complete_json(instructions, data)
    try:
        return _scope_plan(result.get("steps"), scope), result
    except ValueError as error:
        corrected = client.complete_json(instructions, {**data, "validation_error": str(error)})
        return _scope_plan(corrected.get("steps"), scope), corrected


def _scope_key(text: str) -> str:
    return re.sub(r"[\s？?，,。；;]+", "", normalize("NFKC", text)).casefold()


def _scope_plan(raw_steps: object, scope: list[str]) -> tuple[PlanStep, ...]:
    steps = validate_plan(raw_steps)
    scope_keys = tuple(_scope_key(part) for part in scope)
    kept = {}
    aliases = {}
    for step, row in zip(steps, raw_steps):
        target = row.get("target")
        key = _scope_key(target) if isinstance(target, str) else ""
        if not key or not any(key in part for part in scope_keys):
            raise ValueError(f"步骤 {step.id} 的 target 须摘自用户确认的需求范围。")
        # 同一交付项的后续重述不进入检查点。其他步骤依赖映射到保留项。
        representative = kept.setdefault(key, step)
        aliases[step.id] = representative.id
    result = tuple(
        replace(step, depends_on=tuple(dict.fromkeys(
            aliases[dep] for dep in step.depends_on if aliases[dep] != step.id
        )))
        for step in kept.values()
    )
    _validate_dependencies(result)
    return result


def validate_plan(raw_steps: object) -> tuple[PlanStep, ...]:
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 6:
        raise ValueError("计划须包含 1 至 6 个文档分析步骤。")

    steps = []
    for row in raw_steps:
        if not isinstance(row, dict):
            raise ValueError("分析步骤格式无效。")
        step_id, question = row.get("id"), row.get("question")
        dependencies, queries = row.get("depends_on"), row.get("queries")
        if not isinstance(step_id, str) or not re.fullmatch(r"s[1-9][0-9]*", step_id):
            raise ValueError("分析步骤的 ID 格式无效。")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"分析步骤 {step_id} 缺少问题。")
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            raise ValueError(f"分析步骤 {step_id} 的依赖格式无效。")
        if not isinstance(queries, list) or any(not isinstance(query, str) for query in queries):
            raise ValueError(f"分析步骤 {step_id} 的检索词格式无效。")
        keywords = tuple(dict.fromkeys(query.strip() for query in queries if query.strip()))
        if not 1 <= len(keywords) <= 3:
            raise ValueError(f"分析步骤 {step_id} 须包含 1 至 3 个检索词。")
        steps.append(PlanStep(step_id, question.strip(), tuple(dict.fromkeys(dependencies)), keywords))

    _validate_dependencies(steps)
    return tuple(steps)


def _validate_dependencies(steps: list[PlanStep] | tuple[PlanStep, ...]) -> None:
    ids = {step.id for step in steps}
    if len(ids) != len(steps):
        raise ValueError("计划包含重复的步骤 ID。")
    if any(dep not in ids for step in steps for dep in step.depends_on):
        raise ValueError("计划引用了不存在的依赖步骤。")
    try:
        tuple(TopologicalSorter({step.id: step.depends_on for step in steps}).static_order())
    except CycleError:
        raise ValueError("计划包含循环依赖。") from None


def latest_results(results: list[StepResult]) -> dict[str, StepResult]:
    return {result.step_id: result for result in results}
