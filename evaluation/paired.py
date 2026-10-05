"""Immutable baseline receipts and quality-first paired reports (#29).

RAGAS macro scores have no fabricated claim denominator. Custom factual counts
alone support claim-weighted statistics. Workflow success is a separate gate.
"""
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

from .experiment import evidence_content
from .models import EvaluationManifest, RunManifest
from .reevaluation import comparison
from .storage import _redact_value
from .validation import validate_run

PRIMARY = ('factual_precision', 'factual_recall', 'faithfulness')
DIAGNOSTIC = ('context_precision', 'context_recall')


def read(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


def unwrap(path, default=None):
    value = read(path, default)
    return value.get('data', value) if isinstance(value, dict) else value


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        relative = path.relative_to(directory)
        if relative.parts[0] == 'evaluations' or path.name == '.writer.lock':
            continue
        if path.is_symlink():
            raise ValueError('baseline contains symlink: ' + str(relative))
        if path.is_file():
            result[str(relative)] = file_hash(path)
    return result


def preserve(directory, *, instrumentation_only=False, note=None):
    directory = Path(directory).resolve()
    if not (directory / '.frozen').is_file():
        raise ValueError('baseline must be frozen before preservation')
    errors = validate_run(directory)
    if errors:
        raise ValueError('baseline integrity: ' + '; '.join(errors))
    manifest = RunManifest.model_validate_json((directory / 'run.json').read_text())
    if instrumentation_only and not (note or '').strip():
        raise ValueError('instrumentation-only capture requires a note and preserved diff')
    return _redact_value({'schema_version': 'baseline-receipt-v1', 'run_id': manifest.run_id,
        'directory': str(directory), 'commit_sha': manifest.commit_sha, 'dirty': manifest.dirty,
        'dirty_diff_sha256': manifest.dirty_diff_sha256, 'instrumentation_only': instrumentation_only,
        'note': note, 'files': inventory(directory),
        'generation_status': manifest.generation_status.value,
        'workflow_status': manifest.workflow_status.value})


def verify_receipt(receipt):
    if receipt.get('schema_version') != 'baseline-receipt-v1':
        raise ValueError('unsupported baseline receipt')
    directory = Path(receipt['directory']).resolve()
    if inventory(directory) != receipt['files']:
        raise ValueError('preserved generation baseline changed')
    return directory


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def delta(before, after, *, rate=False):
    return None if before is None or after is None else (after - before) * (100 if rate else 1)


def sample_key(sample):
    return tuple(sample.get(k) for k in ('case_id', 'logical_role', 'company_id', 'section_id',
                                         'stage', 'mode', 'attempt'))


def index_samples(directory):
    package = unwrap(directory / 'stage_package.json', {})
    samples = package.get('samples', [])
    if not samples:
        raise ValueError('paired reporting requires preserved #22 stage packages with case/role identity')
    indexed = {}
    for sample in samples:
        key = sample_key(sample)
        if key in indexed:
            raise ValueError('ambiguous sample pair; case/role/company/section/stage/mode/attempt must be unique')
        indexed[key] = sample
    return indexed, package


def sample_values(directory, sample):
    sid = sample['sample_id']
    metrics = {r['metric_name']: r for r in rows(directory / 'ragas_results.jsonl') if r['sample_id'] == sid}
    values = {}
    statuses = {}
    for name in PRIMARY + DIAGNOSTIC:
        record = metrics.get(name, {})
        statuses[name] = record.get('evaluation_status', 'unsupported' if name in sample.get('unsupported_metric_reasons', {}) else 'missing')
        values[name] = record.get('value') if statuses[name] == 'completed' else None
    diagnostics = [r.get('data', r) for r in rows(directory / 'grounding_diagnostics.jsonl')]
    diagnostic = next((r for r in diagnostics if r['sample_id'] == sid), {})
    for name in DIAGNOSTIC:
        search_results = [r[name] for r in diagnostic.get('searches', []) if r.get(name)]
        if search_results:
            statuses[name] = 'completed' if all(r['evaluation_status'] == 'completed' and r.get('value') is not None
                                               for r in search_results) else 'partial'
            values[name] = mean(r.get('value') for r in search_results) if statuses[name] == 'completed' else None
    factual = next((r for r in unwrap(directory / 'custom_factual_metrics.json', [])
                    if r['scope'] == 'sample' and r['key'] == sid), {})
    counts = factual.get('counts', {})
    values.update(claim_count=counts.get('total'), contradicted_count=counts.get('contradicted'),
                  unverifiable_count=counts.get('unverifiable'),
                  contradiction_rate=factual.get('custom_contradicted_over_n'),
                  unverifiable_rate=factual.get('custom_unverifiable_over_n'),
                  judgment_completion=ratio(counts.get('verified', 0) + counts.get('contradicted', 0)
                                            + counts.get('unverifiable', 0), counts.get('total', 0)),
                  critical_contradicted_count=len(factual['critical_contradicted_claim_ids']) if factual else None,
                  critical_unverifiable_count=len(factual['critical_unverifiable_claim_ids']) if factual else None,
                  critical_errors=len(factual['critical_contradicted_claim_ids']) + len(factual['critical_unverifiable_claim_ids'])
                      if factual else None)
    required = next((r for r in unwrap(directory / 'required_information_metrics.json', [])
                     if r['scope'] == 'sample' and r['key'] == sid), {})
    values.update(required_information_coverage=required.get('required_information_coverage'),
                  missing_information_count=len(required['missing_item_ids']) if required else None,
                  incorrect_information_count=len(required['incorrect_item_ids']) if required else None)
    statuses.update(sample=sample.get('evaluation_status', 'missing'),
                    custom_factual=factual.get('status', 'missing'), required_information=required.get('status', 'missing'))
    run_dir = directory.parent.parent
    checks = read(run_dir / 'checks.json', [])
    company_checks = [r for r in checks if r['check_id'].startswith('rule:')
                      and r.get('company_id') in {None, sample.get('company_id')}]
    applicable = [r for r in company_checks if r.get('status') != 'not_applicable']
    finished = all(r.get('status') in {'pass', 'fail'} for r in applicable)
    values['rule_conformance'] = ratio(sum(r.get('status') == 'pass' for r in applicable), len(applicable)) if finished else None
    values['rule_error_count'] = sum(r.get('status') == 'fail' for r in applicable) if finished and applicable else None
    statuses['rule_conformance'] = 'completed' if finished and applicable else 'missing'
    return {'values': values, 'statuses': statuses, 'counts': counts,
            'errors': {k: factual.get(k, []) for k in ('critical_contradicted_claim_ids', 'critical_unverifiable_claim_ids', 'unjudged_claim_ids')},
            'missing_item_ids': required.get('missing_item_ids', []),
            'provenance': {k:sample.get(k) for k in ('input_sha256', 'reference_sha256', 'evidence_ids',
                'input_snapshot', 'response_location', 'invocation_id', 'source_locations')},
            'details': str(directory), 'sample_id': sid, 'search_diagnostics': diagnostic}


def workflow(directory):
    manifest = RunManifest.model_validate_json((directory / 'run.json').read_text())
    checks = read(directory / 'checks.json', [])
    spec = read(directory / 'workflow.json', {})
    calls = rows(directory / 'invocations.jsonl')
    research = read(directory / 'additional_research.json', [])
    success = manifest.execution_succeeded and bool(checks) and all(
        c.get('status') in {'pass', 'not_applicable'} for c in checks)
    return {'run_id': manifest.run_id, 'successful_runs': int(success), 'attempted_runs': 1,
        'success_rate': int(success), 'generation_status': manifest.generation_status.value,
        'workflow_status': manifest.workflow_status.value, 'manifest': str(directory / 'workflow.json'),
        'manifest_sha256': file_hash(directory / 'workflow.json'),
        'required_companies': spec.get('candidate_company_ids', []),
        'coverage_checks': [c for c in checks if c['check_id'].startswith(('coverage_', 'decision_company_coverage'))],
        'checks': checks, 'call_statuses': dict(Counter(c['status'] for c in calls)),
        'retries': sum(c.get('link_reason') == 'retry' for c in calls),
        'fallback_used': manifest.fallback_used, 'fallback_reasons': manifest.fallback_reasons,
        'unsupported_steps': [s for s in spec.get('steps', []) if not s['supported']],
        'additional_research': [{**r, 'success': bool(r.get('search_invocation_ids') and r.get('new_evidence_ids')
            and r.get('state_update') and r.get('reevaluation_invocation_id') and r.get('termination_reason')),
            'score_increase_required': False, 'no_new_evidence_policy': spec.get('no_new_evidence_policy')}
            for r in research]}


def runtime_groups(directories, *, evaluation=False):
    groups = defaultdict(list)
    for directory in directories:
        run = read(directory.parent.parent / 'run.json') if evaluation else read(directory / 'run.json')
        metrics = read(directory / 'metrics.json', {})
        for purpose in ('ragas_runtime', 'custom_runtime') if evaluation else ('generation_runtime',):
            runtime = metrics.get(purpose)
            if runtime is None:
                continue
            key = (purpose, runtime['status'], runtime.get('cache_state', 'unknown'), run['fallback_used'],
                   runtime.get('currency'), tuple(sorted({r.get('price_version') or 'unknown' for r in runtime.get('usage', [])})),
                   run['commit_sha'], run.get('settings_sha256'), run['execution_path'],
                   read(directory / 'evaluation.json')['evaluator']['configuration_sha256'] if evaluation else None)
            groups[key].append(runtime)
    result = []
    for key, items in sorted(groups.items(), key=lambda row: str(row[0])):
        times = [r['duration_seconds'] for r in items if r.get('duration_seconds') is not None]
        costs = [r['cost'] for r in items if r.get('cost') is not None]
        result.append({'purpose': key[0], 'status': key[1], 'cache_state': key[2], 'fallback_used': key[3],
            'currency': key[4], 'price_version': key[5], 'repetitions': len(items),
            'commit_sha': key[6], 'settings_sha256': key[7], 'execution_path': key[8],
            'evaluator_configuration_sha256': key[9],
            'observed_duration_count': len(times), 'median_seconds': statistics.median(times) if times else None,
            'p95_seconds': sorted(times)[math.ceil(.95*len(times))-1] if len(times) >= 20 else None,
            'p95_minimum_samples': 20, 'known_cost_count': len(costs),
            'median_cost': statistics.median(costs) if len(costs) == len(items) else None})
    return result


def aggregates(records):
    """Per-stage company/section macro scores and custom-only micro counts."""
    output = []
    for stage, role in sorted({(r['key'][4], r['key'][1]) for r in records}):
        selected = [r for r in records if (r['key'][4], r['key'][1]) == (stage, role)]
        for side in ('baseline', 'after'):
            company_groups = defaultdict(list)
            for record in selected:
                if record[side]:
                    company_groups[(record['key'][2], record['key'][3])].append(record[side])
            macros = []
            company_claims = Counter()
            totals = Counter()
            denominator_complete = bool(selected) and all(r[side] and r[side]['counts']
                and r[side]['statuses']['custom_factual'] in {'completed', 'not_applicable'} for r in selected)
            for (company, section), samples in company_groups.items():
                macros.append({'company': company, 'section': section, 'sample_count': len(samples),
                    'metrics': {name: mean(s['values'][name] for s in samples) for name in PRIMARY + DIAGNOSTIC},
                    'metric_status_counts': {name: dict(Counter(s['statuses'][name] for s in samples)) for name in PRIMARY + DIAGNOSTIC},
                    'scored_sample_counts': {name: sum(s['values'][name] is not None for s in samples) for name in PRIMARY + DIAGNOSTIC}})
                for sample in samples:
                    count = sample['counts']
                    company_claims[company] += count.get('total', 0)
                    for name in ('total', 'verified', 'contradicted', 'unverifiable', 'pending'):
                        totals[name] += count.get(name, 0)
            total = totals['total']
            output.append({'stage': stage, 'logical_role': role, 'side': side, 'sample_count': len(selected),
                'observed_sample_count': sum(bool(r[side]) for r in selected), 'company_section_means': macros,
                'company_section_macro': {name: mean(m['metrics'][name] for m in macros) for name in PRIMARY + DIAGNOSTIC},
                'ragas_claim_weighted': None, 'ragas_claim_weighted_reason': 'native raw claim counts unavailable',
                'custom_counts': dict(totals), 'denominator_complete': denominator_complete,
                'custom_claim_weighted': {name: ratio(totals[name], total) if denominator_complete else None
                                          for name in ('verified', 'contradicted', 'unverifiable')},
                'company_claim_share': {str(k): ratio(v, total) for k, v in company_claims.items()},
                'claim_concentration_warning': bool(len(company_claims) > 1 and total and max(company_claims.values(), default=0) / total > .5)})
    return output


def paired_report(baseline, after, *, data_mode, baseline_receipt=None):
    if data_mode not in {'frozen', 'live'}:
        raise ValueError('data_mode must be frozen or live')
    baseline, after = Path(baseline).resolve(), Path(after).resolve()
    scope = comparison(baseline, after)
    dirs = [baseline.parent.parent, after.parent.parent]
    if baseline_receipt:
        if verify_receipt(baseline_receipt) != dirs[0]:
            raise ValueError('receipt does not identify this baseline')
    manifests = [RunManifest.model_validate_json((d / 'run.json').read_text()) for d in dirs]
    for d in dirs:
        if not (d / '.frozen').is_file() or validate_run(d):
            raise ValueError('generation integrity or freeze check failed')
        experiment = unwrap(d / 'experiment.json', {})
        if experiment and experiment.get('data_mode') != data_mode:
            raise ValueError('recorded frozen/live mode differs from requested comparison mode')
    actual_input_changed = manifests[0].input_snapshot.sha256 != manifests[1].input_snapshot.sha256
    actual_evidence_changed = evidence_content(dirs[0]) != evidence_content(dirs[1])
    scope.update(source_input_changed=actual_input_changed, source_evidence_changed=actual_evidence_changed)
    for evaluation_dir, generation in zip((baseline, after), manifests, strict=True):
        evaluation = EvaluationManifest.model_validate_json((evaluation_dir / 'evaluation.json').read_text())
        if (evaluation.input_sha256, evaluation.evidence_sha256) != (generation.input_snapshot.sha256, generation.evidence_snapshot.sha256):
            raise ValueError('evaluation does not identify preserved generation input/evidence')
    scope['evaluation_completion'] = {s: {k:read(d / 'evaluation.json')[k] for k in ('sample_count', 'completed_sample_count')}
                                      for s, d in zip(('baseline', 'after'), (baseline, after))}
    if data_mode == 'frozen' and (actual_input_changed or actual_evidence_changed):
        raise ValueError('frozen comparison requires identical input/evidence snapshots')
    indexes, packages = zip(*(index_samples(d) for d in (baseline, after)), strict=True)
    for field in ('role_mapping_version', 'transformation_version', 'extractor'):
        if packages[0].get(field) != packages[1].get(field):
            raise ValueError('sample preparation changed; prepare BOTH sides with the same ' + field)
    for package, generation in zip(packages, manifests):
        if package.get('run_id') != generation.run_id:
            raise ValueError('stage package generation identity mismatch')
    records = []
    for key in sorted(set(indexes[0]) | set(indexes[1]), key=str):
        samples = [index.get(key) for index in indexes]
        if all(samples):
            if samples[0].get('reference_sha256') != samples[1].get('reference_sha256'):
                raise ValueError('sample reference changed; re-evaluate BOTH runs')
            if scope['mode'] == 'isolated' and samples[0].get('input_sha256') != samples[1].get('input_sha256'):
                raise ValueError('isolated agent input changed')
        values = [sample_values(d, s) if s else None for d, s in zip((baseline, after), samples, strict=True)]
        names = values[0]['values'] if values[0] else values[1]['values']
        diffs = {name: delta(values[0]['values'][name], values[1]['values'][name],
                            rate=not name.endswith(('count', 'errors'))) if all(values) else None for name in names}
        records.append({'key': list(key), 'pair_status': 'paired' if all(samples) else 'unpaired',
            'baseline': values[0], 'after': values[1], 'difference': diffs,
            'input_changed': samples[0].get('input_sha256') != samples[1].get('input_sha256') if all(samples) else None})
    workflows = [workflow(d) for d in dirs]
    changes = {name: [getattr(m, name) for m in manifests] for name in
               ('commit_sha', 'execution_path', 'policy_version', 'workflow_version', 'settings_sha256', 'generation_model', 'prompts')
               if getattr(manifests[0], name) != getattr(manifests[1], name)}
    changes = {k: [v.model_dump(mode='json') if hasattr(v, 'model_dump') else
                  [p.model_dump(mode='json') for p in v] if isinstance(v, list) else v for v in values]
               for k, values in changes.items()}
    complete = scope['complete'] and all(r['pair_status'] == 'paired' and all(
        r[s]['values'][n] is not None for s in ('baseline', 'after') for n in PRIMARY)
        and all(r[s]['statuses'][n] == 'completed' for s in ('baseline', 'after')
                for n in ('custom_factual', 'required_information', 'rule_conformance')) for r in records) and bool(records)
    regressions = [r['key'] for r in records if any(v is not None and
        (v < 0 if name in PRIMARY + ('required_information_coverage',) else v > 0)
        for name, v in r['difference'].items() if name in PRIMARY +
        ('required_information_coverage', 'critical_errors', 'missing_information_count', 'incorrect_information_count',
         'contradicted_count', 'unverifiable_count', 'contradiction_rate', 'unverifiable_rate', 'rule_error_count'))]
    axes = {}
    for name in PRIMARY + ('required_information_coverage', 'critical_errors', 'missing_information_count',
                          'incorrect_information_count', 'contradiction_rate', 'unverifiable_rate', 'rule_error_count'):
        diffs = [r['difference'][name] for r in records]
        higher_is_better = name in PRIMARY + ('required_information_coverage',)
        axes[name] = 'incomplete' if not diffs or any(d is None for d in diffs) else (
            'regressed' if any(d < 0 if higher_is_better else d > 0 for d in diffs) else
            'improved' if any(d > 0 if higher_is_better else d < 0 for d in diffs) else 'unchanged')
    return {'schema_version': 'quality-paired-report-v1', 'scope': scope, 'data_mode': data_mode,
        'evaluation_files': {s: {p.name: file_hash(p) for p in d.iterdir() if p.is_file() and p.name != '.writer.lock'}
                             for s, d in zip(('baseline', 'after'), (baseline, after), strict=True)},
        'generation_changes': changes, 'samples': records, 'aggregates': aggregates(records),
        'quality': {'complete': complete, 'regression_samples': regressions,
            'independent_axes': axes,
            'verdict': 'incomplete' if not complete else 'regressed' if regressions else 'no_detected_regression',
            'overall_agent_success': complete and not regressions and all(w['successful_runs'] for w in workflows),
            'weighted_composite_score': None},
        'workflow': dict(zip(('baseline', 'after'), workflows, strict=True)),
        'stage_coverage': {s: p.get('coverage', []) for s, p in zip(('baseline', 'after'), packages, strict=True)},
        'generation_auxiliary': {s: runtime_groups([d]) for s, d in zip(('baseline', 'after'), dirs, strict=True)},
        'evaluation_operations': {s: runtime_groups([d], evaluation=True) for s, d in zip(('baseline', 'after'), (baseline, after), strict=True)},
        'error_propagation_candidates': {s: {'links': p.get('claim_links', p.get('links', [])),
            'fact_assessments': rows(d / 'fact_assessments.jsonl'), 'details': str(d / 'fact_assessments.jsonl'),
            'claims': [{k: c.get(k) for k in ('claim_id', 'error_origin_candidate', 'origin_reason', 'upstream_claim_ids')}
                       for c in p.get('claims', [])], 'causality_confirmed': False}
            for s, p, d in zip(('baseline', 'after'), packages, (baseline, after), strict=True)}}


def campaign_report(attempts):
    """All attempted generation runs form the success denominator, even without an evaluation."""
    if not attempts:
        raise ValueError('campaign must include every attempted repetition')
    result = {'schema_version': 'paired-campaign-v1', 'attempts': [], 'groups': {}}
    seen = set()
    generation_ids = set()
    modes = defaultdict(list)
    for attempt in attempts:
        if attempt['attempt_id'] in seen:
            raise ValueError('duplicate campaign attempt ID')
        seen.add(attempt['attempt_id'])
        if attempt['data_mode'] not in {'frozen', 'live'}:
            raise ValueError('invalid campaign data_mode')
        baseline_run, after_run = [Path(attempt[k]).resolve() for k in ('baseline_run', 'after_run')]
        if baseline_run == after_run or any(d in generation_ids for d in (baseline_run, after_run)):
            raise ValueError('campaign repetitions must identify distinct generation attempts')
        generation_ids.update((baseline_run, after_run))
        receipt = read(Path(attempt['baseline_receipt']))
        if verify_receipt(receipt) != baseline_run:
            raise ValueError('campaign baseline receipt mismatch')
        for run in (baseline_run, after_run):
            if not (run / '.frozen').is_file() or validate_run(run):
                raise ValueError('campaign generation integrity failed')
        row = {'attempt_id': attempt['attempt_id'], 'data_mode': attempt['data_mode'],
               'baseline_run': str(baseline_run), 'after_run': str(after_run),
               'workflow': {s: workflow(d) for s, d in zip(('baseline', 'after'), (baseline_run, after_run))}}
        if attempt.get('baseline_evaluation') and attempt.get('after_evaluation'):
            for evaluation, run in zip((attempt['baseline_evaluation'], attempt['after_evaluation']), (baseline_run, after_run)):
                if Path(evaluation).resolve().parent.parent != run:
                    raise ValueError('campaign evaluation/run mismatch')
            row['comparison'] = paired_report(attempt['baseline_evaluation'], attempt['after_evaluation'],
                data_mode=attempt['data_mode'], baseline_receipt=receipt)
        else:
            row.update(comparison=None, evaluation_status='missing', reason=attempt.get('reason', 'evaluation incomplete or generation failed'))
        modes[attempt['data_mode']].append((attempt, row))
        result['attempts'].append(row)
    for mode, group in modes.items():
        result['groups'][mode] = {'attempted_pairs': len(group),
            'evaluated_pairs': sum(r['comparison'] is not None for _, r in group),
            'workflow': {s: {'successful_runs': sum(r['workflow'][s]['successful_runs'] for _, r in group),
                'attempted_runs': len(group), 'success_rate': sum(r['workflow'][s]['successful_runs'] for _, r in group)/len(group)}
                for s in ('baseline', 'after')},
            'generation_auxiliary': {s: runtime_groups([Path(a[s+'_run']).resolve() for a, _ in group]) for s in ('baseline', 'after')},
            'evaluation_operations': {s: runtime_groups([Path(a[s+'_evaluation']).resolve() for a, _ in group if a.get(s+'_evaluation')], evaluation=True)
                                      for s in ('baseline', 'after')}}
    return result


def render_markdown(report):
    lines = ['# 품질 중심 전후 비교', '', f"자료 모드: {report['data_mode']} / 평가 모드: {report['scope']['mode']}",
             f"품질 판정: {report['quality']['verdict']} / 전체 Agent 성공: {report['quality']['overall_agent_success']}",
             '', '비율 차이는 %p, 건수 차이는 건. 미완료·missing·error·N/A는 null로 보존한다.',
             'RAGAS claim-weighted 평균은 raw claim counts 부재로 계산하지 않는다.', '']
    lines += ['독립 품질 판정: ' + json.dumps(report['quality']['independent_axes'], ensure_ascii=False), '']
    for title, names in [('주 지표 / 보완 지표', PRIMARY + ('contradiction_rate', 'unverifiable_rate',
        'critical_errors', 'critical_contradicted_count', 'critical_unverifiable_count', 'claim_count', 'judgment_completion', 'required_information_coverage',
        'missing_information_count', 'incorrect_information_count', 'rule_conformance', 'rule_error_count')), ('검색 진단', DIAGNOSTIC)]:
        lines += ['## ' + title, '', '| case / 역할 / 기업 / 영역 / 단계 / 모드 / 회차 | pair | ' + ' | '.join(names) + ' |',
                  '|---|---|' + '|'.join('---' for _ in names) + '|']
        for row in report['samples']:
            cells = []
            for name in names:
                b = row['baseline']['values'][name] if row['baseline'] else None
                a = row['after']['values'][name] if row['after'] else None
                cells.append(f'{b} → {a} (Δ {row["difference"][name]})')
            label = ' / '.join(str(v).replace('|', '\\|').replace('\n', ' ') for v in row['key'])
            lines.append('| ' + label + ' | ' + row['pair_status'] + ' | ' + ' | '.join(cells) + ' |')
    for title, key in [('기업·영역 평균과 전체 주장 합산', 'aggregates'), ('워크플로우 성공·coverage·조사 효과', 'workflow'), ('Agent 미지원·실패·skip', 'stage_coverage'),
        ('생성 비용·시간 (보조)', 'generation_auxiliary'), ('RAGAS·custom 평가 운영 비용·시간', 'evaluation_operations'),
        ('실행 경로·정책·모델 변화', 'generation_changes'), ('오류 전파 후보 (확정 인과 아님)', 'error_propagation_candidates')]:
        lines += ['', '## ' + title, '', '```json', json.dumps(report[key], ensure_ascii=False, indent=2), '```']
    lines += ['', '## 상태와 오류 상세', '']
    for row in report['samples']:
        for side in ('baseline', 'after'):
            item = row[side]
            if item:
                lines.append(f'- {side} {item["sample_id"]}: [{item["details"]}]({item["details"]}); '
                             + json.dumps({'statuses': item['statuses'], 'errors': item['errors'],
                                           'missing_item_ids': item['missing_item_ids']}, ensure_ascii=False))
    return '\n'.join(lines) + '\n'


def write_report(report, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    report = _redact_value(report)
    (directory / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    (directory / 'comparison.md').write_text(render_markdown(report))
    with (directory / 'comparison.csv').open('w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['case_role_company_section_stage_mode_attempt', 'pair_status', 'metric', 'baseline', 'after', 'difference', 'baseline_statuses', 'after_statuses'])
        for row in report['samples']:
            for metric, diff in row['difference'].items():
                writer.writerow([json.dumps(row['key'], ensure_ascii=False), row['pair_status'], metric,
                    row['baseline']['values'][metric] if row['baseline'] else '',
                    row['after']['values'][metric] if row['after'] else '', diff,
                    json.dumps(row['baseline']['statuses']) if row['baseline'] else 'unpaired',
                    json.dumps(row['after']['statuses']) if row['after'] else 'unpaired'])
    return directory
