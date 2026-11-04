"""Read-only explanation of immutable opening publications, never a policy input."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from webapp.alpha_lab.db import digest
from webapp.board.db import session_factory
from webapp.live_alpha.store import LiveScan
from webapp.opening import storage
from webapp.opening.model import Advice, Evidence, settings, utc
from webapp.opening.view import Statement

CURATED_ROLE = "Mahalle ve Stajyer ayrı şirket/makro bağlamıdır; bu sinyale AL oyu vermez."


@dataclass(frozen=True)
class PriceEvidence:
    status: str = "unknown"
    spot: float | None = None
    move_pct: float | None = None
    move_atr: float | None = None
    relative_pp: float | None = None
    at: datetime | None = None
    text: str = "Sayısal fiyat bağlamı doğrulanamadı; kayıtlı açıklama aşağıda korunur."


@dataclass(frozen=True)
class FlowEvidence:
    status: str = "unknown"
    directional_premium_usd: float | None = None
    opposite_premium_usd: float | None = None
    unknown_premium_usd: float | None = None
    dominant_share_pct: float | None = None
    prints: int | None = None
    contracts: int | None = None
    at: datetime | None = None
    text: str = "Akışın sayısal özeti doğrulanamadı; kayıtlı açıklama aşağıda korunur."


@dataclass(frozen=True)
class NewsEvidence:
    state: str = "unknown"
    checked_at: datetime | None = None
    text: str = "UW haber kontrolünün sayısal durumu bilinmiyor."
    qualifying_vote: bool | None = None
    role: str = "Haberin bu kayıttaki destek rolü doğrulanamadı."


@dataclass(frozen=True)
class SupportEvidence:
    path: str = "unknown"
    role: str = "unknown"
    text: str = "Seçilmiş destek yolu bu kayıtta doğrulanamadı."


@dataclass(frozen=True)
class Explanation:
    key: str
    ticker: str
    source_valid: bool
    source_status: str
    first_issued_at: datetime | None
    statement_at: datetime | None
    scan_started_at: datetime | None
    scan_finished_at: datetime | None
    decision_at: datetime | None
    expires_at: datetime | None
    current: bool
    reasons: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    price: PriceEvidence = field(default_factory=PriceEvidence)
    flow: FlowEvidence = field(default_factory=FlowEvidence)
    news: NewsEvidence = field(default_factory=NewsEvidence)
    support: SupportEvidence = field(default_factory=SupportEvidence)
    curated_role: str = CURATED_ROLE


def _aware(at: datetime | None) -> datetime | None:
    return at.astimezone(UTC) if at is not None and at.tzinfo is not None and at.utcoffset() is not None else None


def london_time(at: datetime | str | None) -> str:
    value = _timestamp(at) if isinstance(at, str) else _aware(at)
    return value.astimezone(ZoneInfo("Europe/London")).strftime("%d.%m.%Y %H:%M:%S %Z · Europe/London") if value else "Bilinmiyor"


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) and all(isinstance(k, str) for k in value) else {}


def _json(raw: str) -> dict[str, object]:
    # The sole untyped JSON boundary; no numbers are extracted from prose.
    value: object = json.loads(raw)
    return _object(value)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _number(value: object, *, positive: bool = False) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) and (not positive or result > 0) else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _support(card: dict[str, object]) -> SupportEvidence:
    path = card.get("path")
    if path == "P1_news_continuation" and card.get("direction") == "up":
        return SupportEvidence(path, "uw_news_selected", "UW haber desteği seçilmiş. Bu, göreli gücün başarısız olduğu anlamına gelmez; başlık olumlu gelişme kanıtı değildir.")
    if path == "P2_market_relative_flow" and card.get("direction") == "up":
        return SupportEvidence(path, "relative_strength_selected", "SPY'a göre göreli güç desteği seçilmiş. Haber kontrolüyle uygun haber desteği aynı şey değildir.")
    if path == "P3_pullback_entry" and card.get("direction") == "up":
        return SupportEvidence(path, "pullback", "Geri çekilme yolu kaydedilmiş; haber/göreli güç desteğinin hangisinin seçildiği bu yol etiketinden ayrılamaz.")
    if path == "P4_bearish_setup" and card.get("direction") == "down":
        return SupportEvidence(path, "bearish", "Aşağı yönlü akış ve fiyat teyidine dayanan ayrı kurulum. Yukarı yönlü haber VEYA göreli güç açıklaması bu yola uygulanmaz.")
    return SupportEvidence()


def _price(snapshot: dict[str, object], ticker: str, at: datetime) -> PriceEvidence:
    observation = _object(_object(snapshot.get("price_observations")).get(ticker))
    basic_spot = _number(observation.get("spot"), positive=True)
    basic_at = _timestamp(observation.get("source_at"))
    if basic_at is None or basic_at > at:
        return PriceEvidence()
    context = _object(snapshot.get("news_research_context"))
    raw = _object(_object(context.get("prices")).get(ticker)) if context.get("version") == "physical_news_v1" else {}
    spot, previous, atr = (_number(raw.get(k), positive=True) for k in ("spot", "prev_close", "atr"))
    source_at = _timestamp(raw.get("spot_fetched_at"))
    if not raw:
        return PriceEvidence("partial", basic_spot, at=basic_at,
                             text=f"Kayıtlı fiyat ${basic_spot:,.2f}; hareket/ATR/göreli güç sayısal bağlamı yok." if basic_spot is not None else PriceEvidence().text)
    if (source_at is None or source_at > at or spot is None or spot != basic_spot or source_at != basic_at
            or raw.get("ticker") != ticker):
        return PriceEvidence()
    try:
        prior_day = date.fromisoformat(str(raw.get("prev_close_day")))
        source_day = date.fromisoformat(str(raw.get("source_session")))
    except ValueError:
        prior_day, source_day = None, None
    if (raw.get("state") != "checked_found" or prior_day is None or source_day is None
            or prior_day >= source_day or raw.get("source_session") != observation.get("session")
            or source_day != at.astimezone(ZoneInfo("America/New_York")).date()):
        return PriceEvidence("partial", spot, at=source_at,
                             text=f"Kayıtlı fiyat ${spot:,.2f}; tam fiyat/önceki kapanış/ATR bağlamının durumu doğrulanamadı.")
    move = (spot / previous - 1) * 100 if previous else None
    move_atr = (spot - previous) / atr if previous and atr else None
    benchmark = _number(raw.get("benchmark_move"))
    relative = move - benchmark * 100 if move is not None and benchmark is not None else None
    if not all(v is None or math.isfinite(v) for v in (move, move_atr, relative)):
        return PriceEvidence()
    parts = [f"Kayıtlı fiyat ${spot:,.2f}"]
    if move is not None:
        parts.append(f"önceki kapanışa göre %{move:+.2f}")
    if move_atr is not None:
        parts.append(f"{move_atr:+.2f} ATR")
    if relative is not None:
        parts.append(f"SPY'a göre {relative:+.2f} yüzde puan")
    else:
        parts.append("göreli güç bilinmiyor")
    return PriceEvidence("observed" if all(v is not None for v in (move, move_atr, relative)) else "partial",
                         spot, move, move_atr, relative, source_at, "; ".join(parts) + ".")


def _flow(snapshot: dict[str, object], ticker: str, direction: str, at: datetime) -> FlowEvidence:
    raw = _object(_object(snapshot.get("flow_observations")).get(ticker))
    source_at = _timestamp(raw.get("last_ts"))
    if source_at is None or source_at > at or direction not in {"up", "down"}:
        return FlowEvidence()
    if raw.get("ticker") != ticker:
        return FlowEvidence(at=source_at)
    up, down, unknown = (_number(raw.get(k)) for k in ("premium_up", "premium_down", "premium_side_unknown"))
    if any(v is None or v < 0 for v in (up, down, unknown)):
        return FlowEvidence(at=source_at)
    assert up is not None and down is not None and unknown is not None
    if not math.isfinite(up + down):
        return FlowEvidence()
    directional, opposite = (up, down) if direction == "up" else (down, up)
    share = directional / (up + down) * 100 if up + down > 0 else None
    prints, contracts = _count(raw.get("prints_deduped")), _count(raw.get("distinct_contracts"))
    text = f"{'Yukarı' if direction == 'up' else 'Aşağı'} yönlü prim ${directional:,.0f}; karşı yön ${opposite:,.0f}; tarafı bilinmeyen ${unknown:,.0f}."
    if prints is not None and contracts is not None:
        text += f" {prints} tekil baskı, {contracts} farklı kontrat."
    return FlowEvidence("observed", directional, opposite, unknown, share, prints, contracts, source_at, text)


def _news(snapshot: dict[str, object], ticker: str, at: datetime, support: SupportEvidence) -> NewsEvidence:
    raw = _object(_object(snapshot.get("news_observations")).get(ticker))
    state = raw.get("state")
    checked = _timestamp(raw.get("checked_at"))
    texts = {"checked_found": "UW kontrolünde başlık var; bu durum tek başına uygun zaman penceresinde haber oyu veya olumlu haber demek değildir.",
             "checked_none": "UW kontrolü kayıtlı; bu kontrolde başlık bulunmamış. Piyasanın tamamında haber olmadığı anlamına gelmez.",
             "failed": "UW haber kontrolü başarısız; haber durumu bilinmiyor.",
             "stale": "UW haber kontrolü bayat; güncel haber durumu bilinmiyor.",
             "not_checked": "UW haber kontrolü yapılmamış."}
    if not isinstance(state, str) or state not in texts or (state != "not_checked" and checked is None) or (checked is not None and checked > at):
        return NewsEvidence()
    vote = True if support.role == "uw_news_selected" and state == "checked_found" else None
    role = ("Bu kayıtta seçilmiş destek UW haberidir." if vote else
            "Bu kayıtta seçilmiş destek göreli güçtür." if support.role == "relative_strength_selected" else
            "Aşağı yönlü kurulumda haber kontrolü ayrı bir kontroldür; yukarı OR kapısı değildir." if support.role == "bearish" else
            "Kontrol sonucu, tek başına seçilmiş destek rolünü açıklamaz.")
    return NewsEvidence(state, checked, texts[state], vote, role)


def _original(session: Session, row: storage.OpeningDecision, cycle: storage.OpeningCycle | None,
              advice: Advice, now: datetime, cache: dict[str, tuple[LiveScan, dict[str, object]] | None]) -> tuple[dict[str, object], dict[str, object], LiveScan] | None:
    if cycle is None or row.cycle_key != cycle.key or row.day != cycle.day or utc(row.published_at) != utc(cycle.published_at):
        return None
    metadata = _json(cycle.payload_json)
    phase, profile = metadata.get("phase"), metadata.get("profile_hash")
    if (not isinstance(phase, str) or not _hash(profile) or metadata.get("scan_id") != cycle.scan_id
            or _timestamp(metadata.get("published_at")) != utc(row.published_at)
            or cycle.key != digest((profile, cycle.day, cycle.scan_id, phase))
            or row.key != digest((cycle.key, advice.scope, advice.position_id, advice.ticker))
            or row.ticker != advice.ticker or row.action != advice.action):
        return None
    if cycle.scan_id not in cache:
        scan = session.scalar(select(LiveScan).where(LiveScan.scan_id == cycle.scan_id,
                              LiveScan.started_at <= now, LiveScan.finished_at <= now))
        cache[cycle.scan_id] = (scan, _json(scan.snapshot_json)) if scan else None
    found = cache[cycle.scan_id]
    if found is None:
        return None
    scan, snapshot = found
    started, finished, published = utc(scan.started_at), utc(scan.finished_at), utc(row.published_at)
    if (not started <= finished <= published <= now or metadata.get("source_snapshot_hash") != digest(snapshot)
            or _timestamp(metadata.get("source_started_at")) != started
            or _timestamp(metadata.get("source_finished_at")) != finished
            or snapshot.get("policy_version") != advice.source_policy
            or snapshot.get("profile_sha256") != advice.source_profile_hash
            or not advice.source_policy or not _hash(advice.source_profile_hash)):
        return None
    raw_cards = snapshot.get("cards")
    if not isinstance(raw_cards, list):
        return None
    for value in raw_cards:
        card = _object(value)
        if (card.get("ticker") == advice.ticker and card.get("direction") == advice.direction
                and digest(card) == advice.source_card_hash and card.get("policy_version") == advice.source_policy
                and card.get("profile_sha256") == advice.source_profile_hash):
            decision = _timestamp(card.get("decision_as_of"))
            if decision is not None and started <= decision <= finished:
                return snapshot, card, scan
    return None


def _first(session: Session, row: storage.OpeningDecision, cycle: storage.OpeningCycle | None,
           now: datetime, cache: dict[str, tuple[LiveScan, dict[str, object]] | None]) -> datetime | None:
    if cycle is None:
        return None
    metadata = _json(cycle.payload_json)
    profile = metadata.get("profile_hash")
    if not _hash(profile):
        return None
    primary = session.get(storage.OpeningPrimary, digest((profile, row.day, "entry", row.ticker)))
    if (primary is None or primary.kind != "entry" or primary.day != row.day or primary.ticker != row.ticker
            or primary.decision_key is None or utc(primary.published_at) > utc(row.published_at)):
        return None
    original = session.get(storage.OpeningDecision, primary.decision_key)
    original_cycle = session.get(storage.OpeningCycle, primary.cycle_key)
    if (original is None or original_cycle is None or original.cycle_key != primary.cycle_key
            or original.action != "BUY" or original.day != row.day or original.ticker != row.ticker
            or utc(original.published_at) != utc(primary.published_at)
            or utc(original_cycle.published_at) != utc(primary.published_at)
            or _json(original_cycle.payload_json).get("profile_hash") != profile):
        return None
    try:
        original_advice = Advice.model_validate_json(original.payload_json)
        if _original(session, original, original_cycle, original_advice, now, cache) is None:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    return utc(primary.published_at)


def _fallback(key: str, advice: Advice, at: datetime | None, status: str) -> Explanation:
    return Explanation(key, advice.ticker, False, status, None, at, None, None, None,
                       _aware(advice.expires_at), False, advice.reasons, advice.evidence)


def build(engine: Engine, now: datetime, statements: Sequence[Statement] = (),
          keys: Sequence[str] = ()) -> dict[str, Explanation]:
    """Batch by immutable decision keys; current is inherited, never inferred.

    Keys-only records are historical. Missing storage can preserve a supplied
    Statement's saved reasons but cannot manufacture source metrics/permission.
    """
    physical_now = _aware(now)
    if physical_now is None:
        raise ValueError("timezone-aware presentation time required")
    supplied = {s.key: s for s in statements if _aware(s.at) is None or cast(datetime, _aware(s.at)) <= physical_now}
    requested = tuple(dict.fromkeys((*supplied, *keys)))[:settings().query_limit]
    result = {key: _fallback(key, s.advice, _aware(s.at), "saved_record_unavailable") for key, s in supplied.items()}
    if not requested:
        return result
    try:
        names = set(inspect(engine).get_table_names())
        required = (storage.OpeningDecision, storage.OpeningCycle, storage.OpeningPrimary, LiveScan)
        if any(t.__tablename__ not in names for t in required):
            return result
        with session_factory(engine)() as session:
            rows = session.scalars(select(storage.OpeningDecision)
                                   .where(storage.OpeningDecision.key.in_(requested), storage.OpeningDecision.published_at <= physical_now)).all()
            source_cache: dict[str, tuple[LiveScan, dict[str, object]] | None] = {}
            for row in rows:
                try:
                    advice = Advice.model_validate_json(row.payload_json)
                except (ValueError, TypeError):
                    continue
                at = utc(row.published_at)
                explanation = _fallback(row.key, advice, at, "source_unverified")
                result[row.key] = explanation
                try:
                    cycle = session.get(storage.OpeningCycle, row.cycle_key)
                    first = _first(session, row, cycle, physical_now, source_cache)
                    original = _original(session, row, cycle, advice, physical_now, source_cache)
                    statement = supplied.get(row.key)
                    same_statement = statement is None or (statement.advice == advice and _aware(statement.at) == at
                                      and cycle is not None and statement.scan_id == cycle.scan_id)
                    if original is None or not same_statement:
                        result[row.key] = replace(explanation, first_issued_at=first)
                        continue
                    snapshot, card, scan = original
                    support = _support(card)
                    decision_at = _timestamp(card.get("decision_as_of"))
                    assert decision_at is not None
                    result[row.key] = Explanation(row.key, advice.ticker, True, "original_source_verified", first, at,
                        utc(scan.started_at), utc(scan.finished_at), decision_at,
                        _aware(advice.expires_at), bool(statement and statement.current), advice.reasons, advice.evidence,
                        _price(snapshot, advice.ticker, decision_at), _flow(snapshot, advice.ticker, advice.direction, decision_at),
                        _news(snapshot, advice.ticker, decision_at, support), support)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    continue
    except Exception:
        # No connection details or raw exception text leave this read-only view.
        return result
    return result
