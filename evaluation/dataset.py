"""고정 데이터셋 검증·불변 버전 발행·reference 변환 및 실제 run 연결.

작성 기록: Codex가 #21 평가 기반을 구현했다. Agent 판단 로직은 호출하거나 변경하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .dataset_models import FixedDataset, RunCaseBinding
from .models import RagasSampleInput, RunManifest


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def dataset_hash(dataset: FixedDataset) -> str:
    """자기 자신의 해시 필드만 제외한다. 사실·검토·정책·파일 목록 모두 해시에 포함한다."""
    value = dataset.model_dump(mode='json')
    del value['manifest']['sha256']
    return sha256(canonical_bytes(value))


def safe_path(root: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('dataset snapshot must use a relative path: ' + name)
    root = root.resolve()
    target = root / path
    if any((root / Path(*path.parts[:i])).is_symlink() for i in range(1, len(path.parts) + 1)):
        raise ValueError('symlink snapshot: ' + name)
    if not target.resolve().is_relative_to(root):
        raise ValueError('snapshot outside dataset: ' + name)
    return target


def reference_payload(dataset: FixedDataset, case_id: str) -> dict:
    case = next(c for c in dataset.manifest.cases if c.case_id == case_id)
    facts = {f.reference_id: f for f in dataset.facts}
    selected = [facts[r] for r in case.reference_ids if facts[r].review_status == 'verified']
    return {
        'case_id': case.case_id, 'company_id': case.company_id, 'section_id': case.section_id,
        'as_of_date': case.as_of_date.isoformat(),
        'text': '\n'.join(f.statement for f in selected),
        'facts': [f.model_dump(mode='json') for f in selected],
        'excluded_reference_ids': [r for r in case.reference_ids if facts[r].review_status != 'verified'],
    }


def validate_dataset(directory: Path, *, require_ready=False, allow_synthetic=False) -> list[str]:
    """내용 해시뿐 아니라 출처 구간·기업/영역/시점·상호 ID·검토 범위를 확인한다."""
    directory = directory.resolve()
    errors = []
    try:
        dataset = FixedDataset.model_validate_json((directory / 'dataset.json').read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        return ['dataset schema: ' + str(exc)]
    if dataset_hash(dataset) != dataset.manifest.sha256:
        errors.append('dataset hash mismatch')
    for name, digest in dataset.inventory.items():
        try:
            if sha256(safe_path(directory, name).read_bytes()) != digest:
                errors.append('inventory hash mismatch: ' + name)
        except (ValueError, OSError) as exc:
            errors.append(str(exc))

    def snapshot(ref):
        if dataset.inventory.get(ref.path) != ref.sha256:
            errors.append('snapshot absent/different in inventory: ' + ref.path)
        try:
            content = safe_path(directory, ref.path).read_bytes()
            if sha256(content) != ref.sha256:
                errors.append('snapshot hash mismatch: ' + ref.path)
            return content.decode('utf-8')
        except (ValueError, OSError, UnicodeError) as exc:
            errors.append(str(exc))
            return ''

    def indexed(rows, key):
        result = {getattr(row, key): row for row in rows}
        if len(result) != len(rows):
            errors.append('duplicate ' + key)
        return result

    sources = indexed(dataset.sources, 'source_id')
    facts = indexed(dataset.facts, 'reference_id')
    indexed(dataset.facts, 'fact_id')
    items = indexed(dataset.required_items, 'required_item_id')
    reviews = indexed(dataset.case_reviews, 'case_id')
    case_ids = {c.case_id for c in dataset.manifest.cases}
    if set(reviews) != case_ids:
        errors.append('case review IDs differ from cases')
    actual_reviewers = sorted({r.reviewer for r in dataset.case_reviews if r.completeness != 'pending'} |
                             {f.reviewer for f in dataset.facts if f.review_status != 'pending'})
    if sorted(dataset.manifest.reviewers) != actual_reviewers:
        errors.append('manifest reviewers differ from actual review records')
    for source in sources.values():
        snapshot(source.snapshot)
        synthetic = source.provenance == 'synthetic_fixture'
        if synthetic != (source.snapshot_kind == 'synthetic_fixture'):
            errors.append('source snapshot kind/provenance mismatch')
        if dataset.purpose == 'baseline' and (synthetic or source.url is None):
            errors.append('baseline requires original primary sources')
    for fact in facts.values():
        if fact.company_id is None or not fact.source_ids or not fact.source_locations:
            errors.append('fact company/source/location missing: ' + fact.fact_id)
        if len(set(fact.source_ids)) != len(fact.source_ids):
            errors.append('duplicate fact source: ' + fact.fact_id)
        expected_paths = set()
        for source_id in fact.source_ids:
            source = sources.get(source_id)
            if source is None:
                errors.append('unknown fact source: ' + source_id)
                continue
            expected_paths.add(source.snapshot.path)
            if fact.company_id not in source.company_ids:
                errors.append('source company mismatch: ' + fact.fact_id)
            if source.published_date > fact.as_of_date or fact.source_as_of_date > fact.as_of_date:
                errors.append('future source/fact date: ' + fact.fact_id)
        for location in fact.source_locations:
            text = snapshot(location.snapshot)
            if location.snapshot.path not in expected_paths:
                errors.append('quote not from linked source: ' + fact.fact_id)
            if not location.quote or location.end_offset > len(text) or text[location.start_offset:location.end_offset] != location.quote:
                errors.append('original quote/offset mismatch: ' + fact.fact_id)
        for item_id in fact.required_item_ids:
            item = items.get(item_id)
            if item is None or fact.reference_id not in item.reference_ids:
                errors.append('fact/required item linkage mismatch: ' + fact.fact_id)
        if dataset.purpose == 'baseline' and fact.review_status == 'verified' and fact.review_kind != 'human':
            errors.append('baseline reference requires human review: ' + fact.fact_id)
    for item in items.values():
        if item.version != dataset.manifest.version:
            errors.append('required item version differs from dataset')
        for ref in item.reference_ids:
            fact = facts.get(ref)
            if fact is None or (fact.company_id, fact.section_id) != (item.company_id, item.section_id):
                errors.append('required item reference scope mismatch: ' + item.required_item_id)
            elif item.required_item_id not in fact.required_item_ids:
                errors.append('required item/fact linkage mismatch: ' + item.required_item_id)
    for case in dataset.manifest.cases:
        if len(set(case.reference_ids)) != len(case.reference_ids) or len(set(case.required_item_ids)) != len(case.required_item_ids):
            errors.append('duplicate case reference/required item ID: ' + case.case_id)
        review = reviews.get(case.case_id)
        if review is None:
            continue
        for ref in case.reference_ids:
            fact = facts.get(ref)
            if fact is None or (fact.company_id, fact.section_id, fact.as_of_date) != (case.company_id, case.section_id, case.as_of_date):
                errors.append('case reference scope mismatch: ' + case.case_id)
        for item_id in case.required_item_ids:
            item = items.get(item_id)
            if item is None or (item.company_id, item.section_id) != (case.company_id, case.section_id):
                errors.append('case required item scope mismatch: ' + case.case_id)
            elif not set(item.reference_ids).issubset(case.reference_ids):
                errors.append('case omits required item reference: ' + case.case_id)
        evidence_text = snapshot(case.evidence_snapshot)
        try:
            evidence_ids = json.loads(evidence_text)['source_ids']
            if not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
                errors.append('empty/duplicate evidence source IDs: ' + case.case_id)
            for source_id in evidence_ids:
                source = sources.get(source_id)
                if source is None or case.company_id not in source.company_ids or source.published_date > case.as_of_date:
                    errors.append('case evidence scope mismatch: ' + case.case_id)
        except (ValueError, KeyError, TypeError):
            errors.append('invalid evidence snapshot: ' + case.case_id)
            evidence_ids = []
        for source_id in review.relevant_source_ids:
            if source_id not in evidence_ids:
                errors.append('retrieval relevant source absent from evidence: ' + case.case_id)
        try:
            payload = reference_payload(dataset, case.case_id)
            if json.loads(snapshot(case.reference_snapshot)) != payload:
                errors.append('reference snapshot differs from verified facts: ' + case.case_id)
            if any(m.value.startswith('factual_') or m.value.startswith('context_') for m in case.supported_metrics) and not payload['text'] and (require_ready or dataset.status == 'released'):
                errors.append('reference metric requires verified facts: ' + case.case_id)
        except (KeyError, ValueError):
            errors.append('invalid reference snapshot: ' + case.case_id)
        if any(m.value.startswith('context_') for m in case.supported_metrics) and not review.relevant_source_ids:
            errors.append('retrieval benchmark missing relevant sources: ' + case.case_id)
        fact_ids = {facts[r].fact_id for r in case.reference_ids if r in facts}
        for assertion in review.expected_assertions:
            if not set(assertion.fact_ids).issubset(fact_ids):
                errors.append('expected assertion references another case: ' + case.case_id)
        if require_ready or dataset.status == 'released':
            if review.completeness != 'complete':
                errors.append('case completeness not approved: ' + case.case_id)
            if dataset.purpose == 'baseline' and review.review_kind != 'human':
                errors.append('baseline case requires human review: ' + case.case_id)
            if any(facts[r].review_status != 'verified' for r in case.reference_ids if r in facts):
                errors.append('case contains unverified reference: ' + case.case_id)
    if require_ready:
        if dataset.status != 'released':
            errors.append('dataset is draft; publish a reviewed version before scoring')
        if dataset.purpose == 'synthetic_fixture' and not allow_synthetic:
            errors.append('synthetic fixtures cannot be used as baseline data')
    return errors


def load_dataset(directory: Path, *, require_ready=False, allow_synthetic=False) -> FixedDataset:
    errors = validate_dataset(directory, require_ready=require_ready, allow_synthetic=allow_synthetic)
    if errors:
        raise ValueError('\n'.join(errors))
    return FixedDataset.model_validate_json((directory / 'dataset.json').read_text(encoding='utf-8'))


def publish_dataset(dataset: FixedDataset, files: dict[str, bytes], parent: Path) -> Path:
    """검토된 입력에서 새 버전을 발행한다. 존재하는 버전은 절대로 덮어쓰지 않는다."""
    for component in (dataset.manifest.dataset_id, dataset.manifest.version):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', component) or component in {'.', '..'}:
            raise ValueError('invalid dataset ID/version')
    if 'dataset.json' in files:
        raise ValueError('dataset.json is reserved')
    dataset = FixedDataset.model_validate(dataset.model_dump(mode='json'))
    dataset.inventory = {name: sha256(content) for name, content in sorted(files.items())}
    dataset.manifest.sha256 = dataset_hash(dataset)
    parent = parent.resolve() / dataset.manifest.dataset_id
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / dataset.manifest.version
    # mkdir로 병렬 발행을 막는다. 검증 완료 전에는 dataset.json을 공개하지 않는다.
    destination.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix='dataset-', dir=parent) as temporary:
            staging = Path(temporary)
            for name, content in files.items():
                path = safe_path(staging, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (staging / 'dataset.json').write_text(json.dumps(dataset.model_dump(mode='json'), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            load_dataset(staging, require_ready=dataset.status == 'released', allow_synthetic=True)
            for path in sorted(staging.rglob('*')):
                if path.is_file() and path.name != 'dataset.json':
                    target = destination / path.relative_to(staging)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, target)
            os.replace(staging / 'dataset.json', destination / 'dataset.json')
    except BaseException:
        # 이번 호출이 예약한 새 디렉토리만 회수한다. 기존 버전에는 접근하지 않는다.
        import shutil
        shutil.rmtree(destination)
        raise
    return destination


def reference_text(directory: Path, case_id: str, *, allow_synthetic=False) -> str:
    dataset = load_dataset(directory, require_ready=True, allow_synthetic=allow_synthetic)
    return reference_payload(dataset, case_id)['text']


def prepare_run_samples(dataset_directory: Path, run_directory: Path, bindings: list[RunCaseBinding], *, allow_synthetic=False):
    """실제 검색/전달 문맥을 순서대로 사용한다. 데이터셋 evidence로 검색 결과를 대체하지 않는다."""
    dataset = load_dataset(dataset_directory, require_ready=True, allow_synthetic=allow_synthetic)
    from .validation import validate_run, validate_ragas_input
    errors = validate_run(run_directory)
    if errors:
        raise ValueError('\n'.join(errors))
    manifest = RunManifest.model_validate_json((run_directory / 'run.json').read_text())
    if not (run_directory / '.frozen').is_file():
        raise ValueError('sample preparation requires frozen generation')
    def rows(name):
        path = run_directory / name
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    invocations = {r['invocation_id']: r for r in rows('invocations.jsonl')}
    contexts = rows('delivered_contexts.jsonl')
    retrievals = {r['invocation_id']: r for r in rows('retrievals.jsonl')}
    evidence = json.loads((run_directory / 'evidence.json').read_text())
    cases = {c.case_id: c for c in dataset.manifest.cases}
    reviews = {r.case_id: r for r in dataset.case_reviews}
    sources = {s.source_id: s for s in dataset.sources}
    samples, applicability, lineage = [], [], []
    if not bindings or len({b.case_id for b in bindings}) != len(bindings):
        raise ValueError('bindings must be nonempty with unique case IDs')
    for binding in bindings:
        case = cases[binding.case_id]
        if (binding.company_id, binding.section_id, binding.as_of_date) != (case.company_id, case.section_id, case.as_of_date):
            raise ValueError('binding company/section/date differs from case')
        if manifest.as_of_date != case.as_of_date:
            raise ValueError('generation as_of_date differs from fixed case')
        invocation = invocations.get(binding.generation_invocation_id)
        if invocation is None or invocation['status'] != 'succeeded' or invocation['invocation_type'] not in {'llm', 'node', 'function'}:
            raise ValueError('generation must refer to a successful recorded generation')
        section_aliases = {'evaluate_technology': 'technology', 'technical_eval': 'technology',
                           'evaluate_business': 'business', 'evaluate_market': 'market', 'market_eval': 'market',
                           'evaluate_team': 'team', 'team_eval': 'team', 'evaluate_risk': 'risk', 'risk_eval': 'risk',
                           'evaluate_competition': 'competition', 'competition_eval': 'competition', 'rank_companies': 'ranking'}
        section = None
        current, seen = invocation, set()
        while current and current['invocation_id'] not in seen:
            seen.add(current['invocation_id'])
            operation = current.get('section_id') or current['operation_id']
            section = section_aliases.get(operation.split('.')[0], operation)
            if section in {'technology', 'business', 'market', 'traction', 'team', 'risk', 'competition', 'ranking'}:
                break
            current = invocations.get(current.get('parent_invocation_id'))
        if invocation['company_id'] != case.company_id or section != case.section_id:
            raise ValueError('generation invocation scope mismatch')
        ref = binding.response_location.snapshot
        output_refs = [invocation.get('output_snapshot')] + [r['snapshot'] for r in rows('agent_outputs.jsonl') if r['invocation_id'] == binding.generation_invocation_id]
        if not any(r and r['path'] == ref.path and r['sha256'] == ref.sha256 for r in output_refs):
            raise ValueError('response is not from bound invocation output')
        path = Path(ref.path)
        if path.is_symlink() or not path.resolve().is_relative_to(run_directory.resolve()):
            raise ValueError('response snapshot outside run')
        content = path.read_bytes()
        text = content.decode('utf-8')
        location = binding.response_location
        response = text[location.start_offset:location.end_offset]
        if sha256(content) != ref.sha256 or location.end_offset > len(text) or response != location.quote:
            raise ValueError('response quote/hash/offset mismatch')
        fixed_ids = json.loads(safe_path(dataset_directory, case.evidence_snapshot.path).read_text())['source_ids']
        # 동일 내용 해시로 로컬 파일/URL 출처 계보를 연결한다. 원문을 재작성하지 않는다.
        for source_id in fixed_ids:
            source = sources[source_id]
            matches = [e for e in evidence if e['snapshot']['sha256'] == source.snapshot.sha256 and
                       case.company_id in e['company_ids']]
            if not matches:
                raise ValueError('generation evidence differs from fixed source: ' + source_id)
            lineage.append({'case_id': case.case_id, 'dataset_source_id': source_id, 'run_evidence_ids': [e['evidence_id'] for e in matches],
                            'run_source_ids': [e['source_id'] for e in matches], 'match_method': 'exact_content_sha256', 'content_sha256': source.snapshot.sha256})
        payload = reference_payload(dataset, case.case_id)
        delivered = [c for c in contexts if c['invocation_id'] == binding.generation_invocation_id]
        retrieval = retrievals.get(binding.retrieval_invocation_id)
        if binding.retrieval_invocation_id is not None:
            if retrieval is None or retrieval.get('company_id') != case.company_id:
                raise ValueError('retrieval invocation scope mismatch')
            if not any(binding.retrieval_invocation_id in c['retrieval_invocation_ids'] for c in delivered):
                raise ValueError('retrieval was not delivered to bound generation')
        for metric in case.supported_metrics:
            metric = metric.value
            base = dict(sample_id=case.case_id, evaluation_id='prepared', metric_name=metric,
                        user_input=case.user_input, response=response, reference=payload['text'], reference_ids=case.reference_ids)
            if metric.startswith('context_'):
                if retrieval is None:
                    applicability.append({'case_id': case.case_id, 'metric_name': metric, 'evaluation_status': 'not_applicable', 'reason': '실제 검색 호출이 없는 구간: ' + reviews[case.case_id].retrieval_applicability_reason})
                    continue
                candidates = retrieval['candidates']
                base.update(context_kind='retrieved', origin_invocation_id=retrieval['invocation_id'],
                            context_ids=[c['context_id'] for c in candidates], retrieved_contexts=[c['text'] for c in candidates], evidence_ids=[c['evidence_id'] for c in candidates])
            elif metric == 'faithfulness':
                if not delivered:
                    applicability.append({'case_id': case.case_id, 'metric_name': metric, 'evaluation_status': 'not_applicable', 'reason': '실제 전달 문맥이 기록되지 않음'})
                    continue
                base.update(context_kind='delivered', origin_invocation_id=binding.generation_invocation_id,
                            context_ids=[c['context_id'] for c in delivered], retrieved_contexts=[c['text'] for c in delivered], evidence_ids=list(dict.fromkeys(e for c in delivered for e in c['evidence_ids'])))
            else:
                base.update(context_kind='none', origin_invocation_id=binding.generation_invocation_id)
            sample = RagasSampleInput(**base)
            problems = validate_ragas_input(run_directory, sample)
            if problems:
                raise ValueError('\n'.join(problems))
            samples.append(sample)
    return samples, {'dataset_id': dataset.manifest.dataset_id, 'dataset_purpose': dataset.purpose,
                     'dataset_version': dataset.manifest.version, 'reference_sha256': dataset.manifest.sha256,
                     'run_id': manifest.run_id, 'applicability': applicability, 'source_lineage': lineage,
                     'retrieval_records': [retrievals[b.retrieval_invocation_id] for b in bindings if b.retrieval_invocation_id]}
