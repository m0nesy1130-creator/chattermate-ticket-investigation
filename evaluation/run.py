"""用真实 DeepSeek、BGE 和 MySQL 运行虚构案例。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from statistics import median
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context

from app.checkpoints import checkpoint_serializer, mysql_checkpointer
from app.database_query import query_sources
from app.deepseek_client import DeepSeekClient
from app.directories import load_directory
from app.documents import load_evidence
from app.graphs import build_graph
from app.observability import collect_events
from app.payments import payment_scope
from app.planning import latest_results
from app.tasks import (
    finish_with_current_evidence, initial_state, input_receipt, load_snapshot, pending_kind,
    report_from_state, resume_with_clarification, resume_with_document,
)
from .holdout import memory_case


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
WORKFLOW_IDS = ("C01", "R01", "R02", "M01", "M02", "W01")
PAYMENT_QUESTION = "核对指定合同在 2026 年的年费、已入账付款和剩余未付金额。"
RESUME_QUESTION = "维护年费是多少？服务范围包括哪些项目？请逐项核对。"


def matches(patterns, text):
    return all(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def document_case(case, client, hybrid):
    if "directory" in case:
        root = (FIXTURES / case["directory"]).resolve()
        directory, doc = load_directory(str(root))
        required = {tuple(ref) for ref in case["refs"]}
    else:
        path = (FIXTURES / case["file"]).resolve()
        root, directory, doc = path.parent, None, load_evidence(path)
        required = {(path.name, line) for line in case["lines"]}
    state = build_graph(client).invoke(
        initial_state(case["question"], doc, mode="live", hybrid=hybrid, project=None, directory=directory)
    )
    review, claim = state["review"], state["claim"]
    candidates = {e.evidence_id: (Path(e.source).relative_to(root).as_posix(), e.line) for e in state["candidates"]}
    cited = {candidates[key] for key in claim.evidence_ids if key in candidates} if claim else set()
    locations = set(candidates.values())
    supported = review.status == "supported"
    expected = case["status"]
    answer_ok = review.status == expected
    if expected == "supported":
        answer_ok = answer_ok and bool(claim and matches(case["patterns"], claim.text))
    elif expected == "conflicting":
        answer_ok = answer_ok and matches(case["patterns"], review.note)
    citation_ok = None
    if expected == "supported":
        citation_ok = bool(supported and claim and cited == required and all(key in candidates for key in claim.evidence_ids))
    elif expected == "conflicting":
        cited = {
            location for key, location in candidates.items()
            if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(key)}(?![A-Za-z0-9_-])", review.note)
        }
        citation_ok = answer_ok and required.issubset(cited)
    elif case.get("partial"):
        supported_items = [item for item in review.items if item.status == "supported"]
        verified_ids = {ref for item in supported_items for ref in item.evidence_ids}
        cited = {candidates[ref] for ref in verified_ids if ref in candidates}
        answer_ok = (answer_ok and any(item.status == "insufficient" for item in review.items)
                     and matches(case["patterns"], " ".join(item.answer for item in supported_items)))
        citation_ok = cited == required and verified_ids.issubset(candidates)
    return {
        "status": review.status, "expected_status": expected, "answer_ok": answer_ok,
        "citation_ok": citation_ok, "passed": answer_ok and citation_ok is not False,
        "query": state["plan"].query,
        "review_items": [asdict(item) for item in review.items],
        "retried_gaps": state.get("retried_gaps", False),
        "claim": asdict(claim) if claim else None, "note": review.note,
        "candidate_lines": sorted({line for _, line in locations}),
        "cited_lines": sorted({line for _, line in cited}),
        "candidate_locations": sorted(locations), "cited_locations": sorted(cited),
        "required_lines_retrieved": required.issubset(locations) if required else None,
        "expected_locations": sorted(required), "expected_lines": sorted({line for _, line in required}),
        "expected_patterns": case["patterns"],
    }


def start(graph, config, question, document, *, hybrid=False, **options):
    graph.invoke(initial_state(
        question, document, mode="live", hybrid=hybrid, project=None, **options,
    ), config, durability="sync")
    return load_snapshot(graph, config)


def step_rows(snapshot):
    state = snapshot.values
    latest = latest_results(state.get("step_results", []))
    return [
        {
            "id": step.id, "question": step.question, "depends_on": list(step.depends_on),
            "status": latest[step.id].review.status if step.id in latest else "blocked",
            "claim": latest[step.id].claim.text if step.id in latest and latest[step.id].claim else None,
            "refs": list(latest[step.id].claim.evidence_ids) if step.id in latest and latest[step.id].claim else [],
        }
        for step in state.get("plan_steps", ())
    ]


def fee_step(snapshot):
    latest = latest_results(snapshot.values.get("step_results", []))
    return next((
        item for item in latest.values()
        if item.review.status == "supported" and item.claim
        and matches([r"(?<!\d)12[,]?000(?!\d)"], item.claim.text) and "L4" in item.claim.evidence_ids
    ), None)


def clarification_case(graph, config):
    snap = start(graph, config, "帮我分析一下。", load_evidence(FIXTURES / "fee.txt"), clarify=True, hybrid=True)
    first = pending_kind(snap) == "clarification" and not snap.values["clarification"]["scope"]
    if pending_kind(snap) != "clarification":
        return {"passed": False, "steps": step_rows(snap)}
    resume_with_clarification(
        graph, config, snap, input_receipt(snap), action="answer",
        answer="只核对合同年费是多少，不分析其他条款。", direction=None,
    )
    snap = load_snapshot(graph, config)
    scope = snap.values.get("clarification", {}).get("scope")
    if pending_kind(snap) != "clarification" or not scope:
        return {"passed": False, "initial_follow_up": first, "scope": scope}
    resume_with_clarification(
        graph, config, snap, input_receipt(snap), action="confirm", answer=None, direction=None,
    )
    done = load_snapshot(graph, config)
    claim = done.values.get("claim")
    review = done.values.get("review")
    ok = bool(first and "年费" in scope and not done.next and review and review.status == "supported"
              and claim and matches([r"(?<!\d)12[,]?000(?!\d)"], claim.text) and "L4" in claim.evidence_ids)
    return {
        "passed": ok, "initial_follow_up": first, "scope": scope, "waiting": pending_kind(done),
        "status": review.status if review else None,
        "claim": asdict(claim) if claim else None,
        "report": report_from_state(done.values) if not done.next else None,
    }


def cross_process_resume(thread_id):
    config = {"configurable": {"thread_id": thread_id}}
    with mysql_checkpointer(thread_id) as saver, tracing_context(enabled=False), collect_events() as events:
        graph = build_graph(DeepSeekClient.from_environment(), checkpointer=saver)
        before = load_snapshot(graph, config)
        plan = before.values["plan_steps"]
        original_fee = fee_step(before)
        resume_with_document(
            graph, config, before, input_receipt(before), lambda: load_evidence(FIXTURES / "attachment.txt"),
        )
        done = load_snapshot(graph, config)
        fee = fee_step(done)
        steps = step_rows(done)
        report = report_from_state(done.values) if not done.next else None
        plan_kept = plan == done.values["plan_steps"]
        fee_kept = original_fee is not None and original_fee == fee
        attachment_used = any(step["status"] == "supported" and "S1-L2" in step["refs"]
                              and matches(["巡检", "故障排查"], step["claim"] or "") for step in steps)
        return {
            "passed": bool(report and plan_kept and fee_kept and attachment_used
                           and done.values["review"].status == "supported"),
            "plan_kept": plan_kept, "fee_kept": fee_kept, "attachment_used": attachment_used,
            "steps": steps, "report": report, "resume_trace": events,
        }


def resume_or_partial(case_id, client):
    thread_id = uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    def begin(saver):
        graph = build_graph(client, checkpointer=saver)
        snap = start(graph, config, RESUME_QUESTION, load_evidence(FIXTURES / "fee.txt"),
                     multistep=True, hybrid=True)
        return graph, snap

    if case_id == "R02":
        graph, snap = begin(InMemorySaver(serde=checkpoint_serializer()))
        if pending_kind(snap) != "supplement":
            return {"passed": False, "initial_steps": step_rows(snap), "waiting": pending_kind(snap)}
        finish_with_current_evidence(graph, config, snap, input_receipt(snap))
        done = load_snapshot(graph, config)
        report = report_from_state(done.values) if not done.next else None
        fee = fee_step(snap)
        ok = bool(report and done.values["review"].status == "insufficient" and fee and fee_step(done) == fee
                  and "服务范围" in report and "状态：证据不足" in report and "[L4]" in report)
        return {"passed": ok, "initial_steps": step_rows(snap), "steps": step_rows(done), "report": report}

    with mysql_checkpointer(thread_id) as saver:
        _, snap = begin(saver)
        initial_steps = step_rows(snap)
        if pending_kind(snap) != "supplement":
            return {"passed": False, "initial_steps": initial_steps, "waiting": pending_kind(snap)}
    child = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "evaluation.run", "--resume-child", thread_id],
        cwd=ROOT.parent, capture_output=True, encoding="utf-8", timeout=360,
    )
    if child.returncode:
        return {"passed": False, "initial_steps": initial_steps, "thread_id": thread_id,
                "error": f"续跑子进程退出码 {child.returncode}。", "stderr": child.stderr.strip()}
    return {**json.loads(child.stdout), "initial_steps": initial_steps, "thread_id": thread_id}


def payment_case(graph, config, case_id, source_id):
    if case_id == "M01":
        scope, file = payment_scope(source_id, "MT-2026-001", "2026"), "fee.txt"
    else:
        scope, file = payment_scope(source_id, "NO-MATCH-999", "2026"), "fee_empty.txt"
    snap = start(graph, config, PAYMENT_QUESTION, load_evidence(FIXTURES / file), multistep=True, payment=scope)
    results = latest_results(snap.values.get("step_results", []))
    paid, balance = results.get("s2"), results.get("s3")
    if case_id == "M01":
        ok = bool(not snap.next and snap.values["review"].status == "supported" and paid and balance
                  and paid.money and paid.money.amount == "5000.00"
                  and balance.money and balance.money.amount == "7000.00"
                  and {"P-2026-001", "P-2026-002"}.issubset({e.record for e in paid.evidence}))
    else:
        ok = bool(pending_kind(snap) == "supplement" and paid and paid.review.status == "insufficient"
                  and paid.claim is None and balance is None and any("返回 0 条" in e.text for e in paid.evidence))
        if pending_kind(snap) == "supplement":
            finish_with_current_evidence(graph, config, snap, input_receipt(snap))
            snap = load_snapshot(graph, config)
    return {"passed": ok, "steps": step_rows(snap), "report": report_from_state(snap.values) if not snap.next else None}


def web_case(graph, config, source_id):
    source = query_sources().get(source_id)
    if source is None:
        raise ValueError(f"缺少只读工单查询源 {source_id}。")
    url = "https://docs.python.org/3.12/library/pathlib.html"
    question = (
        "请分别核对三项并各自引用来源。"
        "1. 本地说明中的项目 Python 版本是什么？"
        f"2. 打开 {url} 官方原文，确认 Path.resolve(strict=True) 在路径不存在时抛出什么异常？"
        "3. 查询所选工单表，统计 symptom=path_missing 且 status=open 的工单数。"
    )
    directory, document = load_directory(str(FIXTURES / "local"))
    snap = start(graph, config, question, document, directory=directory, web_enabled=True, query_source=source)
    state = snap.values
    steps = step_rows(snap)
    results = latest_results(state.get("step_results", []))
    verified = set()
    for result in results.values():
        if result.review.status != "supported" or not result.claim:
            continue
        claim = result.claim.text
        for evidence in result.evidence:
            if evidence.evidence_id not in result.claim.evidence_ids:
                continue
            if evidence.source.endswith("tickets.txt") and evidence.line == 2 and re.search(r"\b3\.12\b", claim):
                verified.add("local")
            if evidence.source.startswith(url) and "FileNotFoundError" in evidence.text and "FileNotFoundError" in claim:
                verified.add("web")
            if re.search(r'"count\(\*\)"\s*:\s*2\b', evidence.text) and re.search(r"(?<!\d)2(?!\d)", claim):
                verified.add("mysql")
    ok = bool(not snap.next and state["review"].status == "supported" and len(steps) == 3
              and verified == {"local", "web", "mysql"})
    if pending_kind(snap) == "supplement":
        finish_with_current_evidence(graph, config, snap, input_receipt(snap))
        snap = load_snapshot(graph, config)
    return {"passed": ok, "steps": steps, "verified_sources": sorted(verified),
            "observations": state.get("tool_observations", []),
            "report": report_from_state(snap.values) if not snap.next else None}


def workflow_case(case_id, client, payment_id, query_id):
    if case_id in {"R01", "R02"}:
        return resume_or_partial(case_id, client)
    graph = build_graph(client, checkpointer=InMemorySaver(serde=checkpoint_serializer()))
    config = {"configurable": {"thread_id": uuid4().hex}}
    if case_id == "C01":
        return clarification_case(graph, config)
    if case_id in {"M01", "M02"}:
        return payment_case(graph, config, case_id, payment_id)
    return web_case(graph, config, query_id)


def summarize(rows):
    groups = {}
    for mode in ("keyword", "hybrid", "workflow"):
        group = [row for row in rows if row["mode"] == mode]
        if not group:
            continue
        groups[mode] = {
            "passed": sum(row["passed"] for row in group), "total": len(group),
            "errors": sum("error" in row for row in group),
            "median_seconds": round(median(row["seconds"] for row in group), 3),
        }
        attempts = [attempt for row in group for event in row["trace"] for attempt in event["model_attempts"]]
        groups[mode]["model_attempts"] = len(attempts)
        groups[mode]["automatic_retries"] = sum(attempt["attempt"] > 1 for attempt in attempts)
        for phase in ("plan", "retrieve", "resolve", "review"):
            durations = [
                event["seconds"] for row in group for event in row["trace"] if event["phase"] == phase
            ]
            if durations:
                groups[mode].setdefault("median_node_seconds", {})[phase] = round(median(durations), 3)
        for metric, expected in (("answer_ok", "all"), ("citation_ok", {"supported", "conflicting"}),
                                 ("missing_ok", {"insufficient"}), ("required_lines_retrieved", {"supported", "conflicting"})):
            if mode == "workflow":
                break
            selected = group if expected == "all" else [
                r for r in group if r["expected_status"] in expected or (
                    r.get("partial") and metric in {"citation_ok", "required_lines_retrieved"}
                )
            ]
            if selected:
                field = "answer_ok" if metric == "missing_ok" else metric
                groups[mode][metric] = [sum(row.get(field) is True for row in selected), len(selected)]
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("documents", "workflows", "holdout", "all"), default="all")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--payment-source", default="payment_demo")
    parser.add_argument("--query-source", default="ticket_demo")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume-child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.resume_child:
        print(json.dumps(cross_process_resume(args.resume_child), ensure_ascii=False))
        return

    case_file = "holdout_cases.json" if args.suite == "holdout" else "document_cases.json"
    cases = json.loads((ROOT / case_file).read_text(encoding="utf-8"))
    selected = set(args.case)
    available = {c["id"] for c in cases} if args.suite != "workflows" else set()
    if args.suite == "holdout":
        available.add("H09")
    if args.suite in {"workflows", "all"}:
        available.update(WORKFLOW_IDS)
    if selected - available:
        parser.error("未知或不属于所选套件的用例：" + ", ".join(sorted(selected - available)))
    selected_documents = [case for case in cases if not selected or case["id"] in selected] if args.suite != "workflows" else []
    client = DeepSeekClient.from_environment() if selected_documents or args.suite in {"workflows", "all"} else None
    output = args.output or ROOT.parent / "runtime" / "evaluation" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "model": client.model if client is not None else None, "started_at": datetime.now(timezone.utc).isoformat(),
        "suite": args.suite, "rows": [],
    }
    rows = record["rows"]

    def collect(base, execute):
        began = perf_counter()
        with collect_events() as trace:
            try:
                result = execute()
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                result = {"passed": False, "error": str(error)}
        trace.extend(result.pop("resume_trace", []))
        row = {**base, **result, "seconds": round(perf_counter() - began, 3), "trace": trace}
        rows.append(row)
        record["summary"] = summarize(rows)
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{row['id']} {row['mode']}: {'PASS' if row['passed'] else 'FAIL'} ({row['seconds']:.2f}s)", flush=True)

    with tracing_context(enabled=False):
        if selected_documents:
            from app.retrieval import prepare_embedder

            prepare_embedder()
            for case in selected_documents:
                for hybrid in (False, True):
                    base = {
                        "id": case["id"], "mode": "hybrid" if hybrid else "keyword",
                        "expected_status": case["status"], "question": case["question"],
                        "partial": case.get("partial", False),
                    }
                    collect(base, lambda: document_case(case, client, hybrid))
        if args.suite == "holdout" and (not selected or "H09" in selected):
            collect({"id": "H09", "mode": "workflow"}, memory_case)
        if args.suite in {"workflows", "all"}:
            for case_id in WORKFLOW_IDS:
                if selected and case_id not in selected:
                    continue
                collect({"id": case_id, "mode": "workflow"},
                        lambda: workflow_case(case_id, client, args.payment_source, args.query_source))
    print(output.resolve())
    print(json.dumps(record["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
