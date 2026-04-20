"""alerting tables

Revision ID: 001
Revises: 000
Create Date: 2026-04-18 17:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = "001"
down_revision = "000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("instance_name", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("operator", sa.String(length=4), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("notify_ui", sa.Boolean(), nullable=False),
        sa.Column("notify_email", sa.Boolean(), nullable=False),
        sa.Column("email_address", sa.String(length=255), nullable=True),
        sa.Column("notify_webhook", sa.Boolean(), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(op.f("ix_alert_rules_user_id"), "alert_rules", ["user_id"], unique=False)

    op.create_table(
        "alert_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("rule_name", sa.String(length=255), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("instance_name", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("triggered_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_alert_events_rule_id"), "alert_events", ["rule_id"], unique=False)
    op.create_index(op.f("ix_alert_events_user_id"), "alert_events", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_alert_events_user_id"), table_name="alert_events")
    op.drop_index(op.f("ix_alert_events_rule_id"), table_name="alert_events")
    op.drop_table("alert_events")
    op.drop_index(op.f("ix_alert_rules_user_id"), table_name="alert_rules")
    op.drop_table("alert_rules")