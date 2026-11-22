"""将复核结果与可定位的证据整理为 Markdown。"""

from .adaptive import ToolObservation
from .contracts import Claim, Evidence, Plan, PlanStep, RejectedDirection, Review, StepResult, TaskRequest
from .directories import DirectorySource
from .evidence import supplement_question
from .payments import PaymentScope


_STATUS_LABELS = {
    "supported": "直接支持",
    "inference": "有依据的推断",
    "insufficient": "证据不足",
    "conflicting": "证据冲突",
}


def _report_header(
    request: TaskRequest, mode: str, original_question: str | None, directory: DirectorySource | None
) -> list[str]:
    mode_label = {
        "offline": "离线演示（关键词检索与原文摘录）",
        "live": "DeepSeek",
    }.get(mode, mode)
    lines = [
        "# 文档分析报告",
        "",
        f"运行模式：{mode_label}",
        "",
        "## 分析问题",
        "",
        original_question if original_question is not None else request.question,
    ]
    if original_question is not None:
        lines.extend(["", "## 已确认分析范围", "", request.question])
    if directory is not None:
        scope = "包含子目录" if directory["recursive"] else "仅当前目录"
        lines.extend(["", "## 本地资料目录", "", directory["path"], "", f"读取 {directory['file_count']} 份文档（{scope}）。"])
        if directory["skipped"]:
            reasons = "、".join(f"{reason} {count} 项" for reason, count in sorted(directory["skipped"].items()))
            lines.append(f"跳过 {reasons}。")
    return lines


def _append_excerpts(lines: list[str], evidence: tuple[Evidence, ...]) -> None:
    lines.extend(["## 证据摘录", ""])
    if not evidence:
        lines.append("当前没有可列出的原文证据。")
    for item in evidence:
        location = f"第 {item.line} 行" if item.line is not None else f"记录 {item.record}"
        origin = item.source
        if item.source.startswith(("https://", "http://")):
            origin = f"{item.title or '公开网页'} [查看原文](<{item.source}>)"
        lines.extend([
            f"- [{item.evidence_id}] {origin}，{location}",
            f"  - SHA-256 `{item.version}`",
        ])
        if item.published_at:
            lines.append(f"  - 发布日期 {item.published_at}")
        if item.retrieved_at:
            lines.append(f"  - 读取日期 {item.retrieved_at}")
        lines.extend(["", *(f"  > {line}" for line in item.text.splitlines()), ""])


def _append_review_items(lines: list[str], review: Review) -> None:
    if len(review.items) < 2:
        return
    lines.extend(["**逐项复核**", ""])
    for item in review.items:
        answer = f"。{item.answer}" if item.answer else ""
        refs = f"（引用：{', '.join(item.evidence_ids)}）" if item.evidence_ids else ""
        lines.append(f"- {item.question} · {_STATUS_LABELS[item.status]}{answer}{refs}")
    lines.append("")


def render_report(
    request: TaskRequest,
    plan: Plan,
    claim: Claim | None,
    review: Review,
    evidence: tuple[Evidence, ...],
    *,
    mode: str,
    hybrid: bool = False,
    project: str | None = None,
    original_question: str | None = None,
    directory: DirectorySource | None = None,
) -> str:
    lines = _report_header(request, mode, original_question, directory)
    lines.extend([
        "",
        "## 查询计划",
        "",
        f"检索词：{plan.query}",
    ])
    if hybrid:
        lines.append("检索方式：关键词 + 本地向量（BGE）")
    if project is not None:
        lines.append(f"记忆项目：{project}")
    lines.extend(["", "## 复核结果", ""])

    lines.extend([f"状态：{_STATUS_LABELS[review.status]}", review.note, ""])
    _append_review_items(lines, review)

    if claim is not None and review.status in {"supported", "inference"}:
        heading = "## 结论" if review.status == "supported" else "## 待验证推断"
        citations = ", ".join(claim.evidence_ids)
        lines.extend([heading, "", claim.text, "", f"引用：{citations}", ""])
        excerpts = tuple(item for item in evidence if item.evidence_id in claim.evidence_ids)
    else:
        excerpts = evidence

    if review.status == "insufficient":
        lines.extend(["## 需要补充的材料", "", supplement_question(
            request.question, review if len(review.items) > 1 else None,
        ), ""])

    _append_excerpts(lines, excerpts)
    return "\n".join(lines).rstrip() + "\n"


