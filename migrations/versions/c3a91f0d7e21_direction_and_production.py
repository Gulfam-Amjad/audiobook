"""Store the director's fields per shot and the finishing pass per project.

Revision ID: c3a91f0d7e21
Revises: b17d2e9c4a05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3a91f0d7e21"
down_revision = "b17d2e9c4a05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("production", sa.JSON(), nullable=False, server_default="{}")
        )
    with op.batch_alter_table("scenes") as batch:
        batch.add_column(
            sa.Column("direction", sa.JSON(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("scenes") as batch:
        batch.drop_column("direction")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("production")
