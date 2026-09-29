"""Revocable mobile sessions, refresh history and email verification.

Revision ID: b281e309ac74
Revises: a216b40c912e
"""

import sqlalchemy as sa

from alembic import op

revision = "b281e309ac74"
down_revision = "a216b40c912e"
branch_labels = None
depends_on = None


def entity_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.create_table(
        "mobile_sessions",
        *entity_columns(),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("installation_key_hash", sa.String(64), nullable=False),
        sa.Column("device_name", sa.String(80), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("access_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mobile_sessions_user_id", "mobile_sessions", ["user_id"])
    op.create_index("ix_mobile_sessions_expires_at", "mobile_sessions", ["expires_at"])
    op.create_table(
        "mobile_refresh_tokens",
        *entity_columns(),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("mobile_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_mobile_refresh_tokens_session_id", "mobile_refresh_tokens", ["session_id"])
    op.create_table(
        "mobile_registrations",
        *entity_columns(),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("mobile_registrations")
    op.drop_table("mobile_refresh_tokens")
    op.drop_table("mobile_sessions")
