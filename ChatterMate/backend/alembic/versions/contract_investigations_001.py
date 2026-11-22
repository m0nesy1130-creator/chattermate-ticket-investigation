"""Durable jobs for the ChatterMate contract investigation panel."""

from alembic import op
import sqlalchemy as sa


revision = "contract_investigations_001"
down_revision = "note_agent_author_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "contract_investigations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_key", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.String(40), nullable=False),
        sa.Column("billing_period", sa.String(40), nullable=False),
        sa.Column("filename", sa.String(120), nullable=False),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.Column("supplement_filename", sa.String(120)),
        sa.Column("supplement_document", sa.LargeBinary()),
        sa.Column("supplement_receipt", sa.String(160)),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("snapshot", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_key", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("ticket_id", "request_key", name="uq_contract_investigation_request"),
    )
    op.create_index("ix_contract_investigation_queue", "contract_investigations", ["status", "available_at"])
    op.create_index("ix_contract_investigation_ticket", "contract_investigations", ["ticket_id", "created_at"])


def downgrade():
    op.drop_table("contract_investigations")
