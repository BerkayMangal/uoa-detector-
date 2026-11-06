"""Seal actual offline pilot aggregates; never reads raw archives or fits a rule."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from webapp.news_research import config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('refusing to overwrite sealed evidence')
    audit_path = args.input / 'independent-audit.json'
    audit = json.loads(audit_path.read_text())
    if audit.get('status') != 'PASS':
        parser.error('arithmetic/state audit must pass')
    rows = []
    for day in ('2026-11-04', '2026-11-05'):
        export_path, report_path = (args.input / (day + suffix) for suffix in ('-export.json', '-report.json'))
        export, report = json.loads(export_path.read_text()), json.loads(report_path.read_text())
        source, base = export['audit'], report['base']
        assert export['day'] == day and report['identity'] == source['dataset']['identity']
        assert export['profile_hash'] == report['profile_hash'] == config.profile_hash()
        assert export['implementation_hash'] == report['implementation_hash'] == config.implementation_hash()
        assert report['winner'] is None and report['inference'] == 'NOT_PERFORMED'
        rows.append({
            'version': 'fixed_news_daily_pilot_v1', 'day': day,
            'since': export['since'], 'as_of': export['as_of'],
            'profile_hash': export['profile_hash'], 'implementation_hash': export['implementation_hash'],
            'source_identity': report['identity'], 'source_scans': export['source_scans'],
            'status': report['status'],
            'source_characters': export['source_characters'], 'source_character_budget': export['source_character_budget'],
            'new_context_scans': source['new_context_scans'], 'legacy_scans': source['legacy_scans'],
            'opportunity_rows': len(source['dataset']['opportunities']),
            'base_true': sum(o['base'] is True for o in source['dataset']['opportunities']),
            'base_unknown': sum(o['base'] is None for o in source['dataset']['opportunities']),
            'roster_slots': report['roster_count'], 'price_rows': source['price_rows'],
            'stored_prices': len(source['dataset']['prices']),
            'complete_common_sessions': base['complete_common_sessions'],
            'eb_paired_days': len(base['paired_days']),
            'paired_difference_excess': base['total_paired_excess'],
            'unit_missing': dict(Counter(m for u in base['units'] for m in u['missing'])),
            'audit_missing': dict(source['missing']), 'arms': base['arms'],
            'stresses': [{'slippage_bps_per_side': s['slippage_bps_per_side'], 'arms': s['arms'],
                          'complete_common_sessions': s['complete_common_sessions'],
                          'eb_paired_days': len(s['paired_days'])} for s in (base, *report['stresses'])],
            'inference': 'NOT_PERFORMED', 'winner': None, 'new_fitted_trials': 0,
            'evaluated_arms': 5, 'cost_scenarios': 4, 'fixed_arm_cost_evaluations': 20,
            'source_export_sha256': hashlib.sha256(export_path.read_bytes()).hexdigest(),
            'frozen_report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest(),
            'arithmetic_audit_sha256': hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        })
    output = {'version': 'fixed_news_daily_pilot_v1', 'profile_hash': config.profile_hash(),
              'implementation_hash': config.implementation_hash(), 'rows': rows}
    raw = (json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    args.output.with_suffix('.sha256').write_text(hashlib.sha256(raw).hexdigest() + '\n')
    print(json.dumps({'days': len(rows), 'output': str(args.output), 'sha256': hashlib.sha256(raw).hexdigest()}))


if __name__ == '__main__':
    main()
