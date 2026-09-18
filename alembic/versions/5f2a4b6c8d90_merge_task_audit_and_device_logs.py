"""merge task audit and device logs heads

Revision ID: 5f2a4b6c8d90
Revises: 9c14e7b2a5d0, c71beb5be2ea
"""

from collections.abc import Sequence

revision: str = "5f2a4b6c8d90"
down_revision: tuple[str, str] = ("9c14e7b2a5d0", "c71beb5be2ea")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
