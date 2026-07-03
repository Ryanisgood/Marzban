"""node active inbounds

Revision ID: 6f7d9c2b1a4e
Revises: 2b231de97dc3
Create Date: 2026-07-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6f7d9c2b1a4e'
down_revision = '2b231de97dc3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    nodeinboundsmode_enum = sa.Enum('legacy', 'panel', name='nodeinboundsmode')
    nodeinboundsmode_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        'nodes',
        sa.Column(
            'inbounds_mode',
            nodeinboundsmode_enum,
            server_default='legacy',
            nullable=False,
        ),
    )
    op.create_table(
        'node_inbounds_association',
        sa.Column('node_id', sa.Integer(), nullable=True),
        sa.Column('inbound_tag', sa.String(length=256), nullable=True),
        sa.ForeignKeyConstraint(['inbound_tag'], ['inbounds.tag'], ),
        sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ),
        sa.UniqueConstraint('node_id', 'inbound_tag'),
    )


def downgrade() -> None:
    op.drop_table('node_inbounds_association')
    op.drop_column('nodes', 'inbounds_mode')
    nodeinboundsmode_enum = sa.Enum('legacy', 'panel', name='nodeinboundsmode')
    nodeinboundsmode_enum.drop(op.get_bind(), checkfirst=True)
