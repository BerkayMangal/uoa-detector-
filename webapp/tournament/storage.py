"""Additive first-observation records. No existing data/position is rewritten."""

from __future__ import annotations

from datetime import date, datetime
from typing import cast

from sqlalchemy import Date, DateTime, String, Table, Text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase


class TournamentAudit(AlfaBase):
    __tablename__ = "alfa_tournament_audit"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class TournamentDecision(AlfaBase):
    __tablename__ = "alfa_tournament_decision"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    family: Mapped[str] = mapped_column(String, index=True)
    arm: Mapped[str] = mapped_column(String, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class TournamentResult(AlfaBase):
    __tablename__ = "alfa_tournament_result"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    decision_key: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


def ensure(engine: Engine) -> None:
    for table in (TournamentAudit, TournamentDecision, TournamentResult):
        cast(Table, table.__table__).create(engine, checkfirst=True)
