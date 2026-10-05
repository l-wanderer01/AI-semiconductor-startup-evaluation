"""#27 frozen 자동 결과를 참조하는 append-only 사람 검토와 표본 성능.

리뷰는 run/reviews/<evaluation_id>.jsonl에 저장하므로 기존 evaluation 동결을 유지한다.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .models import HumanReviewRecord, Identifier, RecordModel, Sha256
from .recording import digest
from .storage import EvaluationStorage, serialize_payload

THREE_STATE = ['verified', 'contradicted', 'unverifiable']
STRATA = ('number', 'currency', 'unit', 'date', 'technology', 'commercial_status')


def strata(text):
    import re
    patterns = {
        'number': r'\d', 'currency': r'원|억|달러|USD|KRW|\$|€',
        'unit': r'TOPS|TFLOPS|\bW\b|와트|nm|나노|배|%',
        'date': r'\d{4}년|\d{4}-\d{2}|\d+월|분기',
        'technology': r'INT8|FP16|NPU|GPU|HBM|ResNet|TOPS|TFLOPS|공정|반도체',
        'commercial_status': r'PoC|계약|양산|검증|유료|고객',
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)] or ['other']


class ReviewTarget(RecordModel):
    evaluation_id: Identifier
    target_id: Identifier
    target_kind: Literal['claim', 'metric', 'extraction']
    group: Identifier
    label_space: list[str | bool] = Field(default_factory=list)
    automatic_result: dict
    automatic_label: str | bool | None = None
    source_sha256: Sha256
    critical: bool = False
    strata: list[str] = Field(default_factory=list)


class ReviewDecision(HumanReviewRecord):
    schema_version: Literal['review-v1'] = 'review-v1'
    status: Literal['proposed', 'confirmed', 'adjudicated']
    provenance: Literal['human', 'synthetic_fixture']
    source_sha256: Sha256
    # continuous metrics: {score: float|null, error: bool, error_type: str, ...}; no class threshold.
    resolves_review_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_resolution(self):
        if self.status == 'adjudicated' and len(set(self.resolves_review_ids)) < 2:
            raise ValueError('adjudication requires at least two conflicting review IDs')
        if self.status != 'adjudicated' and self.resolves_review_ids:
            raise ValueError('only adjudication resolves conflicting reviews')
        return self


def rows(path):
    if not path.exists():
        return []
    if path.is_symlink():
        raise ValueError('symlink sources are not allowed')
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def inventory(directory: Path):
    """자동 JSONL 원행·source hash에 연결한다. 없는 파일에서 결과를 만들지 않는다."""
    from .models import EvaluationManifest
    directory = directory.resolve()
    manifest = EvaluationManifest.model_validate_json((directory / 'evaluation.json').read_text())
    if (manifest.run_id, manifest.evaluation_id) != (directory.parent.parent.name, directory.name):
        raise ValueError('evaluation manifest/path identity mismatch')
    if not (directory / '.frozen').is_file():
        raise ValueError('human review requires a terminal frozen evaluation')
    claims = {r['claim_id']: r for r in rows(directory / 'claims.jsonl')}
    samples = {r['sample_id']: r for r in rows(directory / 'ragas_samples.jsonl')}
    targets = []
    for filename in ('fact_assessments.jsonl', 'grounding_assessments.jsonl', 'ragas_results.jsonl'):
        source = directory / filename
        raw_rows = rows(source)
        source_hash = digest(source.read_bytes()) if source.exists() else None
        for row in raw_rows:
            if row['evaluation_id'] != manifest.evaluation_id:
                raise ValueError('automatic result evaluation scope mismatch')
            if filename == 'ragas_results.jsonl':
                group = row['metric_name']
                target_id = row['sample_id'] + '.' + group
                text = samples.get(row['sample_id'], {}).get('response') or ''
                label, labels, critical, kind = None, [], False, 'metric'
            else:
                group = 'custom_factual' if filename.startswith('fact_') else row['basis']
                target_id = row['claim_id'] + '.' + group
                claim = claims.get(row['claim_id'], {})
                text = claim.get('atomic_statement', '')
                critical, kind = claim.get('critical', row.get('critical', False)), 'claim'
                label = row.get('verdict') if group == 'custom_factual' else row.get('grounded')
                labels = THREE_STATE if group == 'custom_factual' else [True, False]
            targets.append(ReviewTarget(evaluation_id=manifest.evaluation_id, target_id=target_id,
                target_kind=kind, group=group, label_space=labels, automatic_result=row,
                automatic_label=label, source_sha256=source_hash, critical=critical, strata=strata(text)))
    package_path = directory / 'stage_package.json'
    if package_path.exists():
        if package_path.is_symlink():
            raise ValueError('symlink stage package')
        package = json.loads(package_path.read_text())['data']
        for audit in package['extractions']:
            unit = audit['unit']
            extracted = [a['candidate'] for a in package['atoms'] if a['unit_id'] == unit['unit_id']]
            targets.append(ReviewTarget(evaluation_id=manifest.evaluation_id,
                target_id=unit['unit_id'] + '.extraction', target_kind='extraction', group='custom_extraction',
                source_sha256=digest(package_path.read_bytes()), strata=strata(unit['text']),
                automatic_result={'evaluation_status':audit['status'], 'unit':unit, 'extracted':extracted}))
    if len({t.target_id for t in targets}) != len(targets):
        raise ValueError('duplicate review target')
    return targets


def review_plan(targets, *, seed='ko-review-v1', per_stratum=2):
    """핵심 사실 전수 + metric/stratum 층별 고정 hash 표본. 전체 정확도 추정이 아니다."""
    if per_stratum < 1:
        raise ValueError('per_stratum must be positive')
    chosen = {t.target_id for t in targets if t.critical}
    groups = defaultdict(list)
    for target in targets:
        for tag in target.strata:
            groups[(target.group, tag)].append(target)
    for group in sorted(groups):
        ordered = sorted(groups[group], key=lambda t: digest((seed + t.target_id).encode()))
        chosen.update(t.target_id for t in ordered[:per_stratum])
    return {'policy_version': 'ko-review-v1', 'seed': seed, 'per_stratum': per_stratum,
            'scope': 'critical census plus stratified sample; not population accuracy',
            'missing_strata': sorted(set(STRATA) - {s for t in targets for s in t.strata}),
            'targets': [t.model_dump(mode='json') for t in sorted(targets, key=lambda t: t.target_id)
                        if t.target_id in chosen]}


class ReviewStore:
    def __init__(self, evaluation_directory):
        self.directory = Path(evaluation_directory).absolute()
        if self.directory.is_symlink() or any(p.is_symlink() for p in self.directory.parents):
            # macOS /tmp can resolve to /private/tmp; callers should pass resolved paths.
            raise ValueError('use canonical review paths without symlinks')
        self.directory = self.directory.resolve()
        self.run = self.directory.parent.parent
        self.storage = EvaluationStorage(self.run.parent)
        if self.directory.parent.name != 'evaluations':
            raise ValueError('review requires run/evaluations/<evaluation_id>')
        self.storage._validate_component(self.run.name)
        self.storage._validate_component(self.directory.name)
        self.ledger = self.run / 'reviews' / (self.directory.name + '.jsonl')

    def read(self):
        if self.ledger.parent.is_symlink() or self.ledger.is_symlink():
            raise ValueError('symlink review ledger')
        return [ReviewDecision.model_validate(r) for r in rows(self.ledger)]

    def append(self, decision):
        decision = ReviewDecision.model_validate(decision)
        targets = {t.target_id: t for t in inventory(self.directory)}
        target = targets.get(decision.target_id)
        if target is None or decision.evaluation_id != self.directory.name or decision.target_kind != target.target_kind:
            raise ValueError('unknown or out-of-scope review target')
        if decision.source_sha256 != target.source_sha256 or decision.automatic_result != target.automatic_result:
            raise ValueError('review must preserve exact automatic result and source hash')
        if target.label_space:
            if not any(type(decision.human_result) is type(label) and decision.human_result == label
                       for label in target.label_space):
                raise ValueError('human result must use the declared label space')
        elif target.target_kind == 'extraction':
            from .extraction_labels import compare_labels
            result = decision.human_result
            if not isinstance(result, dict) or not isinstance(result.get('facts'), list) or not isinstance(result.get('kinds'), list):
                raise ValueError('extraction review requires facts/kinds lists from the original unit')
            if not all(isinstance(s, str) and s.strip() for s in result['facts']) or not all(k in {'fact', 'opinion', 'mixed', 'score', 'decision'} for k in result['kinds']):
                raise ValueError('invalid extraction labels')
            compare_labels(result, target.automatic_result['extracted'])
        else:
            result = decision.human_result
            if not isinstance(result, dict) or type(result.get('error')) is not bool:
                raise ValueError('metric review requires an explicit error boolean and optional score')
            score = result.get('score')
            if score is not None and (type(score) not in (float, int) or not 0 <= score <= 1):
                raise ValueError('continuous review score outside 0..1')
        with self.storage._directory_lock(self.run):
            history = self.read()
            if any(r.review_id == decision.review_id for r in history):
                raise ValueError('duplicate review_id')
            previous = [r for r in history if r.target_id == decision.target_id]
            last = previous[-1] if previous else None
            if any(r.provenance != decision.provenance for r in previous):
                raise ValueError('cannot mix synthetic and human review provenance for one target')
            if decision.previous_review_id != (last.review_id if last else None):
                raise ValueError('review must link to latest revision')
            if last and decision.reviewed_at < last.reviewed_at:
                raise ValueError('review timestamps cannot move backwards')
            if decision.status == 'adjudicated':
                latest_votes = {r.reviewer:r for r in previous if r.status != 'proposed'}
                if not set(decision.resolves_review_ids).issuperset(r.review_id for r in latest_votes.values()):
                    raise ValueError('adjudication must resolve current reviewer opinions, not stale revisions')
                resolved = [r for r in previous if r.review_id in decision.resolves_review_ids]
                if len(resolved) != len(set(decision.resolves_review_ids)) or len({r.reviewer for r in resolved}) < 2:
                    raise ValueError('adjudication requires independent reviewers of the same target')
                if len({json.dumps(r.human_result, sort_keys=True) for r in resolved}) < 2:
                    raise ValueError('adjudication requires an actual disagreement')
                if decision.reviewer in {r.reviewer for r in resolved}:
                    raise ValueError('adjudicator must be an independent third reviewer')
                if any(r.provenance != decision.provenance or r.status == 'proposed' for r in resolved):
                    raise ValueError('adjudication requires confirmed reviews with matching provenance')
            if self.ledger.parent.is_symlink():
                raise ValueError('symlink review directory')
            self.ledger.parent.mkdir(exist_ok=True)
            # Logical append, atomic publication; every old row remains byte-for-byte.
            existing = self.ledger.read_bytes() if self.ledger.exists() else b''
            if existing and not existing.endswith(b'\n'):
                raise ValueError('incomplete review ledger')
            self.storage._atomic_write(self.ledger,
                existing + (serialize_payload(decision, pretty=False) + '\n').encode(), replace=True)
        return self.ledger


def confirmed_reviews(targets, history):
    """사람 간 미조정 불일치는 gold에서 제외하고 최신 수정 이력을 보존한다."""
    target_ids = {t.target_id for t in targets}
    grouped = defaultdict(list)
    for review in history:
        if review.target_id not in target_ids:
            raise ValueError('unknown target in review history')
        target = next(t for t in targets if t.target_id == review.target_id)
        if review.evaluation_id != target.evaluation_id or review.target_kind != target.target_kind:
            raise ValueError('review history scope mismatch')
        if review.automatic_result != target.automatic_result or review.source_sha256 != target.source_sha256:
            raise ValueError('automatic source changed after review')
        grouped[review.target_id].append(review)
    final, conflicts = {}, []
    for target_id, reviews in grouped.items():
        latest = reviews[-1]
        if latest.status == 'adjudicated':
            final[target_id] = latest
            continue
        # A correction replaces that reviewer's opinion, not another person's opinion.
        last_adjudication = next((i for i in range(len(reviews)-1, -1, -1)
                                  if reviews[i].status == 'adjudicated'), None)
        active = reviews[last_adjudication:] if last_adjudication is not None else reviews
        by_reviewer = {r.reviewer: r for r in active}
        eligible = [r for r in by_reviewer.values() if r.status in {'confirmed', 'adjudicated'}]
        if len({json.dumps(r.human_result, sort_keys=True) for r in eligible}) > 1:
            conflicts.append(target_id)
        elif eligible:
            final[target_id] = eligible[-1]
    return final, sorted(conflicts)


def reliability_report(targets, history, *, plan=None):
    final, conflicts = confirmed_reviews(targets, history)
    groups = defaultdict(list)
    for t in targets:
        groups[t.group].append(t)
    summaries = {}
    def completed(target):
        return target.automatic_result['evaluation_status'] == 'completed' and (
            target.target_kind == 'extraction' or
            (target.automatic_label is not None if target.label_space else target.automatic_result.get('value') is not None))
    for group, members in sorted(groups.items()):
        applicable = [t for t in members if t.automatic_result['evaluation_status'] != 'not_applicable']
        reviewed = [(t, final[t.target_id]) for t in members if t.target_id in final]
        paired = [(t, r) for t, r in reviewed if completed(t)]
        real = [(t, r) for t, r in paired if r.provenance == 'human']
        summary = {'total': len(members), 'applicable': len(applicable), 'reviewed': len(reviewed), 'paired': len(paired),
                   'review_completion_rate': len(reviewed) / len(members),
                   'automatic_completion_rate': sum(completed(t) for t in applicable) / len(applicable) if applicable else None,
                   'statuses': {s: sum(t.automatic_result['evaluation_status'] == s for t in members)
                                for s in ('completed', 'missing', 'error', 'pending', 'not_applicable')},
                   'validated_accuracy': None,
                   'reason': 'no population accuracy estimate; confidence is not accuracy',
                   'synthetic_pair_count': sum(r.provenance == 'synthetic_fixture' for _, r in paired)}
        # Human-only statistics never combine synthetic oracles with real reviews.
        summary['human_pair_count'] = len(real)
        if members[0].target_kind == 'extraction':
            from .extraction_labels import compare_labels
            summary['extraction_diagnostics'] = [
                {'target_id': t.target_id, 'provenance': r.provenance, 'review_id': r.review_id,
                 **compare_labels(r.human_result, t.automatic_result['extracted'])} for t, r in reviewed]
            summary['reason'] = 'exact statement/kind omissions and unexpected atoms; inspect semantic equivalence manually'
        elif members[0].label_space:
            labels = members[0].label_space
            matrix = {str(g): {str(p): 0 for p in labels} for g in labels}
            for target, review in real:
                matrix[str(review.human_result)][str(target.automatic_label)] += 1
            performance = {}
            for label in labels:
                key = str(label)
                tp = matrix[key][key]
                predicted = sum(matrix[str(g)][key] for g in labels)
                actual = sum(matrix[key].values())
                performance[key] = {'precision': tp / predicted if predicted else None,
                                    'recall': tp / actual if actual else None,
                                    'support': actual}
            summary.update(confusion_matrix=matrix, matrix_axes='rows=human, columns=automatic',
                           per_label=performance, sample_agreement=sum(matrix[str(l)][str(l)] for l in labels) / len(real) if real else None)
        else:
            summary.update(raw_metric_reviews=[{'target_id': t.target_id,
                'automatic_score': t.automatic_result.get('value'), 'human_result': r.human_result,
                'review_id': r.review_id, 'reason': r.reason} for t, r in reviewed],
                metric_error_count=sum(r.human_result['error'] for _, r in real),
                confusion_matrix=None, per_label=None)
        summaries[group] = summary
    if plan:
        planned_ids = {t['target_id'] for t in plan['targets']}
        if not planned_ids.issubset({t.target_id for t in targets}):
            raise ValueError('plan contains unknown targets')
        expected = {t.target_id: t for t in targets}
        if any(ReviewTarget.model_validate(t) != expected[t['target_id']] for t in plan['targets']):
            raise ValueError('review plan source/configuration changed')
    else:
        planned_ids = {t.target_id for t in targets}
    return {'schema_version': 'reliability-v1', 'groups': summaries,
            'conflicting_target_ids': conflicts, 'planned_count': len(planned_ids),
            'unreviewed_planned_ids': sorted(planned_ids - set(final)),
            'critical_unreviewed_ids': sorted(t.target_id for t in targets if t.critical and t.target_id not in final),
            'strata_review_counts': {tag: sum(tag in t.strata and t.target_id in final for t in targets)
                                     for tag in STRATA},
            'confidence_is_validated_accuracy': False}
