"""供 ChatterMate 调用的合同调查接口，复用本地 Agent 的执行图和检查点。"""

import os
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .api import _save_upload, _task_session, _task_view
from .api import ClarificationReply
from .database_query import query_sources
from .directories import load_directory
from .documents import MAX_DOCUMENT_BYTES
from .payments import payment_scope
from .planning import latest_results
from .tasks import (
    finish_with_current_evidence, initial_state, load_snapshot, pending_kind, resume_with_document, task_graph,
    normalize_project, report_from_state, resume_with_clarification, TaskInputError,
)


def verify_bridge(
    auth: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
) -> None:
    token = os.environ.get("ANALYSIS_AGENT_BRIDGE_TOKEN", "")
    if not token:
        raise HTTPException(503, "合同调查服务未配置共享凭据。")
    if auth is None or not compare_digest(auth.credentials.encode("utf-8"), token.encode("utf-8")):
        raise HTTPException(401, "合同调查服务认证失败。")


app = FastAPI(title="Contract investigation bridge", dependencies=[Depends(verify_bridge)], docs_url=None, redoc_url=None)


def bridge_snapshot(graph, config):
    snapshot = load_snapshot(graph, config)
    if not snapshot.values.get("bridge_fingerprint"):
        raise HTTPException(404, "找不到此合同调查。")
    return snapshot


def bridge_view(run_id: UUID, snapshot):
    task = _task_view(run_id.hex, snapshot)
    state = snapshot.values
    results = latest_results(state.get("step_results", []))
    evidence = {
        item.evidence_id: {
            "id": item.evidence_id,
            "source": Path(item.source).name if item.line is not None else item.source,
            "line": item.line,
            "record": item.record,
            "text": item.text[:1000],
        }
        for result in results.values()
        for item in result.evidence
    }
    balance_step = next((step for step in state.get("plan_steps", ()) if step.kind == "balance"), None)
    balance = results.get(balance_step.id) if balance_step else None
    return {
        "task_id": task.task_id,
        "status": task.status,
        "review_status": task.review_status,
        "prompt": task.prompt,
        "receipt": task.receipt,
        "clarification": task.clarification,
        "confirmed_scope": task.confirmed_scope,
        "can_finish": task.can_finish,
        "document_revision": state.get("document_revision", 0),
        "steps": [
            {
                **step.model_dump(mode="json"),
                "material_revision": result.revision if (result := results.get(step.id)) else None,
                "review_note": result.review.note if result else None,
                "previous_claim": state.get("prior_step_claims", {}).get(step.id),
            }
            for step in task.steps if step.id != "summary"
        ],
        "evidence": list(evidence.values()),
        "active_step": state.get("active_step") if snapshot.next else None,
        "next_nodes": list(snapshot.next) if not snapshot.interrupts else [],
        "balance": asdict(balance.money) if balance is not None and balance.money is not None else None,
    }


@app.post("/analysis/{run_id}")
def start_analysis(
    run_id: UUID,
    question: Annotated[str, Form(min_length=1, max_length=4000)],
    web_enabled: Annotated[bool, Form()] = False,
    configured_sources: Annotated[bool, Form()] = False,
    hybrid: Annotated[bool, Form()] = False,
    project: Annotated[str | None, Form(max_length=128)] = None,
    organization_id: Annotated[UUID | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
):
    question = question.strip()
    if not question:
        raise HTTPException(422, "问题不能为空。")
    raw = file.file.read(MAX_DOCUMENT_BYTES + 1) if file else b""
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "文件大小超过 2 MiB。")
    if file:
        file.file.seek(0)
    project_key = None
    if project is not None:
        try:
            project = normalize_project(project)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if organization_id is None:
            raise HTTPException(422, "项目记忆需要组织标识。")
        project_key = "chattermate:" + sha256(f"{organization_id.hex}\0{project}".encode()).hexdigest()
    # Keep the original fingerprint for investigations queued by older workers.
    options = f"{hybrid}\0{project_key or ''}\0" if hybrid or project_key else ""
    fingerprint = sha256(f"analysis\0{question}\0{web_enabled}\0{configured_sources}\0{options}".encode() + raw).hexdigest()
    with _task_session(run_id.hex) as (saver, config):
        reader = task_graph("offline", saver)
        existing = reader.get_state(config)
        if "request" in existing.values:
            if existing.values.get("bridge_fingerprint") != fingerprint:
                raise HTTPException(409, "此调查编号已绑定不同的请求。")
            return bridge_view(run_id, existing)
        directory_path = os.environ.get("ANALYSIS_AGENT_BRIDGE_DIRECTORY") if configured_sources else None
        source_id = os.environ.get("ANALYSIS_AGENT_BRIDGE_QUERY_SOURCE") if configured_sources else None
        directory, evidence = load_directory(directory_path, recursive=False) if directory_path else (None, ())
        source = query_sources().get(source_id) if source_id else None
        if source_id and source is None:
            raise HTTPException(503, "未找到工作台配置的只读查询源。")
        document = _save_upload(run_id.hex, file) if file else ()
        state = initial_state(
            question, (*document, *evidence), mode="live", hybrid=hybrid, project=project_key,
            clarify=True, multistep=True, adaptive=True, web_enabled=web_enabled,
            directory=directory, query_source=source,
        )
        state["bridge_fingerprint"] = fingerprint
        task_graph("live", saver).invoke(state, config, durability="sync")
        return bridge_view(run_id, bridge_snapshot(reader, config))


