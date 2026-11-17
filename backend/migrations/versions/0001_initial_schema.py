"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-16 19:37:24.200007
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('campaign_jobs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('idempotency_key', sa.String(length=64), nullable=False),
    sa.Column('request', sa.JSON(), nullable=False),
    sa.Column('alias', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('campaign_id', sa.Integer(), nullable=True),
    sa.Column('geo_stream_id', sa.Integer(), nullable=True),
    sa.Column('offer_stream_id', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    op.create_table('campaigns',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('alias', sa.String(length=200), nullable=False),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('offers',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('streams',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('campaign_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('remote_snapshot', sa.JSON(), nullable=True),
    sa.Column('remote_missing', sa.Boolean(), nullable=False),
    sa.Column('draft_version', sa.Integer(), nullable=False),
    sa.Column('push_state', sa.String(length=16), nullable=False),
    sa.Column('push_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('streams', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_streams_campaign_id'), ['campaign_id'], unique=False)

    op.create_table('stream_offers',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('stream_id', sa.Integer(), nullable=False),
    sa.Column('offer_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('share', sa.Integer(), nullable=False),
    sa.Column('pinned', sa.Boolean(), nullable=False),
    sa.Column('kt_state', sa.String(length=16), nullable=False),
    sa.Column('pub_present', sa.Boolean(), nullable=False),
    sa.Column('pub_share', sa.Integer(), nullable=True),
    sa.Column('pub_state', sa.String(length=16), nullable=True),
    sa.Column('pub_pinned', sa.Boolean(), nullable=False),
    sa.Column('ever_published', sa.Boolean(), nullable=False),
    sa.Column('remote_link_id', sa.Integer(), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['stream_id'], ['streams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stream_id', 'offer_id', name='uq_stream_offer')
    )
    with op.batch_alter_table('stream_offers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stream_offers_offer_id'), ['offer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stream_offers_stream_id'), ['stream_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('stream_offers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stream_offers_stream_id'))
        batch_op.drop_index(batch_op.f('ix_stream_offers_offer_id'))

    op.drop_table('stream_offers')
    with op.batch_alter_table('streams', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_streams_campaign_id'))

    op.drop_table('streams')
    op.drop_table('offers')
    op.drop_table('campaigns')
    op.drop_table('campaign_jobs')
