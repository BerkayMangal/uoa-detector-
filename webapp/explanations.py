"""Plain-English explanations for the screener UI.

Every axis, term, and label gets a human explanation so the dashboard
teaches as it screens. This is content, not logic — kept in one place so
the wording is consistent and easy to edit.
"""

from __future__ import annotations

# Axes that have NO live data source in our current tier — they are only
# populated for the sample backtest (computed from the ThetaData chain we own).
# Live, they read '—' and do NOT contribute real signal to the combined score.
OFFLINE_LIVE_AXES: frozenset[str] = frozenset(
    {"convexity_score", "relative_premium_score"},
)

# One entry per StoredSignal sub-score, in display order. (key, title, what)
AXES: list[tuple[str, str, str]] = [
    ("uoa_score", "Unusual Options Activity",
     "How aggressive this specific options print is — a sweep, a block, or "
     "lifting the offer (paying up). NOT a vs-history comparison; that is the "
     "separate Relative Premium axis."),
    ("convexity_score", "Convexity",
     "Whether this looks like a cheap, asymmetric, high-payoff bet (small "
     "risk, large potential). The detector's prize axis — but it is computed "
     "from the option chain and is NOT in our live data tier, so it reads '—' "
     "on live cards (populated only for the sample backtest)."),
    ("gamma_score", "Dealer Gamma (GEX)",
     "Whether market-makers are 'short gamma' near a flip — squeeze-prone, "
     "computed from the dealer GEX across the chain. Live (from UW). The Gamma "
     "& vol board shows the full per-name map (regime, flip, walls)."),
    ("price_confirmation_score", "Price Confirmation",
     "Did the underlying's spot move the SAME way as the bet around the print? "
     "Confirmation strengthens it. Live; defaults to neutral (0.5) when the "
     "move is flat or data is thin."),
    ("sector_confirmation_score", "Sector / Peer Flow",
     "Is similar flow showing up in the ticker's sector peers? Aligned peer "
     "flow suggests a theme. Live; neutral (0.5) when peers are quiet."),
    ("cluster_density_score", "Cluster / Sequence",
     "Is this print part of a cluster of related trades (a building sequence) "
     "or an isolated one-off? Clusters score higher. Live."),
    ("relative_premium_score", "Relative Premium",
     "Dollars-at-risk vs this ticker's typical trade size. Needs per-ticker "
     "median sizes, which are NOT wired live, so it reads '—' on live cards."),
    ("event_score", "Event Calendar",
     "Proximity to a scheduled catalyst (earnings/FDA). Live; defaults to "
     "neutral (0.3) when no catalyst is near — the common case."),
    ("time_of_day_weight", "Time of Day",
     "When in the session it fired. Prime-session prints weigh higher than "
     "open-auction or after-hours. Fully live — the one always-varying axis."),
]

# Tooltip glossary: term -> definition.
GLOSSARY: dict[str, str] = {
    "GEX": "Gamma Exposure — the aggregate dealer gamma across the option "
           "chain. Negative (dealers short gamma) means hedging chases the "
           "move; the 'flip' is where it crosses zero.",
    "ISO": "Intermarket Sweep Order — an aggressive order that sweeps "
           "multiple venues at once to fill fast. A marker of urgency / "
           "informed execution.",
    "DTE": "Days To Expiry — how long until the option expires. Short-dated "
           "(low DTE) options are the most leveraged / convex.",
    "sweep": "A trade executed aggressively across venues (often ISO) — the "
             "buyer wanted size now, not a better price.",
    "moneyness": "Spot price divided by strike. ~1.0 = at-the-money; >1 = "
                 "in-the-money call territory.",
    "max_r": "The position size the detector would assign, in units of risk "
             "(R). 0 = no position taken.",
    "combined score": "The weighted blend of all axes into one number. The "
                      "profile's threshold decides whether a print becomes a "
                      "flagged signal.",
    "fill side": "Where the trade printed vs the bid/ask. At/above ask = "
                 "aggressive buying; at/below bid = aggressive selling.",
}

