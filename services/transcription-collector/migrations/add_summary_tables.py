"""Add summary and action items tables

Revision ID: add_summary_tables
Revises: 
Create Date: 2025-08-03 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'add_summary_tables'
down_revision = None  # Replace with your latest revision
branch_labels = None
depends_on = None


def upgrade():
    # Create meeting_summaries table
    op.create_table(
        'meeting_summaries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('summary_data', sa.JSON(), nullable=False),
        sa.Column('overview', sa.ARRAY(sa.Text()), nullable=True),
        sa.Column('subject_line', sa.String(255), nullable=True),
        sa.Column('sentiment', sa.String(50), nullable=True),
        sa.Column('key_points', sa.ARRAY(sa.Text()), nullable=True),
        sa.Column('decisions', sa.ARRAY(sa.Text()), nullable=True),
        sa.Column('questions', sa.ARRAY(sa.Text()), nullable=True),
        sa.Column('challenges', sa.ARRAY(sa.Text()), nullable=True),
        sa.Column('participants', sa.ARRAY(sa.String()), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create foreign key constraint
    op.create_foreign_key(
        'fk_meeting_summaries_meeting_id',
        'meeting_summaries',
        'meetings',
        ['meeting_id'],
        ['id'],
        ondelete='CASCADE'
    )
    
    # Create index for faster lookups
    op.create_index('idx_meeting_summaries_meeting_id', 'meeting_summaries', ['meeting_id'])
    
    # Create action_items table
    op.create_table(
        'action_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('summary_id', sa.Integer(), nullable=True),
        sa.Column('task', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('owner', sa.String(255), nullable=True),
        sa.Column('assignee_email', sa.String(255), nullable=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('priority', sa.String(20), nullable=True, server_default='medium'),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create foreign key constraints for action_items
    op.create_foreign_key(
        'fk_action_items_meeting_id',
        'action_items',
        'meetings',
        ['meeting_id'],
        ['id'],
        ondelete='CASCADE'
    )
    
    op.create_foreign_key(
        'fk_action_items_summary_id',
        'action_items',
        'meeting_summaries',
        ['summary_id'],
        ['id'],
        ondelete='CASCADE'
    )
    
    # Create indexes for action_items
    op.create_index('idx_action_items_meeting_id', 'action_items', ['meeting_id'])
    op.create_index('idx_action_items_summary_id', 'action_items', ['summary_id'])
    op.create_index('idx_action_items_status', 'action_items', ['status'])
    op.create_index('idx_action_items_due_date', 'action_items', ['due_date'])
    
    # Create meeting_insights table for additional analytics
    op.create_table(
        'meeting_insights',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('summary_id', sa.Integer(), nullable=True),
        sa.Column('total_speaking_time', sa.Integer(), nullable=True),  # in seconds
        sa.Column('participant_speaking_times', sa.JSON(), nullable=True),  # {"participant": seconds}
        sa.Column('word_count', sa.Integer(), nullable=True),
        sa.Column('sentiment_scores', sa.JSON(), nullable=True),  # detailed sentiment analysis
        sa.Column('topics_discussed', sa.ARRAY(sa.String()), nullable=True),
        sa.Column('meeting_effectiveness_score', sa.Float(), nullable=True),
        sa.Column('engagement_metrics', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create foreign key constraints for meeting_insights
    op.create_foreign_key(
        'fk_meeting_insights_meeting_id',
        'meeting_insights',
        'meetings',
        ['meeting_id'],
        ['id'],
        ondelete='CASCADE'
    )
    
    op.create_foreign_key(
        'fk_meeting_insights_summary_id',
        'meeting_insights',
        'meeting_summaries',
        ['summary_id'],
        ['id'],
        ondelete='CASCADE'
    )
    
    # Create index for meeting_insights
    op.create_index('idx_meeting_insights_meeting_id', 'meeting_insights', ['meeting_id'])


def downgrade():
    # Drop tables in reverse order due to foreign key constraints
    op.drop_table('meeting_insights')
    op.drop_table('action_items')
    op.drop_table('meeting_summaries')
