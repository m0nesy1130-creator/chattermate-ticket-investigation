"""Contract investigation controls in the authenticated ticket workspace."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import PurePath
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.tickets import SETTLED_STATUSES, _get_ticket_or_404, approve_actions, manage_tickets, view_tickets
from app.database import get_db
from app.models.contract_investigation import ContractInvestigation
from app.models.ticket import TicketStatus
from app.models.user import User
from app.services.contract_investigation import BUSY_STATES, bridge_settings, job_view, latest_job, reset_delivery
from app.services.ticket import TicketService
from app.services.ticket_access import check_ticketing_access
from app.services.ticket_events import emit_ticket_update


router = APIRouter()
MAX_BYTES = 2 * 1024 * 1024
SCOPE_FIELD = Annotated[str, Form(pattern=r"^[A-Za-z0-9._/-]{1,40}$")]


def enabled_for(org_id):
    url, token, configured_org = bridge_settings()
    return bool(url and token and configured_org == org_id)


def require_integration(org_id):
    if not enabled_for(org_id):
        raise HTTPException(503, "Contract investigations are disabled.")


def ticket_for(db, ticket_id, user, *, lock=False):
    check_ticketing_access(db, user.organization_id)
    return _get_ticket_or_404(db, ticket_id, user, for_update=lock)


def current_for(db, ticket_id, run_id, user):
    ticket = ticket_for(db, ticket_id, user, lock=True)
    current = latest_job(db, ticket)
    if current is None or current.id != run_id:
        raise HTTPException(404, "The current investigation was not found.")
    job = db.scalar(
        select(ContractInvestigation).where(ContractInvestigation.id == run_id)
        .execution_options(populate_existing=True).with_for_update()
    )
    return ticket, job


async def read_document(file):
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if len(filename) > 120 or PurePath(filename).suffix.lower() not in {".txt", ".md"}:
        raise HTTPException(422, "Upload a UTF-8 TXT or Markdown file.")
    raw = await file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "The file exceeds 2 MiB.")
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "The file is not valid UTF-8.") from exc
    if not decoded.strip() or "\0" in decoded:
        raise HTTPException(422, "The file must contain readable text.")
    return filename, raw


@router.get("/{ticket_id}/contract-investigation")
async def get_contract_investigation(
    ticket_id: UUID, user: User = Depends(view_tickets), db: Session = Depends(get_db),
):
    ticket = ticket_for(db, ticket_id, user)
    return {"enabled": enabled_for(user.organization_id), "job": job_view(latest_job(db, ticket))}


@router.post("/{ticket_id}/contract-investigation/analysis", status_code=202)
async def start_ticket_analysis(
    ticket_id: UUID, request_key: Annotated[UUID, Form()],
    question: Annotated[str, Form(min_length=1, max_length=4000)],
    file: Annotated[UploadFile | None, File()] = None,
    web_enabled: Annotated[bool, Form()] = False,
    configured_sources: Annotated[bool, Form()] = False,
    user: User = Depends(manage_tickets), db: Session = Depends(get_db),
):
    require_integration(user.organization_id)
    question = question.strip()
    if not question:
        raise HTTPException(422, "请输入分析需求。")
    filename, raw = await read_document(file) if file else ("", b"")
    ticket = ticket_for(db, ticket_id, user, lock=True)
    request = {"question": question, "web_enabled": web_enabled, "configured_sources": configured_sources}
    previous = db.scalar(select(ContractInvestigation).where(
        ContractInvestigation.ticket_id == ticket.id, ContractInvestigation.request_key == request_key,
    ))
    if previous is not None:
        if (previous.analysis_request, previous.filename, previous.document) != (request, filename, raw):
            raise HTTPException(409, "此请求编号已绑定不同的分析输入。")
        return job_view(previous)
    current = latest_job(db, ticket)
    if str(ticket.status) in SETTLED_STATUSES or (current is not None and current.status != "completed"):
        raise HTTPException(409, "请先继续当前调查，或重新打开已办结的工单。")
    job = ContractInvestigation(
        ticket_id=ticket.id, organization_id=ticket.organization_id, request_key=request_key,
        contract_id="", billing_period="", filename=filename, document=raw, analysis_request=request,
        created_at=datetime.now(timezone.utc),
    )
    reset_delivery(job, "start")
    db.add(job)
    db.commit()
    return job_view(job)


@router.post("/{ticket_id}/contract-investigation/{run_id}/clarification", status_code=202)
async def clarify_ticket_analysis(
    ticket_id: UUID, run_id: UUID,
    answer: Annotated[str, Form(max_length=4000)] = "",
    confirm: Annotated[bool, Form()] = False,
    user: User = Depends(manage_tickets), db: Session = Depends(get_db),
):
    require_integration(user.organization_id)
    _, job = current_for(db, ticket_id, run_id, user)
    snapshot = job.snapshot or {}
    answer = answer.strip()
    if job.status != "clarifying" or not snapshot.get("receipt"):
        raise HTTPException(409, "当前任务没有待回答的追问。")
    if confirm and not (snapshot.get("clarification") or {}).get("scope"):
        raise HTTPException(422, "请先回答追问，等待分析范围生成。")
    if not confirm and not answer:
        raise HTTPException(422, "请输入补充信息。")
    job.clarification_reply = {
        "receipt": snapshot["receipt"], "action": "confirm" if confirm else "answer",
        "answer": None if confirm else answer,
    }
    reset_delivery(job, "clarification")
    db.commit()
    return job_view(job)


@router.post("/{ticket_id}/contract-investigation", status_code=202)
async def start_contract_investigation(
    ticket_id: UUID, request_key: Annotated[UUID, Form()],
    contract_id: SCOPE_FIELD, billing_period: SCOPE_FIELD,
    file: Annotated[UploadFile, File()],
    user: User = Depends(manage_tickets), db: Session = Depends(get_db),
):
    require_integration(user.organization_id)
    filename, raw = await read_document(file)
    ticket = ticket_for(db, ticket_id, user, lock=True)
    existing = db.scalar(select(ContractInvestigation).where(
        ContractInvestigation.ticket_id == ticket.id, ContractInvestigation.request_key == request_key,
    ))
    if existing is not None:
        if (existing.contract_id, existing.billing_period, existing.filename, existing.document) != (
            contract_id, billing_period, filename, raw,
        ):
            raise HTTPException(409, "This request key is already bound to another document.")
        return job_view(existing)
    if str(ticket.status) in SETTLED_STATUSES:
        raise HTTPException(409, "Reopen the ticket before starting a new investigation.")
    current = latest_job(db, ticket)
    if current is not None and current.status != "completed":
        raise HTTPException(409, "Continue the current investigation first.")
    job = ContractInvestigation(
        ticket_id=ticket.id, organization_id=ticket.organization_id, request_key=request_key,
        contract_id=contract_id, billing_period=billing_period,
        filename=filename, document=raw, created_at=datetime.now(timezone.utc),
    )
    reset_delivery(job, "start")
    db.add(job)
    db.commit()
    return job_view(job)


@router.post("/{ticket_id}/contract-investigation/{run_id}/retry", status_code=202)
async def retry_contract_investigation(
    ticket_id: UUID, run_id: UUID, user: User = Depends(manage_tickets), db: Session = Depends(get_db),
):
    require_integration(user.organization_id)
    _, job = current_for(db, ticket_id, run_id, user)
    if job.status == "failed":
        reset_delivery(job, job.operation)
        db.commit()
    return job_view(job)


@router.post("/{ticket_id}/contract-investigation/{run_id}/supplement", status_code=202)
async def supplement_contract_investigation(
    ticket_id: UUID, run_id: UUID, file: Annotated[UploadFile, File()],
    user: User = Depends(manage_tickets), db: Session = Depends(get_db),
):
    require_integration(user.organization_id)
    filename, raw = await read_document(file)
    _, job = current_for(db, ticket_id, run_id, user)
    if job.status in BUSY_STATES and job.operation == "supplement" and job.supplement_document == raw:
        return job_view(job)
    receipt = (job.snapshot or {}).get("receipt")
    if job.status != "awaiting_input" or not receipt:
        raise HTTPException(409, "This investigation is not waiting for a document.")
    if raw == job.document:
        raise HTTPException(422, "This document is already in the investigation.")
    job.supplement_filename = filename
    job.supplement_document = raw
    job.supplement_receipt = receipt
    reset_delivery(job, "supplement")
    db.commit()
    return job_view(job)


@router.post("/{ticket_id}/contract-investigation/{run_id}/finish", status_code=202)
async def finish_contract_investigation(
    ticket_id: UUID, run_id: UUID, user: User = Depends(manage_tickets), db: Session = Depends(get_db),
):
    require_integration(user.organization_id)
    _, job = current_for(db, ticket_id, run_id, user)
    if job.status in BUSY_STATES and job.operation == "finish":
        return job_view(job)
    receipt = (job.snapshot or {}).get("receipt")
    if job.status != "awaiting_input" or not receipt:
        raise HTTPException(409, "This investigation is not waiting for a document.")
    job.supplement_receipt = receipt
    reset_delivery(job, "finish")
    db.commit()
    return job_view(job)


@router.post("/{ticket_id}/contract-investigation/{run_id}/publish")
async def publish_contract_investigation(
    ticket_id: UUID, run_id: UUID, user: User = Depends(approve_actions), db: Session = Depends(get_db),
):
    ticket, job = current_for(db, ticket_id, run_id, user)
    if job.published_at is not None:
        return job_view(job)
    snapshot = job.snapshot or {}
    balance = snapshot.get("balance")
    if job.status != "completed" or (not job.analysis_request and (
        snapshot.get("review_status") != "supported" or not balance
    )):
        raise HTTPException(409, "请先完成调查并复核结果。")
    if job.analysis_request:
        content = [
            f"分析需求 · {job.analysis_request['question']}",
            f"分析范围 · {snapshot.get('confirmed_scope') or '未确认'}",
            f"调查编号 {job.id}",
        ]
    else:
        content = [
            f"合同付款核对 · {job.contract_id} / {job.billing_period}",
            f"未付余额 {balance['currency']} {balance['amount']}",
            f"调查编号 {job.id}",
        ]
    for step in snapshot.get("steps", ()):
        if step.get("claim"):
            refs = ", ".join(step.get("references", ()))
            content.append(f"{step['claim']}（{refs}）")
        elif job.analysis_request:
            content.append(f"{step['question']} · {step['status']}")
    service = TicketService(db)
    service.add_comment(ticket, "\n".join(content), is_internal=True, actor_user_id=user.id)
    if not job.analysis_request and Decimal(balance["amount"]) > 0 and str(ticket.status) in {TicketStatus.OPEN, TicketStatus.REOPENED}:
        service.transition_status(ticket, TicketStatus.IN_PROGRESS, actor_user_id=user.id, reason="Verified outstanding balance")
    job.published_at = datetime.now(timezone.utc)
    db.commit()
    await emit_ticket_update(user.organization_id, ticket.id, "comment")
    return job_view(job)
