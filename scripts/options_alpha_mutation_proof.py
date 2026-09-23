"""Prove the options-alpha guards can actually fail, one mutation at a time.

    uv run python scripts/options_alpha_mutation_proof.py

A green test suite is not evidence that a guard protects anything. This project has
repeatedly caught guards that could never go red -- a job no registry held, a test
that built the composition it asserted, and inside this very scope an
ambiguity branch that tested a daily CLOSE against two thresholds the close could
never straddle. So each critical guard is broken on purpose and the named test that
claims to protect it must go red for the right reason.

The procedure, per mutation:

  1. hash the file's bytes
  2. assert the anchor appears EXACTLY once, so the mutation lands where intended
  3. write the mutated file
  4. run the one named test
  5. restore the original bytes and verify the hash is identical

A mutation the test SURVIVES is the important outcome: it is a finding about the
TEST, not a note to file away, and the test must be replaced rather than the result
explained.

Two things this deliberately does not do. It does not delete lines to create
mutations that fail on a SyntaxError -- a file that cannot parse proves nothing
about a test. And it does not clear __pycache__: no such rule exists in this
repository, and rewriting a file changes its mtime, which is what Python actually
consults.

Writes one evidence artifact. Spends no quota.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/options-alpha-v1/mutation_proof.md"

STRUCTURES = "src/uoa_detector/options_alpha/structures.py"
CARD = "src/uoa_detector/options_alpha/card.py"
EXITS = "src/uoa_detector/options_alpha/exits.py"

T_STRUCT = "tests/unit/test_options_alpha_structures.py"
T_CARD = "tests/unit/test_options_alpha_card.py"
T_EXITS = "tests/unit/test_options_alpha_exits.py"


@dataclass(frozen=True)
class Mutation:
    guard: str
    path: str
    old: str
    new: str
    test_file: str
    test_name: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        guard="Long girisi ask'ten fiyatlanir",
        path=STRUCTURES,
        old='_side(leg.quote, costs.entry_long_side, "giris")',
        new='_side(leg.quote, costs.exit_long_side, "giris")',
        test_file=T_STRUCT,
        test_name="test_entry_reads_the_ask_and_never_the_bid",
    ),
    Mutation(
        guard="Caprazlanmis kotasyon reddedilir",
        path=STRUCTURES,
        old="if leg.quote.is_crossed:",
        new="if False:",
        test_file=T_STRUCT,
        test_name="test_a_crossed_quote_is_refused_rather_than_priced",
    ),
    Mutation(
        guard="Butceye sigmayan yapi sifir adettir",
        path=STRUCTURES,
        old="count = int(budget // risk_each)",
        new="count = max(1, int(budget // risk_each))",
        test_file=T_STRUCT,
        test_name="test_one_structure_over_budget_sizes_to_zero_and_says_what_it_needs",
    ),
    Mutation(
        guard="Hedef yapinin tavanini asamaz",
        path=CARD,
        old=(
            "target_usd = structure_ceiling if target_capped and structure_ceiling "
            "is not None else raw_target"
        ),
        new="target_usd = raw_target",
        test_file=T_CARD,
        test_name="test_a_target_can_never_exceed_the_structures_own_ceiling",
    ),
    Mutation(
        guard="Replay karti giris olarak sunulmaz",
        path=CARD,
        old=(
            "    status = (\n"
            "        OpportunityStatus.WATCH\n"
            "        if data_origin is DataOrigin.REPLAY\n"
            "        else OpportunityStatus.PAPER_ENTRY_READY\n"
            "    )"
        ),
        new="    status = OpportunityStatus.PAPER_ENTRY_READY",
        test_file=T_CARD,
        test_name="test_a_replay_card_is_never_offered_as_an_entry",
    ),
    Mutation(
        # The plain stop branch assigns a byte-identical line, so the anchor has to
        # carry the `ambiguous = True` above it or the mutation lands in the wrong
        # branch and proves nothing.
        guard="Ayni gun iki esik: sira uydurulmaz, stop uygulanir",
        path=EXITS,
        old=(
            "            ambiguous = True\n"
            "            exit_day, exit_value, reason = observation.day, stop_level, "
            "ExitReason.STOP"
        ),
        new=(
            "            ambiguous = True\n"
            "            exit_day, exit_value, reason = observation.day, target_level, "
            "ExitReason.TARGET"
        ),
        test_file=T_EXITS,
        test_name="test_both_levels_inside_one_day_is_ambiguous_and_takes_the_stop",
    ),
    Mutation(
        guard="Fiyatlanamayan pozisyon basarili islem sayilmaz",
        path=EXITS,
        old="reason = ExitReason.NO_EXIT_DATA",
        new="reason = ExitReason.TIME",
        test_file=T_EXITS,
        test_name="test_no_priceable_day_is_not_a_successful_trade",
    ),
)


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_test(test_file: str, test_name: str) -> tuple[bool, str]:
    """(passed, tail of output). Keys are stripped so no test can reach the vendor."""
    env = dict(os.environ)
    for key in ("UNUSUAL_WHALES_API_KEY", "THETADATA_API_KEY", "THETADATA_USERNAME"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-k", test_name, "-q", "--no-header"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = (result.stdout or result.stderr).strip().splitlines()
    return result.returncode == 0, tail[-1] if tail else ""


def main() -> int:
    rows: list[dict[str, object]] = []
    survivors: list[str] = []

    for mutation in MUTATIONS:
        path = ROOT / mutation.path
        original = path.read_bytes()
        before = hashlib.sha256(original).hexdigest()
        text = original.decode("utf-8")

        occurrences = text.count(mutation.old)
        if occurrences != 1:
            rows.append(
                {
                    "guard": mutation.guard,
                    "test": mutation.test_name,
                    "outcome": f"CAPA ESLESMEDI ({occurrences} kez)",
                    "detail": "mutasyon uygulanmadi",
                }
            )
            survivors.append(f"{mutation.guard}: capa {occurrences} kez bulundu")
            continue

        # Baseline first: a test that is already red proves nothing about the guard.
        baseline_ok, baseline_line = run_test(mutation.test_file, mutation.test_name)
        if not baseline_ok:
            rows.append(
                {
                    "guard": mutation.guard,
                    "test": mutation.test_name,
                    "outcome": "TABAN KIRMIZI",
                    "detail": baseline_line,
                }
            )
            survivors.append(f"{mutation.guard}: test mutasyonsuz da kirmizi")
            continue

        path.write_text(text.replace(mutation.old, mutation.new), encoding="utf-8")
        try:
            passed, line = run_test(mutation.test_file, mutation.test_name)
        finally:
            path.write_bytes(original)

        after = sha256(path)
        restored = after == before
        if passed:
            survivors.append(f"{mutation.guard}: {mutation.test_name} mutasyondan SAG CIKTI")

        rows.append(
            {
                "guard": mutation.guard,
                "test": mutation.test_name,
                "outcome": "test KIRMIZI (koruma gercek)" if not passed else "SAG KALDI (test zayif)",
                "detail": line,
                "restored": restored,
            }
        )
        print(
            f"  {'OK ' if not passed else 'ZAYIF'}  {mutation.guard:52s} "
            f"{'geri alindi' if restored else 'GERI ALINAMADI'}"
        )

    lines = [
        "# options-alpha-v1 — mutasyon kanıtı",
        "",
        f"**Koşum:** {datetime.now(UTC).isoformat()}",
        "",
        "Her koruma kasten bozuldu ve adı konmuş testin kırmızıya dönmesi beklendi.",
        "Mutasyondan **sağ çıkan** bir test, testin kendisi hakkında bir bulgudur.",
        "",
        "| Koruma | Adı konmuş test | Sonuç | Dosya geri alındı |",
        "|---|---|---|---|",
    ]
    for row in rows:
        restored = row.get("restored")
        mark = "evet" if restored else ("—" if restored is None else "**HAYIR**")
        lines.append(f"| {row['guard']} | `{row['test']}` | {row['outcome']} | {mark} |")

    lines += ["", f"**Toplam:** {len(MUTATIONS)} mutasyon, **{len(survivors)}** sorunlu.", ""]
    if survivors:
        lines.append("## Sorunlu olanlar")
        lines += [f"- {s}" for s in survivors]
    else:
        lines.append(
            "Tüm korumalar mutasyonla kırmızıya döndü ve her dosya bayt-birebir geri alındı."
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{len(MUTATIONS)} mutasyon, {len(survivors)} sorunlu -> {OUT}")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
