"""Add an implemented exec version; retain all existing version snapshots."""

import json
import time
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    versions = sa.table(
        "resource_versions",
        sa.column("id", sa.String),
        sa.column("resource_id", sa.String),
        sa.column("version", sa.Integer),
        sa.column("status", sa.String),
        sa.column("spec", sa.JSON),
        sa.column("created_at", sa.Float),
    )
    rows = connection.execute(sa.select(versions)).mappings().all()
    latest = {}
    for row in rows:
        spec = row["spec"]
        if isinstance(spec, str):
            spec = json.loads(spec)
        if spec.get("registry_key") == "exec":
            previous = latest.get(row["resource_id"])
            if not previous or previous["version"] < row["version"]:
                latest[row["resource_id"]] = row
    for row in latest.values():
        if row["status"] == "unavailable":
            connection.execute(
                versions.insert().values(
                    id=uuid.uuid4().hex,
                    resource_id=row["resource_id"],
                    version=row["version"] + 1,
                    status="ready",
                    spec={"registry_key": "exec", "category": "execution", "writes": True},
                    created_at=time.time(),
                )
            )
            connection.execute(
                sa.text("UPDATE resources SET description=:description WHERE id=:id"),
                {"description": "在应用工作空间运行本机命令；需启用本机执行", "id": row["resource_id"]},
            )


def downgrade():
    raise RuntimeError(
        "Restore a pre-upgrade backup; exec versions may be referenced by published applications"
    )
