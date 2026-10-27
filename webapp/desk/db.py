"""New append-only tables; JSON is an explicitly scoped persistence adapter."""
from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import DateTime, Float, String, Table, Text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.alpha_lab.db import append, digest, dumps
from webapp.board.db import AlfaBase

__all__ = ["Benchmark", "ManualPlan", "Sample", "append", "digest", "dumps", "ensure"]


class ManualPlan(AlfaBase):
    __tablename__ = "alfa_desk_manual_plan"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    trade_id: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    stop: Mapped[float] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)


class Sample(AlfaBase):
    __tablename__ = "alfa_desk_sample"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    position_id: Mapped[str] = mapped_column(String, index=True)
    scope: Mapped[str] = mapped_column(String, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class Benchmark(AlfaBase):
    __tablename__ = "alfa_desk_benchmark"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    paper_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


def ensure(engine: Engine) -> None:
    for model in (ManualPlan, Sample, Benchmark):
        cast(Table, model.__table__).create(engine, checkfirst=True)
