"""Store general analysis requests and clarification replies."""

from alembic import op
import sqlalchemy as sa

revision = "ticket_analysis_001"
down_revision = "contract_investigations_001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("contract_investigations", sa.Column("analysis_request", sa.JSON()))
    op.add_column("contract_investigations", sa.Column("clarification_reply", sa.JSON()))


def downgrade():
    op.drop_column("contract_investigations", "clarification_reply")
    op.drop_column("contract_investigations", "analysis_request")
