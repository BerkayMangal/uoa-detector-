"""SQLAlchemy 2.x ORM models for the backtest store's persistent schema.

Phase 3.2.1 schema (v1):

  * ``backtest_run`` — run metadata (started/finished, profile id+hash,
    universe_id, source_config_hash, signals processed, errors,
    dataset window, notes).
  * ``signal`` — one row per processed event. Columns include
    ``pipeline_latency_ms`` and ``data_source_latency_ms`` for
    backtest realism analysis.
  * ``backtest_run_error`` — one row per stage error encountered during
    a run.
  * ``schema_version`` — single-row Alembic-managed table.

Indexes (declared on the model):

  * ``signal(run_id, ts)`` — primary access pattern: "all signals in
    this run, ordered by event time" — driven by the metric calculator.
  * ``signal(run_id, ticker)`` — ticker-filtered iteration for
    per-symbol analysis.
  * ``backtest_run_error(run_id, occurred_at)`` — error timeline per
    run.

The ``full_record_json`` column on ``signal`` carries the entire
``StoredSignal`` Pydantic dump — top-level columns are the cross-cell
comparison axes (run, time, ticker, label, score, latency); the JSON
blob is the full audit record for any post-hoc analysis that needs more.
This trade-off keeps queries fast while preserving fidelity.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for the backtest store schema."""


class BacktestRunRow(Base):
    """``backtest_run`` table — one row per backtest invocation."""

    __tablename__ = "backtest_run"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    profile_id: Mapped[str] = mapped_column(String, nullable=False)
    profile_content_hash: Mapped[str] = mapped_column(String, nullable=False)
    universe_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_config_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    total_signals_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    total_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dataset_window_start: Mapped[datetime | None] = mapped_column(nullable=True)
    dataset_window_end: Mapped[datetime | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    signals: Mapped[list[SignalRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
    )
    errors: Mapped[list[BacktestRunErrorRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
    )


class SignalRow(Base):
    """``signal`` table — one row per processed event in a run."""

    __tablename__ = "signal"

    # Composite PK so the same event_id can appear in multiple runs
    # (e.g., two cells of the 4-cell matrix replaying the same data).
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("backtest_run.run_id"), primary_key=True,
    )
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    max_r: Mapped[float] = mapped_column(Float, nullable=False)
    combined_score_pre: Mapped[float | None] = mapped_column(Float, nullable=True)
    combined_score_post: Mapped[float | None] = mapped_column(Float, nullable=True)
    profile_id: Mapped[str] = mapped_column(String, nullable=False)
    profile_content_hash: Mapped[str] = mapped_column(String, nullable=False)
    pipeline_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_source_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    full_record_json: Mapped[str] = mapped_column(Text, nullable=False)

    run: Mapped[BacktestRunRow] = relationship(back_populates="signals")

    __table_args__ = (
        Index("ix_signal_run_ts", "run_id", "ts"),
        Index("ix_signal_run_ticker", "run_id", "ticker"),
    )


class BacktestRunErrorRow(Base):
    """``backtest_run_error`` table — per-stage error log per run."""

    __tablename__ = "backtest_run_error"

    # Synthetic auto-increment PK; a single run can have many errors,
    # potentially with the same stage_name + event_id, so a composite
    # business key isn't unique.
    error_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("backtest_run.run_id"), nullable=False,
    )
    event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stage_name: Mapped[str] = mapped_column(String, nullable=False)
    error_type: Mapped[str] = mapped_column(String, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)

    run: Mapped[BacktestRunRow] = relationship(back_populates="errors")

    __table_args__ = (
        Index("ix_error_run_occurred", "run_id", "occurred_at"),
    )
