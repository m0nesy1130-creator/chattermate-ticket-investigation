"""Durable ChatterMate jobs for contract payment investigations."""

from uuid import uuid4

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, LargeBinary, String
from sqlalchemy import Text, UniqueConstraint, Uuid, func

from app.database import Base


class ContractInvestigation(Base):
    __tablename__ = "contract_investigations"
    __table_args__ = (
        UniqueConstraint("ticket_id", "request_key", name="uq_contract_investigation_request"),
        Index("ix_contract_investigation_queue", "status", "available_at"),
        Index("ix_contract_investigation_ticket", "ticket_id", "created_at"),
    )

    id = Column(Uuid, primary_key=True, default=uuid4)
    ticket_id = Column(Uuid, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    request_key = Column(Uuid, nullable=False)
    contract_id = Column(String(40), nullable=False)
    billing_period = Column(String(40), nullable=False)
    filename = Column(String(120), nullable=False)
    document = Column(LargeBinary, nullable=False)
    supplement_filename = Column(String(120))
    supplement_document = Column(LargeBinary)
    supplement_receipt = Column(String(160))
    operation = Column(String(16), nullable=False)
    status = Column(String(24), nullable=False)
    snapshot = Column(JSON)
    analysis_request = Column(JSON)
    clarification_reply = Column(JSON)
    error = Column(Text)
    attempts = Column(Integer, nullable=False, default=0)
    available_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_until = Column(DateTime(timezone=True))
    lease_key = Column(Uuid)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    published_at = Column(DateTime(timezone=True))
