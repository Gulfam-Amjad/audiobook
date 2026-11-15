"""Record the speaking rate a voice was measured at.

A 60-second brief produced a 94-second video because the script was
written to an assumed 150 words a minute while en_US-joe-medium actually
speaks at about 180. The assumption cannot be fixed by picking a better
number, so the rate is measured from lines the voice has already recorded
and stored here.

Revision ID: 8f21a0c4b3de
Revises: 4c8cbd4144de
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8f21a0c4b3de"
down_revision = "4c8cbd4144de"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("voice_profiles") as batch:
        batch.add_column(sa.Column("measured_wpm", sa.Float(), nullable=True))
        batch.add_column(sa.Column("measured_chars_per_s", sa.Float(), nullable=True))
        batch.add_column(
            sa.Column(
                "measured_segments",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("voice_profiles") as batch:
        batch.drop_column("measured_segments")
        batch.drop_column("measured_chars_per_s")
        batch.drop_column("measured_wpm")
