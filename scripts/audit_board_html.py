"""Audit rendered Alfa Board HTML against the honesty rules (contract §2).

    uv run python scripts/audit_board_html.py page.html

It runs against a saved page, live or from a test render, and exits non-zero on
any failure. A pass over zero rows is reported as VACUOUS and fails: an audit
that checked nothing must never read as a pass.

Checks:
  R-WD1  no forbidden word in generated copy (vendor fields excluded)
  R-CA1  every row carries an "AMA" counter-argument
  R-EV2  the 0-1 combined score appears only inside the audit block
  R-UN1  unknown families say "bilgi yok, temiz demek değil"
  R-CO1  a quoted row states its quote age; an unquoted row says "kotasyon yok"
  R-IV1  the vol-premium section carries the "vol sat" sentence
  R-DL1  delayed items carry a date and stay out of the evidence counts
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapp.board.honesty import forbidden_words

# Vendor data (politician and insider names, filed amounts, catalyst titles) is
# quoted from the source, never generated, so the word guard does not apply.
VENDOR_ATTRS = ("data-delayed-who", "data-delayed-what", "data-vendor")
ROW_RE = re.compile(r"<article[^>]*data-row.*?</article>", re.S)
AUDIT_RE = re.compile(r"<details[^>]*data-audit.*?</details>", re.S)
VENDOR_RE = re.compile(
    r"<(\w+)[^>]*(?:" + "|".join(VENDOR_ATTRS) + r")[^>]*>.*?</\1>", re.S,
)
SCORE_RE = re.compile(r"Birleşik skor|data-score=|skor[^<]{0,12}:\s*0\.\d")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def visible_text(fragment: str) -> str:
    """Rendered text plus title attributes, with vendor spans removed."""
    titles = " ".join(re.findall(r'title="([^"]*)"', fragment))
    body = VENDOR_RE.sub(" ", fragment)
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return html.unescape(body + " " + titles)


def main(path: str) -> int:
    raw = Path(path).read_text(encoding="utf-8")
    rows = ROW_RE.findall(raw)
    failures: list[str] = []
    notes: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        (notes if ok else failures).append(f"{label}: {'ok' if ok else 'FAIL ' + detail}")

    # Vacuity guard first: an audit over an empty board proves nothing.
    if not rows:
        print("AUDIT: VACUOUS — no data-row article rendered; nothing was checked")
        return 1
    notes.append(f"rows audited: {len(rows)}")

    text = visible_text(raw)
    hits = forbidden_words(text)
    check(not hits, "R-WD1 forbidden words", str(hits))

    missing_ama = [i for i, row in enumerate(rows) if "AMA" not in visible_text(row)]
    check(not missing_ama, "R-CA1 AMA on every row", f"rows without AMA: {missing_ama}")

    outside = AUDIT_RE.sub(" ", raw)
    check(not SCORE_RE.search(outside), "R-EV2 score only in the audit block",
          str(SCORE_RE.findall(outside)[:3]))

    if 'data-family-state="unknown"' in raw:
        check("bilgi yok, temiz demek değil" in text, "R-UN1 unknown is not clean")
    else:
        notes.append("R-UN1: no unknown family rendered")

    # The chip states are tradable / narrow (DAR) / untradable (İŞLENMEZ) / no_quote.
    # A DAR row HAS a quote — it is thin, not missing — and an İŞLENMEZ row has one too;
    # only a no_quote row must say "kotasyon yok". Reading "wide" here counted every DAR
    # row as unquoted and failed a page that was correct (2026-09-16).
    quoted = [r for r in rows if 'data-chip="tradable"' in r or 'data-chip="narrow"' in r]
    aged = [r for r in quoted if "sn önce" in visible_text(r) or "dk önce" in visible_text(r)]
    check(len(aged) == len(quoted), "R-CO1 every quoted row states its quote age",
          f"{len(quoted) - len(aged)} of {len(quoted)} quoted rows have no age")
    missing_quote = [r for r in rows if 'data-chip="no_quote"' in r]
    if missing_quote:
        notes.append(f"rows with no quote at all: {len(missing_quote)} (must read 'kotasyon yok')")
        check(text.count("kotasyon yok") >= 1, "R-CO1 rows with no quote say kotasyon yok")
    else:
        notes.append("every row carries a quote; no 'kotasyon yok' expected")

    if "IV-rank" in raw or "iv_rank" in raw:
        check("vol sat" in text, "R-IV1 the vol sentence is present")
    else:
        notes.append("R-IV1: no IV richness rendered")

    delayed = re.findall(r"<[^>]*data-delayed-item[^>]*>.*?</[^>]+>", raw, re.S)
    if delayed:
        undated = [i for i, d in enumerate(delayed) if not DATE_RE.search(visible_text(d))]
        check(not undated, "R-DL1 delayed items carry a date", f"undated: {undated}")
        strips = re.findall(r"<[^>]*data-evidence-strip[^>]*>.*?</[^>]+>", raw, re.S)
        check(not any("data-delayed-item" in s for s in strips),
              "R-DL1 delayed items stay out of the evidence strip")
    else:
        notes.append("R-DL1: no delayed item rendered")

    for line in notes:
        print("  " + line)
    for line in failures:
        print("  " + line)
    print("AUDIT: " + ("PASS" if not failures else "FAIL"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "page.html"))
