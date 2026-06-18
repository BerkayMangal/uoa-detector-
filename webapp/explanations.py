"""Plain-English explanations for the screener UI.

Every axis, term, and label gets a human explanation so the dashboard
teaches as it screens. This is content, not logic — kept in one place so
the wording is consistent and easy to edit.
"""

from __future__ import annotations

# One entry per StoredSignal sub-score, in display order. (key, title, what)
AXES: list[tuple[str, str, str]] = [
    ("uoa_score", "Unusual Options Activity",
     "How abnormal this options flow is versus the ticker's own norm. "
     "High = someone is doing something out of the ordinary here."),
    ("convexity_score", "Convexity",
     "Whether this looks like a cheap, asymmetric, high-payoff bet "
     "(small risk, large potential) — the kind of position informed "
     "traders use to express conviction. This is the detector's prize axis."),
    ("gamma_score", "Dealer Gamma (GEX)",
     "Are options market-makers positioned 'short gamma' near a flip point? "
     "If so, their hedging can amplify a move — a squeeze-prone setup. "
     "Computed from the whole option chain's open interest."),
    ("price_confirmation_score", "Price Confirmation",
     "Did the underlying's spot move in the SAME direction as the bet "
     "around the time of the print? Confirmation strengthens the signal; "
     "a contradiction weakens it."),
    ("sector_confirmation_score", "Sector / Peer Flow",
     "Is similar flow showing up in the ticker's sector peers? Aligned "
     "peer flow suggests a theme, not a one-off."),
    ("cluster_density_score", "Cluster / Sequence",
     "Is this print part of a cluster of related trades (a building "
     "sequence), or an isolated one-off? Clusters score higher."),
    ("relative_premium_score", "Relative Premium",
     "How large is the dollars-at-risk versus this ticker's typical trade? "
     "A print many times the median is what 'unusual size' means."),
    ("event_score", "Event Calendar",
     "Proximity to a scheduled catalyst (earnings). Flow right before a "
     "known event reads differently than flow in a quiet window."),
    ("time_of_day_weight", "Time of Day",
     "When in the session it fired. Prime-session prints are weighted "
     "higher than open-auction or after-hours noise."),
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

# SignalLabel value -> one-line meaning. Unknown labels fall back to the raw value.
LABEL_MEANINGS: dict[str, str] = {
    "HIGH_CONVICTION_SEQUENCE": "Top of the stack — strong, corroborated, "
                                "part of a building sequence.",
    "HIGH_CONVICTION_INITIAL": "Strong first print of a potential sequence.",
    "CONVEXITY_CLUSTER": "A cluster of convex, asymmetric bets — the prize "
                         "pattern.",
    "CONVEXITY_BURST": "A sudden burst of convex positioning.",
    "SWEEP_UOA": "Aggressive sweep with unusual size.",
    "STANDARD_UOA": "Unusual activity, but without strong corroboration.",
    "PRE_CATALYST_FLOW": "Flow positioned ahead of a scheduled catalyst.",
    "LEAP_POSITIONING": "Long-dated positioning (not the short-dated thesis).",
    "PENALIZED_BELOW_THRESHOLD": "Scored, but below the profile's bar — logged, "
                                 "not actioned.",
    "DISCARD_OR_LOG": "Below the noise floor.",
    "REJECTED": "Rejected by a hard rule.",
}


def label_meaning(label: str) -> str:
    return LABEL_MEANINGS.get(label, label.replace("_", " ").title())
