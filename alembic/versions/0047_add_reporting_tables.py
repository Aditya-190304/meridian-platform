"""Add reporting tables: kpi_snapshots and dashboard_widgets.

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-10 14:32:07.841250
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # kpi_snapshots — stores periodic dashboard snapshots per tenant
    # ------------------------------------------------------------------
    op.create_table(
        "kpi_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column(
            "snapshot_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    # Note: no index on (tenant_id, created_at) — the ORDER BY created_at DESC
    # query in get_latest_snapshot will do a full table scan as the table grows.

    # ------------------------------------------------------------------
    # dashboard_widgets — user-configured chart widgets per tenant/user
    # ------------------------------------------------------------------
    op.create_table(
        "dashboard_widgets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("widget_type", sa.String(length=64), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("position_x", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("position_y", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("width", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    # Note: no index on (tenant_id, user_id) — dashboard load queries that filter
    # by both columns will scan the full table once widget count is large.

    # ------------------------------------------------------------------
    # Extend tasks table with columns needed for KPI queries
    # (assumes tasks table was created in an earlier migration)
    # ------------------------------------------------------------------
    op.add_column(
        "tasks",
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("story_points", sa.Integer(), nullable=True),
    )
    # Note: no index on tasks.completed_at — velocity queries that filter
    # by (tenant_id, team_id, status, completed_at) will read the whole
    # tasks partition for a tenant.
    #
    # Note: no index on tasks.due_date — the overdue query that filters
    # Task.due_date < today will also scan without an index benefit.
    #
    # A composite index on (tenant_id, status, due_date) and another on
    # (tenant_id, team_id, status, completed_at) would be appropriate here
    # but are deferred to a follow-up performance pass.


def downgrade() -> None:
    op.drop_column("tasks", "story_points")
    op.drop_column("tasks", "completed_at")
    op.drop_table("dashboard_widgets")
    op.drop_table("kpi_snapshots")
