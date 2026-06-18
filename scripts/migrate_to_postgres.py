"""Copy stored signals from a local SQLite store into Postgres (Railway).

The screener reads whatever ``DATABASE_URL`` points at. Locally that is the
SQLite seed; on Railway it is Postgres. This script copies the rows so the
deployed app shows the same signals. Idempotent: re-running merges (upserts)
on primary key, so it is safe to run repeatedly.

Usage:
    DATABASE_URL=postgres://... \\
      PYTHONPATH=src .venv/bin/python scripts/migrate_to_postgres.py \\
        --source sqlite:///webapp/seed.db
"""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from uoa_detector.backtest.sqlite_models import (
    BacktestRunErrorRow,
    BacktestRunRow,
    Base,
    SignalRow,
)

_ORDER = [BacktestRunRow, SignalRow, BacktestRunErrorRow]  # FK-safe order


def _norm(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _copy_one(row: object, model: type) -> object:
    cols = {c.name: getattr(row, c.name) for c in model.__table__.columns}
    return model(**cols)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="migrate_to_postgres")
    p.add_argument("--source", default="sqlite:///webapp/seed.db")
    p.add_argument("--dest", default=os.environ.get("DATABASE_URL", ""))
    args = p.parse_args(argv)
    if not args.dest:
        raise SystemExit("set DATABASE_URL or pass --dest")

    src = create_engine(args.source, future=True)
    dst = create_engine(_norm(args.dest), future=True)
    Base.metadata.create_all(dst)

    src_session = sessionmaker(src, future=True)
    dst_session = sessionmaker(dst, future=True)

    with src_session() as s, dst_session() as d:
        for model in _ORDER:
            n = 0
            for row in s.execute(select(model)).scalars():
                d.merge(_copy_one(row, model))
                n += 1
            d.commit()
            print(f"{model.__tablename__}: {n} rows")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
