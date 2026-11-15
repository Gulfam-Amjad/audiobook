"""Record whether a project's stills came from an AI image model.

The publishing disclosure has to say what in a video is machine-made, and
the app cannot tell an AI still from a photograph by looking at it. Rather
than assert one or the other, it stores what the user has said. Null means
nothing has been said, and the disclosure then makes no claim.

Revision ID: b17d2e9c4a05
Revises: 8f21a0c4b3de
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b17d2e9c4a05"
down_revision = "8f21a0c4b3de"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("stills_ai_generated", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("stills_ai_generated")
