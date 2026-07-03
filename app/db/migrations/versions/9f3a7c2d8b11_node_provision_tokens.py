"""node provision tokens

Revision ID: 9f3a7c2d8b11
Revises: 6f7d9c2b1a4e
Create Date: 2026-07-03 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "9f3a7c2d8b11"
down_revision = "6f7d9c2b1a4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_provision_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=34), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("active_inbounds_json", sa.JSON(), nullable=False),
        sa.Column("core_kind", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_node_provision_tokens_node_id"),
        "node_provision_tokens",
        ["node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_node_provision_tokens_token_hash"),
        "node_provision_tokens",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_node_provision_tokens_token_hash"),
        table_name="node_provision_tokens",
    )
    op.drop_index(
        op.f("ix_node_provision_tokens_node_id"),
        table_name="node_provision_tokens",
    )
    op.drop_table("node_provision_tokens")