# SignalLabel value -> one-line meaning (all 18 enum values). Unknown labels
# fall back to the prettified raw value via label_meaning().
LABEL_MEANINGS: dict[str, str] = {
    "HIGH_CONVICTION_SEQUENCE": "Top of the stack — strong, corroborated, part "
                                "of a building sequence.",
    "CONVEXITY_CLUSTER": "A cluster of convex, asymmetric bets — the prize "
                         "pattern.",
    "CONVEXITY_BURST": "A sudden burst of convex positioning.",
    "CONVEXITY_WATCH": "Convex-looking, but not yet corroborated — worth watching.",
    "SWEEP_UOA": "Aggressive sweep with unusual size.",
    "STANDARD_UOA": "Unusual activity, but without strong corroboration.",
    "PRE_CATALYST_FLOW": "Flow positioned ahead of a scheduled catalyst.",
    "SECTOR_FLOW_CLUSTER": "Part of a cluster of aligned flow across sector peers.",
    "CONFIRMED_OPENING_FLOW": "A new opening position, confirmed by OI change.",
    "OPENING_UNCONFIRMED": "Looks like an opening position, pending OI confirmation.",
    "OPTIONS_EQUITY_TAPE_CONFIRMATION": "Options flow confirmed by the equity tape.",
    "GAMMA_ACCELERATION_RISK": "Dealer-gamma setup that could accelerate a move.",
    "LEAP_POSITIONING": "Long-dated positioning (not the short-dated thesis).",
    "PENALIZED_BELOW_THRESHOLD": "Scored, but below the profile's bar — logged, "
                                 "not actioned.",
    "LIKELY_CLOSING_OR_NOISE": "Probably a closing trade or noise, not new conviction.",
    "POST_EVENT_NOISE": "Flow after the catalyst already passed — usually noise.",
    "IGNORE_NOISE": "Below the noise floor — ignore.",
    "REJECTED": "Rejected by a hard rule.",
}


def label_meaning(label: str) -> str:
    return LABEL_MEANINGS.get(label, label.replace("_", " ").title())


# Short phrase per label for the one-line plain-English card headline.
_LABEL_PHRASE: dict[str, str] = {
    "SWEEP_UOA": "aggressive sweep",
    "CONVEXITY_CLUSTER": "cluster of convex bets",
    "CONVEXITY_BURST": "burst of convex positioning",
    "PRE_CATALYST_FLOW": "positioning before a catalyst",
    "STANDARD_UOA": "unusual options activity",
    "LEAP_POSITIONING": "long-dated positioning",
    "HIGH_CONVICTION_SEQUENCE": "high-conviction sequence",
    "HIGH_CONVICTION_INITIAL": "high-conviction opener",
    "PENALIZED_BELOW_THRESHOLD": "flow below the action threshold",
    "OPENING_UNCONFIRMED": "an opening position",
    "IGNORE_NOISE": "background noise",
}


def headline(option_type: str, label: str, swept: bool, dte: int) -> str:
    """One plain-English sentence: what this card is, at a glance."""
    side = "Bullish call" if option_type == "call" else "Bearish put"
    phrase = _LABEL_PHRASE.get(label, label.replace("_", " ").lower())
    bits = [f"{side} — {phrase}"]
    if swept:
        bits.append("swept across venues (urgent)")
    bits.append("expires today" if dte == 0 else f"{dte} days to expiry")
    return " · ".join(bits)


def conviction(score: float | None) -> tuple[str, int]:
    """Map the combined score to a (tier word, 0-100 meter %) for display.

    Conviction = how strong / corroborated the signal is. Deliberately NOT a
    probability of profit — the backtest found no mechanical edge.
    """
    if score is None:
        return ("—", 0)
    pct = max(0, min(100, round(score / 0.5 * 100)))
    if score >= 0.40:
        return ("Strong", pct)
    if score >= 0.30:
        return ("Moderate", pct)
    if score >= 0.20:
        return ("Weak", pct)
    return ("Noise", pct)


def vol_structure(*, regime: str) -> str:
    """Defined-risk structure TEMPLATE (type, not strikes) for harvesting vol
    premium. Honesty: a template the user sizes/strikes — never a recommendation."""
    base = "~30 DTE, ~30Δ iron fly or credit spread — collect vol premium, defined risk"
    return base if regime == "long" else base + " (short-gamma: moves amplify, keep size small)"


def vol_read(*, iv_rank: int, earnings_in_window: bool) -> str:
    """Plain-English description of WHY this name is on the board. Descriptive."""
    rich = f"IV-rank {iv_rank} — implied vol is high in its own 1y range, so the vol here is rich to sell."
    earn = (" ⚠ Earnings inside the window — the high IV is a justified charge, not free premium."
            if earnings_in_window else " No earnings in the window.")
    return rich + earn


def vol_caveat() -> str:
    """Fixed honesty line shown on every vol-board row."""
    return ("The base vol premium is the tested edge; the gamma regime is context, "
            "not extra return. Size for a vol spike (fat left tail).")

