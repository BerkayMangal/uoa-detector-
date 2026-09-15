"""Frozen Turkish copy for the Alfa Board (Phase 5.2, contract §2).

Owner-mandated strings, byte for byte. Tests pin every constant. The board may
only show the texts defined here and in the other frozen dictionaries; it never
generates free text. Changing any of them is a contract change, not a copy edit.
"""

from __future__ import annotations

from typing import Final

# R-EV1: hover text on every evidence word.
EVIDENCE_HOVER: Final = "Kâr olasılığı DEĞİL."

# R-UN1: label on an unknown or out-of-scope evidence family.
UNKNOWN_NOT_CLEAN: Final = "bilgi yok, temiz demek değil"

# R-CA1: counter-argument lead and the fallback when no counter-argument is found.
COUNTER_LEAD: Final = "AMA"
NO_COUNTER_FOUND: Final = "Bariz bir karşı argüman bulunamadı — bu bir onay değildir"

# R-IV1: unsuppressible sentence wherever IV richness is shown.
IV_NOT_SELL_VOL: Final = (
    "Bu 'vol sat' demek DEĞİLDİR — o tez test edildi, "
    "maliyet ve örnek-dışı testten sonra ayakta kalmadı."
)

# R-EM1: first-class board state when no row qualifies as a clean candidate.
NO_CLEAN_CANDIDATE: Final = "Bugün temiz aday yok"

# Owner-named cost gate label (static UI chrome, not generated text).
GATE_LABEL: Final = "Alabileceklerimi göster"

# R-UN2: the strength label that is forbidden with too many unknowns.
STRONG_LABEL: Final = "Güçlü"
