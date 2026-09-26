"""Resume jobs deferred by the retired shared token allowance.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""UPDATE jobs SET run_after = now(), last_error = NULL, updated_at = now()
                  WHERE status = 'queued' AND last_error IN (
                      'AI paused: daily workspace budget reached',
                      'AI paused: daily global budget reached')""")


def downgrade():
    # Resumed work cannot be undone; usage history and schema are unchanged.
    pass