def render_step_report(
    request: TaskRequest,
    steps: tuple[PlanStep, ...],
    results: dict[str, StepResult],
    review: Review,
    *,
    mode: str,
    hybrid: bool,
    project: str | None,
    original_question: str | None,
    directory: DirectorySource | None = None,
    payment: PaymentScope | None = None,
    observations: list[ToolObservation] | None = None,
    planning_evidence: tuple[Evidence, ...] = (),
    planning_rejected: tuple[RejectedDirection, ...] = (),
) -> str:
    lines = _report_header(request, mode, original_question, directory)
    if payment is not None:
        source = payment["source"]
        lines.extend([
            "", "## 付款核对范围", "",
            f"- 只读台账 `{source['database']}.{source['table']}`",
            f"- 合同编号 `{payment['contract_id']}`",
            f"- 计费周期 `{payment['billing_period']}`",
        ])
    lines.extend(["", "## 执行计划", ""])
    if hybrid:
        lines.append("检索方式：关键词 + 本地向量（BGE）")
    if project is not None:
        lines.append(f"记忆项目：{project}")
    for step in steps:
        dependency = f"（依赖 {', '.join(step.depends_on)}）" if step.depends_on else ""
        lines.append(f"- `{step.id}` {step.question}{dependency}")
    lines.append(f"- 汇总各步骤结果（依赖 {', '.join(step.id for step in steps)}）")
    if observations:
        lines.extend(["", "## 检索过程", ""])
        for item in observations:
            stage = "规划" if item["phase"] == "plan" else f"执行 {item['step_id']}"
            action = item["action"]
            target = action.get("url") or action.get("query")
            label = f"{action['tool']} · {target}" if target else action["tool"]
            lines.append(f"- {stage} · {label} · {item['result']}")
    lines.extend(["", "## 步骤结果", "", review.note, ""])

    excerpts: dict[str, Evidence] = {}
    rejected = list(planning_rejected)
    for step in steps:
        lines.extend([f"### {step.id} · {step.question}", ""])
        result = results.get(step.id)
        if result is None:
            lines.extend(["状态：依赖尚未获得原文直接支持，步骤未执行。", ""])
            continue
        status = result.review.status
        lines.append(f"状态：{_STATUS_LABELS[status]}")
        if result.attempted_queries:
            lines.append(f"已用检索词：{'、'.join(result.attempted_queries)}")
        lines.extend([result.review.note, ""])
        _append_review_items(lines, result.review)
        if result.claim is not None and status in {"supported", "inference"}:
            text = result.claim.text if status == "supported" else f"待验证推断：{result.claim.text}"
            lines.extend([text, "", f"引用：{', '.join(result.claim.evidence_ids)}", ""])
        elif result.evidence:
            lines.extend([f"候选材料：{', '.join(item.evidence_id for item in result.evidence)}", ""])
        for item in result.evidence:
            excerpts[item.evidence_id] = item
        rejected.extend(result.review.rejected)

    if rejected:
        lines.extend(["## 已排除的分析方向", ""])
        for item in dict.fromkeys(rejected):
            lines.extend([f"- **{item.direction}** · {item.reason}（[{item.evidence_id}]）", f"  > {item.quote}"])
        lines.append("")
        cited = {item.evidence_id for item in rejected}
        for item in planning_evidence:
            if item.evidence_id in cited:
                excerpts[item.evidence_id] = item

    _append_excerpts(lines, tuple(excerpts.values()))
    return "\n".join(lines).rstrip() + "\n"
