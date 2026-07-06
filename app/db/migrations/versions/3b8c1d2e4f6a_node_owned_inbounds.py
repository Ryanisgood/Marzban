"""node owned inbounds

Revision ID: 3b8c1d2e4f6a
Revises: 9f3a7c2d8b11
Create Date: 2026-07-06 00:00:00.000000

"""
from alembic import op
import re
import sqlalchemy as sa


revision = "3b8c1d2e4f6a"
down_revision = "9f3a7c2d8b11"
branch_labels = None
depends_on = None

_GENERATED_TAG = re.compile(r"^node-(\d+)-(hy2|anytls|vless|vmess|trojan|shadowsocks|ss)-(\d+)$")


def upgrade() -> None:
    with op.batch_alter_table("inbounds") as batch_op:
        batch_op.add_column(sa.Column("owner_node_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_inbounds_owner_node_id", ["owner_node_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_inbounds_owner_node_id_nodes",
            "nodes",
            ["owner_node_id"],
            ["id"],
            ondelete="SET NULL",
        )

    bind = op.get_bind()
    node_ids = {row[0] for row in bind.execute(sa.text("SELECT id FROM nodes")).fetchall()}
    inbound_tags = [row[0] for row in bind.execute(sa.text("SELECT tag FROM inbounds")).fetchall()]
    for tag in inbound_tags:
        match = _GENERATED_TAG.match(tag or "")
        if not match:
            continue
        node_id = int(match.group(1))
        if node_id not in node_ids:
            continue
        bind.execute(
            sa.text("UPDATE inbounds SET owner_node_id = :node_id WHERE tag = :tag"),
            {"node_id": node_id, "tag": tag},
        )


def downgrade() -> None:
    with op.batch_alter_table("inbounds") as batch_op:
        batch_op.drop_constraint("fk_inbounds_owner_node_id_nodes", type_="foreignkey")
        batch_op.drop_index("ix_inbounds_owner_node_id")
        batch_op.drop_column("owner_node_id")
