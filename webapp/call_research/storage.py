"""Separate append-only shadow evidence; no order or position-execution API."""

from __future__ import annotations

from datetime import date, datetime
from typing import cast

from sqlalchemy import Date, DateTime, String, Table, Text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase


class ResearchCohort(AlfaBase):
    __tablename__ = "alfa_call_research_cohort"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class ResearchResult(AlfaBase):
    __tablename__ = "alfa_call_research_result"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    cohort_key: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class CallContext(AlfaBase):
    __tablename__ = "alfa_call_context"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    decision_key: Mapped[str] = mapped_column(String, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class DailyReceipt(AlfaBase):
    __tablename__ = "alfa_call_data_receipt"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


def ensure(engine: Engine) -> None:
    for model in (ResearchCohort, ResearchResult, CallContext, DailyReceipt):
        cast(Table, model.__table__).create(engine, checkfirst=True)