@app.post("/investigations/{run_id}/clarification")
def clarify_investigation(run_id: UUID, reply: ClarificationReply):
    with _task_session(run_id.hex) as (saver, config):
        reader = task_graph("offline", saver)
        snapshot = bridge_snapshot(reader, config)
        if pending_kind(snapshot) == "clarification" and bridge_view(run_id, snapshot)["receipt"] == reply.receipt:
            try:
                resume_with_clarification(
                    task_graph("live", saver), config, snapshot, reply.receipt,
                    action=reply.action, answer=reply.answer, direction=reply.direction,
                )
            except TaskInputError as error:
                raise HTTPException(422, str(error)) from error
            snapshot = bridge_snapshot(reader, config)
        return bridge_view(run_id, snapshot)


@app.post("/investigations/{run_id}")
def start_or_read(
    run_id: UUID,
    contract_id: Annotated[str, Form(min_length=1, max_length=40)],
    billing_period: Annotated[str, Form(min_length=1, max_length=40)],
    file: Annotated[UploadFile, File()],
):
    source_id = os.environ.get("ANALYSIS_AGENT_BRIDGE_SOURCE", "").strip()
    if not source_id:
        raise HTTPException(503, "合同调查服务未指定只读付款台账。")
    raw = file.file.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "文件大小超过 2 MiB。")
    file.file.seek(0)
    fingerprint = sha256(
        b"\0".join(value.encode() for value in (source_id, contract_id, billing_period)) + b"\0" + raw
    ).hexdigest()
    with _task_session(run_id.hex) as (saver, config):
        reader = task_graph("offline", saver)
        existing = reader.get_state(config)
        if "request" in existing.values:
            if existing.values.get("bridge_fingerprint") != fingerprint:
                raise HTTPException(409, "此调查编号已绑定一份不同的合同资料。")
            return bridge_view(run_id, existing)
        scope = payment_scope(source_id, contract_id, billing_period)
        graph = task_graph("live", saver)
        state = initial_state(
            "核对合同年费、已入账付款及未付余额。",
            _save_upload(run_id.hex, file), mode="live", hybrid=False, project=None,
            multistep=True, payment=scope,
        )
        state["bridge_fingerprint"] = fingerprint
        graph.invoke(state, config, durability="sync")
        return bridge_view(run_id, bridge_snapshot(reader, config))


@app.get("/investigations/{run_id}")
def get_investigation(run_id: UUID):
    with _task_session(run_id.hex) as (saver, config):
        return bridge_view(run_id, bridge_snapshot(task_graph("offline", saver), config))


@app.get("/investigations/{run_id}/report")
def investigation_report(run_id: UUID):
    with _task_session(run_id.hex) as (saver, config):
        snapshot = bridge_snapshot(task_graph("offline", saver), config)
        if snapshot.interrupts or snapshot.next:
            raise HTTPException(409, "调查完成后才能下载报告。")
        return Response(report_from_state(snapshot.values), media_type="text/markdown")


@app.post("/investigations/{run_id}/retry")
def retry_investigation(run_id: UUID):
    with _task_session(run_id.hex) as (saver, config):
        reader = task_graph("offline", saver)
        snapshot = bridge_snapshot(reader, config)
        if snapshot.next and not snapshot.interrupts:
            task_graph("live", saver).invoke(None, config, durability="sync")
            snapshot = bridge_snapshot(reader, config)
        return bridge_view(run_id, snapshot)


@app.post("/investigations/{run_id}/supplement")
def supplement_investigation(
    run_id: UUID,
    receipt: Annotated[str, Form(min_length=1, max_length=160)],
    file: Annotated[UploadFile, File()],
):
    with _task_session(run_id.hex) as (saver, config):
        reader = task_graph("offline", saver)
        snapshot = bridge_snapshot(reader, config)
        if pending_kind(snapshot) != "supplement":
            return bridge_view(run_id, snapshot)
        resume_with_document(
            task_graph("live", saver), config, snapshot, receipt, lambda: _save_upload(run_id.hex, file),
        )
        return bridge_view(run_id, bridge_snapshot(reader, config))


@app.post("/investigations/{run_id}/finish")
def finish_investigation(run_id: UUID, receipt: Annotated[str, Form(min_length=1, max_length=160)]):
    with _task_session(run_id.hex) as (saver, config):
        reader = task_graph("offline", saver)
        snapshot = bridge_snapshot(reader, config)
        if pending_kind(snapshot) == "supplement":
            finish_with_current_evidence(task_graph("live", saver), config, snapshot, receipt)
            snapshot = bridge_snapshot(reader, config)
        return bridge_view(run_id, snapshot)
