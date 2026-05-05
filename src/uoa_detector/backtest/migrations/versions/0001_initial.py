"""Initial schema: backtest_run, signal, backtest_run_error.

Revision ID: 0001_initial
Revises:
Create Date: Phase 3.2.1

This migration creates all four tables for v1. ``schema_version`` is
implicitly created by Alembic itself as ``alembic_version``; the
acceptance doc's "schema_version" reference is satisfied by Alembic's
own version-tracking table — there is no separate user-facing
schema_version table because Alembic provides one already.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_run",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("profile_content_hash", sa.String(), nullable=False),
        sa.Column("universe_id", sa.String(), nullable=True),
        sa.Column("source_config_hash", sa.String(), nullable=True),
        sa.Column(
            "total_signals_processed", sa.Integer(), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_errors", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column("dataset_window_start", sa.DateTime(), nullable=True),
        sa.Column("dataset_window_end", sa.DateTime(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    op.create_table(
        "signal",
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("backtest_run.run_id"),
            primary_key=True,
        ),
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("max_r", sa.Float(), nullable=False),
        sa.Column("combined_score_pre", sa.Float(), nullable=True),
        sa.Column("combined_score_post", sa.Float(), nullable=True),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("profile_content_hash", sa.String(), nullable=False),
        sa.Column("pipeline_latency_ms", sa.Float(), nullable=True),
        sa.Column("data_source_latency_ms", sa.Float(), nullable=True),
        sa.Column("full_record_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_signal_run_ts", "signal", ["run_id", "ts"])
    op.create_index("ix_signal_run_ticker", "signal", ["run_id", "ticker"])

    op.create_table(
        "backtest_run_error",
        sa.Column(
            "error_id", sa.Integer(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("backtest_run.run_id"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(), nullable=True),
        sa.Column("stage_name", sa.String(), nullable=False),
        sa.Column("error_type", sa.String(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_error_run_occurred", "backtest_run_error", ["run_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_error_run_occurred", table_name="backtest_run_error")
    op.drop_table("backtest_run_error")
    op.drop_index("ix_signal_run_ticker", table_name="signal")
    op.drop_index("ix_signal_run_ts", table_name="signal")
    op.drop_table("signal")
    op.drop_table("backtest_run")
