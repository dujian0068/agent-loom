"""Keep private draft test snapshots separate from published releases."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_sessions", sa.Column("preview_snapshot", sa.JSON(none_as_null=True), nullable=True))
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("release_id", existing_type=sa.String(32), nullable=True)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM chat_sessions WHERE preview_snapshot IS NOT NULL")):
        raise RuntimeError(
            "Draft previews exist; restore a pre-upgrade backup to downgrade without losing data"
        )
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("release_id", existing_type=sa.String(32), nullable=False)
    op.drop_column("chat_sessions", "preview_snapshot")
