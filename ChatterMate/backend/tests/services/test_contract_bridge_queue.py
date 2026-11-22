"""Checks the replay behavior of the persistent ChatterMate bridge queue."""

import os
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import Column, MetaData, Table, Uuid, create_engine, update
from sqlalchemy.orm import sessionmaker

from app.models.contract_investigation import ContractInvestigation
from app.services.contract_investigation import claim_job, reset_delivery, run_one


class ContractBridgeQueueTest(TestCase):
    def test_clarification_is_not_resent_after_receipt_advances(self):
        calls = []
        with self.sessions.begin() as db:
            job = db.get(ContractInvestigation, self.task_id)
            job.analysis_request = {"question": "Count open tickets"}
            job.clarification_reply = {"receipt": "first-round", "action": "answer", "answer": "This month"}
            reset_delivery(job, "clarification")

        def transport(request):
            calls.append(request.method)
            return httpx.Response(200, json={
                **self.result("clarifying"), "receipt": "next-round",
            })

        self.run_worker(transport)
        self.assertEqual(calls, ["GET"])
        with self.sessions() as db:
            job = db.get(ContractInvestigation, self.task_id)
            self.assertEqual(job.status, "clarifying")
            self.assertIsNone(job.clarification_reply)

    def setUp(self):
        self.engine = create_engine(os.environ.get("BRIDGE_QUEUE_TEST_DB", "sqlite://"))
        metadata = MetaData()
        self.orgs = Table("organizations", metadata, Column("id", Uuid, primary_key=True))
        self.tickets = Table("tickets", metadata, Column("id", Uuid, primary_key=True))
        self.jobs = ContractInvestigation.__table__.to_metadata(metadata)
        metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(self.engine)
        self.task_id = uuid4()
        org, ticket = uuid4(), uuid4()
        with self.sessions.begin() as db:
            db.execute(self.orgs.insert().values(id=org))
            db.execute(self.tickets.insert().values(id=ticket))
            job = ContractInvestigation(
                id=self.task_id, organization_id=org, ticket_id=ticket, request_key=uuid4(),
                contract_id="MT-2026-001", billing_period="2026", filename="contract.txt", document=b"test",
            )
            reset_delivery(job, "start")
            db.add(job)

    def run_worker(self, transport):
        client = httpx.Client
        with patch(
            "app.services.contract_investigation.httpx.Client",
            side_effect=lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs),
        ):
            self.assertTrue(run_one(self.sessions, "http://localhost", "fixture"))

    def result(self, status):
        return {"task_id": self.task_id.hex, "status": status, "steps": [{"id": "s1", "status": "supported"}]}

    def test_lost_start_response_recovers_completed_task_without_restarting(self):
        calls = []
        completed = False

        def transport(request):
            nonlocal completed
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json=self.result("completed")) if completed else httpx.Response(404)
            completed = True
            raise httpx.ReadTimeout("response lost", request=request)

        self.run_worker(transport)
        with self.sessions.begin() as db:
            job = db.get(ContractInvestigation, self.task_id)
            self.assertEqual(job.status, "queued")
            job.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.run_worker(transport)
        with self.sessions() as db:
            self.assertEqual(db.get(ContractInvestigation, self.task_id).status, "completed")
        self.assertEqual(calls, ["GET", "POST", "GET"])

    def test_resume_uses_retry_endpoint_for_the_original_id(self):
        calls = []

        def transport(request):
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json=self.result(
                "incomplete" if request.method == "GET" else "completed"
            ))

        self.run_worker(transport)
        with self.sessions.begin() as db:
            job = db.get(ContractInvestigation, self.task_id)
            self.assertEqual(job.status, "failed")
            reset_delivery(job, job.operation)
        self.run_worker(transport)
        self.assertEqual(calls[-1], ("POST", f"/investigations/{self.task_id}/retry"))
        with self.sessions() as db:
            self.assertEqual(db.get(ContractInvestigation, self.task_id).status, "completed")

    def test_expired_worker_claim_can_be_taken_over(self):
        with self.sessions() as db:
            original = claim_job(db)
        with self.sessions() as db:
            self.assertIsNone(claim_job(db))
        with self.sessions.begin() as db:
            db.execute(update(ContractInvestigation).values(
                lease_until=datetime.now(timezone.utc) - timedelta(seconds=1),
            ))
        with self.sessions() as db:
            reclaimed = claim_job(db)
        self.assertEqual(reclaimed.id, original.id)
        self.assertNotEqual(reclaimed.lease_key, original.lease_key)
