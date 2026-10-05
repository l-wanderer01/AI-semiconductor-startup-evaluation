"""Independent, versioned arithmetic oracle. No product scoring functions or LLM calls."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from .models import CheckCounts, RuleCheckResult

POLICY_FILE = Path(__file__).with_name('policies') / 'rules-v1.json'
DIMENSIONS = ('technology', 'market', 'business', 'team', 'risk', 'competition')
EVALUATIONS = {'technology': 'technical_evaluation', 'market': 'market_evaluation',
               'team': 'team_evaluation', 'risk': 'risk_analysis', 'competition': 'competition_evaluation'}


def load_spec():
    return json.loads(POLICY_FILE.read_text(encoding='utf-8'))


def check_rules(run_id, execution_path, data, *, settings=None, profiles=None,
                observed_weights=None, policy_version=None, evaluation_id=None):
    """Check raw decisions/ranking/report. Missing observations are errors, not passes.

    Profiles must be effective decision-node inputs (post enrichment), not seed data.
    Target agents checks are deliberately excluded until a compatible target is agreed.
    """
    spec = load_spec()
    version = policy_version or f'{execution_path}-baseline-v1'
    checks = []

    def add(name, expected, actual, *, company=None, status=None, reason=None):
        if status is None:
            equal = (math.isclose(expected, actual, rel_tol=0, abs_tol=spec['tolerance'])
                     if type(expected) in (int, float) and type(actual) in (int, float)
                     else expected == actual)
            status = 'pass' if equal else 'fail'
        checks.append(RuleCheckResult(run_id=run_id, evaluation_id=evaluation_id,
            execution_path=execution_path, policy_version=version,
            check_id=f'rule:{version}:{name}', company_id=company,
            evaluation_status='completed' if status in ('pass', 'fail') else status,
            verdict=status if status in ('pass', 'fail') else None,
            expected=expected, actual=actual, reason=reason or name))

    if version not in spec['policies'] or spec['policies'][version]['path'] != execution_path:
        add('policy', execution_path, version, status='error', reason='Unknown or incompatible policy version')
        return checks
    policy = copy.deepcopy(spec['policies'][version])
    settings, profiles = settings or {}, profiles or {}
    target = policy['mode'] == 'target_conformance'
    threshold = settings.get('recommendation_threshold' if execution_path == 'agents' else 'selective_dd_threshold', policy['threshold'])
    high = settings.get('high_priority_threshold', 80) if target else policy.get('high_priority', 80)
    top_k = settings.get('top_k', policy['top_k']) if execution_path == 'agents' else policy['top_k']
    try:
        if type(threshold) is not int or not policy['range'][0] <= threshold <= policy['range'][1]:
            raise ValueError('threshold outside policy scale')
        if type(top_k) is not int or top_k < 1:
            raise ValueError('top_k must be a positive integer')
        if target and (type(high) is not int or not 50 <= threshold <= high <= 100):
            raise ValueError('target grade thresholds must be ordered')
        if not isinstance(data, dict):
            raise ValueError('result is missing')
        rows = data.get('evaluations' if execution_path == 'agents' else 'decisions')
        if not isinstance(rows, list) or not rows:
            raise ValueError('no decisions to check')
        names = [r['company_name'] for r in rows]
        if any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
            raise ValueError('duplicate/invalid company identities')
    except (KeyError, TypeError, ValueError) as exc:
        add('input', 'valid complete policy input', None, status='error', reason=str(exc))
        return checks

    expected_scores = {}
    expected_grades = {}
    for row in rows:
        name = row['company_name']
        try:
            if execution_path == 'agents':
                signals = {key: row[key + '_score'] for key in DIMENSIONS}
            else:
                signals = {key: row[field]['score'] for key, field in EVALUATIONS.items()}
            valid = all(type(v) is int and 1 <= v <= 5 for v in signals.values())
            add('dimension_range', True, valid, company=name)
            if not valid:
                raise ValueError('dimension scores must be integers in 1..5')
            if execution_path == 'agents':
                expected = sum(signals.values())
                actual = row.get('total_score')
                add('stage_weights', None, None, company=name, status='not_applicable', reason='agents has no Stage weights; scale is 6-30 (#1)')
                add('risk_direction', 'higher_is_more_manageable', 'higher_is_more_manageable', company=name,
                    reason='Positive coefficient; evidence semantics are checked separately')
                add('grade', None, None, company=name, status='not_applicable', reason='agents does not define DD grades')
            else:
                stage = row['stage']
                weights = spec['weights'][stage]
                if observed_weights is None:
                    add('stage_weights', weights, None, company=name, status='error', reason='Observed Stage weight snapshot is unavailable')
                else:
                    add('stage_weights', weights, observed_weights.get(stage), company=name)
                    observed = observed_weights.get(stage, {})
                    add('weight_sum', 1.0, sum(observed.values()), company=name)
                profile = profiles.get(name)
                if profile is None:
                    raise ValueError('Effective decision input profile is unavailable')
                add('stage', stage, profile.get('stage'), company=name)
                raw = {k: profile[k + '_signal'] for k in ('technology', 'market', 'traction', 'team', 'risk', 'competition')}
                if not all(type(v) is int and 1 <= v <= 5 for v in raw.values()):
                    raise ValueError('effective signals must be integers in 1..5')
                contributions = {k: raw[k] / 5 * weights[k] * 100 for k in ('technology', 'team', 'competition')}
                contributions['risk'] = (6 - raw['risk']) / 5 * weights['risk'] * 100
                contributions['market'] = (raw['market'] / 5 * weights['market'] * 100 + raw['traction'] / 5 * weights['traction'] * 100
                    if target else round((raw['market'] + raw['traction']) / 2) / 5 * (weights['market'] + weights['traction']) * 100)
                add('risk_direction', 6 - raw['risk'], signals['risk'], company=name)
                for key in ('technology', 'team', 'competition'):
                    add('signal_' + key, raw[key], signals[key], company=name)
                add('signal_market', round((raw['market'] + raw['traction']) / 2), signals['market'], company=name,
                    reason='Merged display score; contribution independently checks #3')
                for key, value in contributions.items():
                    add('contribution_' + key, value, row[EVALUATIONS[key]].get('weighted_score'), company=name,
                        reason='Independent /5*weight*100; #3 uses separate market/traction weights in target mode')
                expected = round(sum(contributions.values()))
                actual = row.get('final_score')
                grade = 'High Priority DD' if expected >= high else 'Selective DD' if expected >= (threshold if target else 65) else 'Watchlist' if expected >= 50 else 'No DD'
                expected_grades[name] = grade
                add('grade', grade, row.get('recommendation'), company=name)
            add('total_range', True, type(actual) is int and policy['range'][0] <= actual <= policy['range'][1], company=name)
            add('score_formula', expected, actual, company=name)
            expected_scores[name] = expected
            add('risk_evidence_semantics', None, None, company=name, status='not_applicable',
                reason='Arithmetic does not determine risk evidence meaning; reviewed rubric is unresolved (#6)')
        except (KeyError, TypeError, ValueError) as exc:
            add('decision_input', 'complete decision and effective signals', None, company=name, status='error', reason=str(exc))

    if len(expected_scores) != len(rows):
        add('ranking_input', 'all scores independently recalculated', None, status='error', reason='Incomplete decisions prevent ranking oracle')
        return checks
    order = list(profiles) if execution_path == 'investment_pipeline' else data.get('companies', names)
    if not isinstance(order, list) or set(order) != set(names) or len(order) != len(names):
        add('candidate_order', names, order, status='error', reason='Candidate input order is required for deterministic ties')
        return checks
    ranked = sorted(order, key=lambda n: expected_scores[n], reverse=True)
    passed = [n for n in ranked if expected_scores[n] >= threshold]
    selected = passed[:top_k]
    branch = 'top3' if selected else 'hold'
    if execution_path == 'agents':
        add('decision_order', ranked, names)
        for field, expected in [('selected_companies', selected), ('hold_companies', [n for n in ranked if n not in selected])]:
            actual = data.get(field)
            add(field, expected, [r.get('company_name') for r in actual] if isinstance(actual, list) else actual)
        add('recommendation_route', branch, data.get('policy_decision'))
    else:
        ranking = data.get('ranking', {})
        if not isinstance(ranking, dict):
            add('ranking_input', 'ranking object', ranking, status='error', reason='Invalid ranking shape')
            ranking = {}
        for field, expected in [('passed_companies', passed), ('top_companies', selected), ('score_threshold', threshold),
                                ('high_priority_threshold', settings.get('high_priority_threshold', 80)), ('branch', branch),
                                ('watchlist_companies', [n for n in ranked if expected_grades[n] == 'Watchlist'])]:
            add('ranking_' + field, expected, ranking.get(field))
        add('recommendation_route', branch, data.get('branch'))
    report = data.get('final_report' if execution_path == 'agents' else 'report_markdown', '')
    _check_report(report, rows, expected_scores, expected_grades, add, execution_path, selected, threshold)
    return checks


def _check_report(report, rows, scores, grades, add, path, selected, threshold):
    if not isinstance(report, str) or not report.strip():
        add('report_numeric', 'nonempty report', None, status='error', reason='Report is missing')
        return
    # Parse the existing English table contract, never infer numbers from free prose.
    headers = None
    report_selected = []
    counts = {r['company_name']: 0 for r in rows}
    for line in report.splitlines():
        if not line.strip().startswith('|'):
            headers = None
            continue
        cells = [s.strip() for s in line.strip().strip('|').split('|')]
        if 'Company' in cells and ('Final Score' in cells or 'DD Score' in cells):
            headers = cells
            continue
        if headers is None or len(cells) != len(headers) or all(set(c) <= set('-: ') for c in cells):
            continue
        row = dict(zip(headers, cells))
        name = row['Company']
        if name not in counts:
            add('report_unknown_company', sorted(counts), name, status='fail')
            continue
        counts[name] += 1
        add(f'report_score_{counts[name]}', str(scores[name]), row.get('Final Score', row.get('DD Score')), company=name)
        if 'Rank' in row:
            report_selected.append(name)
            add(f'report_rank_{counts[name]}', str(len(report_selected)), row['Rank'], company=name)
        if 'Stage' in row:
            source_stage = next(r for r in rows if r['company_name'] == name).get('stage')
            add(f'report_stage_{counts[name]}', source_stage, row['Stage'], company=name)
        if 'Recommendation' in row and name in grades:
            add(f'report_grade_{counts[name]}', grades[name], row['Recommendation'], company=name)
        source = next(r for r in rows if r['company_name'] == name)
        if path == 'investment_pipeline':
            for title, key in [('Technology', 'technology'), ('Market', 'market'), ('Team', 'team'), ('Competition', 'competition'), ('Risk', 'risk')]:
                if title in row:
                    add(f'report_dimension_{title}_{counts[name]}', str(source[EVALUATIONS[key]]['score']), row[title], company=name)
    for name, count in counts.items():
        add('report_numeric_coverage', True if count else None, bool(count) if count else None, company=name,
            status=None if count else ('not_applicable' if path == 'agents' else 'error'),
            reason='Checked explicit numeric tables' if count else 'No supported numeric table; prose is not treated as numeric evidence')
    if path == 'investment_pipeline':
        add('report_top3_order', selected, report_selected,
            reason='Explicit Rank/Company/DD Score table must match independently selected Top3')
        displayed_thresholds = re.findall(r'`(\d+)`점 이상', report)
        add('report_threshold', [str(threshold)] * len(displayed_thresholds), displayed_thresholds,
            status=None if displayed_thresholds else 'not_applicable',
            reason='Explicit threshold markers checked; unsupported prose is not parsed')


def summarize(checks):
    """K = pass / applicable; errors keep K undefined, N/A excluded."""
    groups = {}
    for version in sorted({c.policy_version for c in checks}):
        rows = [c for c in checks if c.policy_version == version]
        statuses = [c.status for c in rows]
        counts = CheckCounts(applicable=len(rows) - statuses.count('not_applicable'),
            passed=statuses.count('pass'), failed=statuses.count('fail'),
            error=statuses.count('error'), missing=statuses.count('missing'),
            pending=statuses.count('pending'), not_applicable=statuses.count('not_applicable'))
        groups[version] = {'counts': counts.model_dump(mode='json'), 'K': counts.pass_rate,
                           'mode': load_spec()['policies'].get(version, {}).get('mode'),
                           'score_scale': load_spec()['policies'].get(version, {}).get('scale'),
                           'meaning': 'policy conformance, not investment success prediction'}
    return groups


def check_recorded_run(recorder, result):
    """Read recorded effective profiles and observed settings, preserving invocation lineage."""
    profiles = {}
    invocations = {}
    for record in recorder.records:
        if record.operation_id == 'decision' and record.status == 'succeeded':
            payload = json.loads(Path(record.input_snapshot.path).read_text())['data']
            profile = payload['state']['selected_company_context_state']
            profiles[profile['name']] = profile
            invocations[profile['name']] = record.invocation_id
    settings = json.loads((recorder.directory / 'settings_snapshot.json').read_text())['data']
    observed_file = recorder.directory / 'scoring_policy_observed.json'
    observed = json.loads(observed_file.read_text())['data'] if observed_file.exists() else {}
    settings.update(observed.get('settings', {}))
    versions = [f'{recorder.path}-baseline-v1']
    if recorder.path == 'investment_pipeline':
        versions.append('investment_pipeline-target-v1')
    from .recording import json_value
    checks = []
    for version in versions:
        checks.extend(check_rules(recorder.run_id, recorder.path, json_value(result), settings=settings,
            profiles=profiles, observed_weights=observed.get('weights'), policy_version=version))
    for check in checks:
        check.invocation_id = invocations.get(check.company_id)
    summary = {'schema_version': 'rule-conformance-v1', 'run_id': recorder.run_id,
               'policies': summarize(checks), 'policy_sha256': hashlib.sha256(POLICY_FILE.read_bytes()).hexdigest(),
               'known_discrepancies': load_spec()['known_discrepancies'],
               'unresolved_target_policies': [1, 2, 4, 6],
               'ragas_combined': False}
    summary['source_snapshots'] = [
        {'path': str(recorder.directory / filename),
         'sha256': hashlib.sha256((recorder.directory / filename).read_bytes()).hexdigest()}
        for filename in ('settings_snapshot.json', 'decisions.json', 'scoring_policy_observed.json', 'report_original.md')
        if (recorder.directory / filename).is_file()
    ]
    return checks, summary
