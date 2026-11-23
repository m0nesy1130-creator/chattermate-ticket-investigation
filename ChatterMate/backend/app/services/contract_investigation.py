"""Queue contract investigations and reconcile responses by stable run ID."""

import json
import os
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, or_, select

from app.models.contract_investigation import ContractInvestigation


CLAIM_SECONDS = 210
REQUEST_SECONDS = 160
MAX_DELIVERIES = 3
BUSY_STATES = {"queued", "running"}


def bridge_settings():
    try:
        org_id = UUID(os.environ.get("ANALYSIS_AGENT_BRIDGE_ORG_ID", ""))
    except ValueError:
        org_id = None
    return (
        os.environ.get("ANALYSIS_AGENT_BRIDGE_URL", "").strip().rstrip("/"),
        os.environ.get("ANALYSIS_AGENT_BRIDGE_TOKEN", ""),
        org_id,
    )


def report_version(job):
    snapshot = {key: value for key, value in (job.snapshot or {}).items() if key != "receipt"}
    serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(f"{job.id}:{serialized}".encode("utf-8")).hexdigest()


def job_view(job):
    if job is None:
        return None
    snapshot = dict(job.snapshot or {})
    snapshot.pop("receipt", None)
    return {
        "id": job.id,
        "contract_id": job.contract_id,
        "billing_period": job.billing_period,
        "analysis_request": job.analysis_request,
        "status": job.status,
        "error": job.error,
        "snapshot": snapshot,
        "report_version": report_version(job) if job.status == "completed" else None,
        "published_at": job.published_at,
    }


def latest_job(db, ticket):
    return db.scalar(
        select(ContractInvestigation)
        .where(ContractInvestigation.ticket_id == ticket.id, ContractInvestigation.organization_id == ticket.organization_id)
        .order_by(ContractInvestigation.created_at.desc(), ContractInvestigation.id.desc())
        .limit(1)
    )


def reset_delivery(job, operation):
    job.operation = operation
    job.status = "queued"
    job.error = None
    job.attempts = 0
    job.available_at = datetime.now(timezone.utc)


