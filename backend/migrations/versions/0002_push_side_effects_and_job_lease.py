"""push side effects and campaign job lease

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('streams', schema=None) as batch_op:
        batch_op.add_column(sa.Column('side_effect_fields', sa.JSON(), nullable=True))
    with op.batch_alter_table('campaign_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lease_token', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('lease_until', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('campaign_jobs', schema=None) as batch_op:
        batch_op.drop_column('lease_until')
        batch_op.drop_column('lease_token')
    with op.batch_alter_table('streams', schema=None) as batch_op:
        batch_op.drop_column('side_effect_fields')