def claim_job(db, org_id=None):
    now = datetime.now(timezone.utc)
    query = (
        select(ContractInvestigation)
        .where(or_(
            and_(ContractInvestigation.status == "queued", ContractInvestigation.available_at <= now),
            and_(ContractInvestigation.status == "running", ContractInvestigation.lease_until <= now),
        ))
        .order_by(ContractInvestigation.available_at, ContractInvestigation.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if org_id is not None:
        query = query.where(ContractInvestigation.organization_id == org_id)
    job = db.scalar(query)
    if job is None:
        return None
    if job.attempts >= MAX_DELIVERIES:
        job.status = "failed"
        job.error = "Worker recovery limit reached. Retry the investigation to check the saved state."
        job.lease_key = job.lease_until = None
        db.commit()
        return None
    job.status = "running"
    job.attempts += 1
    job.lease_key = uuid4()
    job.lease_until = now + timedelta(seconds=CLAIM_SECONDS)
    db.flush()
    db.expunge(job)
    db.commit()
    return job


class BridgeError(Exception):
    def __init__(self, message, *, retry=False, needs_document=False):
        super().__init__(message)
        self.retry = retry
        self.needs_document = needs_document


def _read_result(response, job):
    if response.status_code == 409:
        raise BridgeError("The analysis task is busy. Checking again shortly.", retry=True)
    if response.status_code in {401, 403}:
        raise BridgeError("Analysis service credentials were rejected.")
    if response.status_code >= 500:
        raise BridgeError("The analysis service did not finish the operation. Check its configuration.", retry=True)
    if not response.is_success:
        raise BridgeError("The analysis service rejected this operation or document.")
    try:
        result = response.json()
        if (
            isinstance(result, dict) and result.get("task_id") == job.id.hex
            and result.get("status") in {"incomplete", "awaiting_input", "clarifying", "completed"}
        ):
            return result
    except ValueError:
        pass
    raise BridgeError("The analysis service returned an unexpected response.")


def execute_job(client, job):
    url = f"/investigations/{job.id}"
    current = client.get(url)
    if current.status_code == 404:
        if job.operation in {"supplement", "finish", "clarification"} or job.snapshot:
            raise BridgeError("The saved analysis task was not found. Check the analysis checkpoint database.")
        response = client.post(
            f"/analysis/{job.id}" if job.analysis_request else url,
            data={**job.analysis_request, "organization_id": str(job.organization_id)} if job.analysis_request else {
                "contract_id": job.contract_id, "billing_period": job.billing_period,
            },
            files={"file": (job.filename, job.document, "text/plain")} if job.document else None,
        )
    else:
        snapshot = _read_result(current, job)
        if job.operation == "retry" and snapshot["status"] == "incomplete":
            response = client.post(f"{url}/retry")
        elif job.operation == "clarification" and snapshot["status"] == "clarifying" and (
            snapshot.get("receipt") == (job.clarification_reply or {}).get("receipt")
        ):
            response = client.post(f"{url}/clarification", json=job.clarification_reply)
        elif (
            job.operation in {"supplement", "finish"} and snapshot["status"] == "awaiting_input"
            and snapshot.get("receipt") == job.supplement_receipt
        ):
            files = (
                {"file": (job.supplement_filename, job.supplement_document, "text/plain")}
                if job.operation == "supplement" else None
            )
            response = client.post(
                f"{url}/{job.operation}",
                data={"receipt": job.supplement_receipt},
                files=files,
            )
        else:
            return snapshot
    if response.status_code >= 500 or (response.status_code == 409 and job.operation == "supplement"):
        # A failed graph call can still have durably saved earlier nodes.
        current = client.get(url)
        if current.status_code != 404:
            recovered = _read_result(current, job)
            input_receipt = (job.clarification_reply or {}).get("receipt") if job.operation == "clarification" else job.supplement_receipt
            if job.operation not in {"supplement", "clarification", "finish"} or recovered.get("receipt") != input_receipt:
                return recovered
            if response.status_code == 409:
                raise BridgeError("The document was not applied. Upload a new version.", needs_document=True)
    if response.status_code in {400, 413, 422} and job.operation in {"supplement", "clarification"}:
        if job.operation == "clarification":
            raise BridgeError("The clarification could not be applied. Check the answer or confirm the scope.")
        raise BridgeError("The analysis service rejected this file. Upload a new version.", needs_document=True)
    return _read_result(response, job)


def run_one(session_factory, service_url, token, org_id=None):
    with session_factory() as db:
        job = claim_job(db, org_id)
    if job is None:
        return False
    result = None
    error = None
    try:
        with httpx.Client(
            base_url=service_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(REQUEST_SECONDS, connect=5),
            trust_env=False,
        ) as client:
            result = execute_job(client, job)
    except BridgeError as exc:
        error = exc
    except httpx.RequestError:
        error = BridgeError("The analysis service could not be reached. The original task ID is saved.", retry=True)
    except httpx.InvalidURL:
        error = BridgeError("The analysis service URL is invalid.")

    with session_factory() as db:
        saved = db.scalar(select(ContractInvestigation).where(
            ContractInvestigation.id == job.id, ContractInvestigation.lease_key == job.lease_key,
        ).with_for_update())
        if saved is None:
            return True
        saved.lease_key = saved.lease_until = None
        if result is not None:
            saved.snapshot = result
            saved.status = "failed" if result["status"] == "incomplete" else result["status"]
            saved.error = "An analysis step failed. Retry to continue at that step." if saved.status == "failed" else None
            saved.supplement_document = saved.supplement_filename = saved.supplement_receipt = None
            saved.clarification_reply = None
            if saved.status == "failed":
                saved.operation = "retry"
        else:
            saved.error = str(error)
            if saved.operation == "clarification" and not error.retry and (saved.snapshot or {}).get("status") == "clarifying":
                saved.status = "clarifying"
                saved.clarification_reply = None
            elif error.needs_document:
                saved.status = "awaiting_input"
                saved.supplement_document = saved.supplement_filename = saved.supplement_receipt = None
            else:
                saved.status = "queued" if error.retry and saved.attempts < MAX_DELIVERIES else "failed"
                saved.available_at = datetime.now(timezone.utc) + timedelta(seconds=8)
        db.commit()
    return True
