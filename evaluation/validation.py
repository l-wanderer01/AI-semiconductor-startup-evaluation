"""저장된 run의 파일 간 ID와 snapshot 해시를 검증한다."""
import hashlib
import json
from pathlib import Path

from .models import AgentOutputRecord, DeliveredContext, EvidenceRecord, EventRecord, InvocationRecord, RetrievalRecord, RunManifest, RuleCheckResult, SnapshotRef


def validate_run(directory: Path) -> list[str]:
    directory = directory.resolve()
    errors = []
    manifest = RunManifest.model_validate_json((directory / 'run.json').read_text())
    def read_rows(name, model):
        path = directory / name
        if not path.exists():
            return []
        return [model.model_validate_json(line) for line in path.read_text().splitlines()]
    invocations = read_rows('invocations.jsonl', InvocationRecord)
    events = read_rows('events.jsonl', EventRecord)
    outputs = read_rows('agent_outputs.jsonl', AgentOutputRecord)
    contexts = read_rows('delivered_contexts.jsonl', DeliveredContext)
    retrievals = read_rows('retrievals.jsonl', RetrievalRecord)
    evidence = [EvidenceRecord.model_validate(v) for v in json.loads((directory / 'evidence.json').read_text())]
    invocation_ids = {r.invocation_id for r in invocations}
    evidence_ids = {r.evidence_id for r in evidence}
    context_ids = {r.context_id for r in contexts}
    retrieval_ids = {r.invocation_id for r in retrievals}
    for label, ids, rows in [('invocation', invocation_ids, invocations), ('evidence', evidence_ids, evidence),
                              ('context', context_ids, contexts)]:
        if len(ids) != len(rows):
            errors.append(f'duplicate {label} ID')
    def require(value, allowed, label):
        if value is not None and value not in allowed:
            errors.append(f'{label}: unknown reference {value}')
    def snapshot(ref):
        if ref is None:
            return
        path = Path(ref.path)
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            errors.append('snapshot outside run: ' + ref.path)
        elif not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref.sha256:
            errors.append('snapshot hash mismatch: ' + ref.path)
    snapshot(manifest.input_snapshot)
    snapshot(manifest.evidence_snapshot)
    rule_summary = directory / 'rule_conformance.json'
    if rule_summary.is_file():
        summary = json.loads(rule_summary.read_text())['data']
        for ref in summary.get('source_snapshots', []) + [summary['policy_snapshot']]:
            snapshot(SnapshotRef.model_validate(ref))
        for value in json.loads((directory / 'checks.json').read_text()):
            value.pop('status', None)
            check = RuleCheckResult.model_validate(value)
            if check.run_id != manifest.run_id:
                errors.append('rule check scope mismatch')
            require(check.invocation_id, invocation_ids, 'rule invocation')
    for r in invocations:
        if r.run_id != manifest.run_id or r.evaluation_id is not None:
            errors.append('invocation scope mismatch: ' + r.invocation_id)
        require(r.parent_invocation_id, invocation_ids, 'parent')
        require(r.previous_invocation_id, invocation_ids, 'previous')
        snapshot(r.input_snapshot)
        snapshot(r.output_snapshot)
        snapshot(r.state_update)
        if r.prompt:
            snapshot(r.prompt.snapshot)
        for v in r.evidence_ids:
            require(v, evidence_ids, 'invocation evidence')
        for v in r.context_ids:
            require(v, context_ids, 'invocation context')
    for r in evidence:
        snapshot(r.snapshot)
    for r in events + outputs + contexts + retrievals:
        require(r.invocation_id, invocation_ids, 'invocation')
        if hasattr(r, 'run_id') and r.run_id != manifest.run_id:
            errors.append('record run_id mismatch')
    for r in outputs:
        snapshot(r.snapshot)
        for v in r.evidence_ids:
            require(v, evidence_ids, 'output evidence')
        for v in r.context_ids:
            require(v, context_ids, 'output context')
    for r in contexts:
        if hashlib.sha256(r.text.encode()).hexdigest() != r.sha256:
            errors.append('delivered context hash mismatch')
        for v in r.evidence_ids:
            require(v, evidence_ids, 'delivered evidence')
        for v in r.retrieval_invocation_ids:
            require(v, retrieval_ids, 'delivered retrieval')
        for v in r.upstream_invocation_ids:
            require(v, invocation_ids, 'upstream')
    for r in retrievals:
        for c in r.candidates:
            require(c.evidence_id, evidence_ids, 'retrieved evidence')
            source = next((e.source_id for e in evidence if e.evidence_id == c.evidence_id), None)
            if source != c.source_id:
                errors.append('retrieval source mismatch')
            if hashlib.sha256(c.text.encode()).hexdigest() != c.content_sha256:
                errors.append('retrieved content hash mismatch')
    research_path = directory / 'additional_research.json'
    if research_path.exists():
        from .models import AdditionalResearchRecord
        for value in json.loads(research_path.read_text()):
            record = AdditionalResearchRecord.model_validate(value)
            require(record.invocation_id, invocation_ids, 'research invocation')
            require(record.reevaluation_invocation_id, invocation_ids, 'research reevaluation')
            snapshot(record.state_update)
            for v in record.new_evidence_ids + record.duplicate_evidence_ids:
                require(v, evidence_ids, 'research evidence')
            for v in record.search_invocation_ids:
                require(v, retrieval_ids, 'research search')
    for record in invocations:
        seen = set()
        current = record
        by_id = {r.invocation_id: r for r in invocations}
        while current is not None:
            if current.invocation_id in seen:
                errors.append('invocation parent cycle')
                break
            seen.add(current.invocation_id)
            current = by_id.get(current.parent_invocation_id)
    for artifact in manifest.artifacts:
        if artifact.status == 'saved':
            snapshot(SnapshotRef(path=artifact.path, sha256=artifact.sha256))
    return errors


def validate_ragas_input(directory: Path, sample) -> list[str]:
    """metric 입력을 실제 generation 호출·문맥·증거에 대조한다."""
    def rows(name):
        path = directory / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    invocations = {r['invocation_id']: r for r in rows('invocations.jsonl')}
    delivered = {r['context_id']: r for r in rows('delivered_contexts.jsonl')}
    retrievals = {r['invocation_id']: r for r in rows('retrievals.jsonl')}
    evidence_ids = {r['evidence_id'] for r in json.loads((directory / 'evidence.json').read_text())}
    errors = []
    if sample.origin_invocation_id not in invocations:
        errors.append('missing/unknown origin invocation')
    for value in sample.evidence_ids:
        if value not in evidence_ids:
            errors.append('unknown evidence ID: ' + value)
    if sample.context_kind == 'delivered':
        invocation = invocations.get(sample.origin_invocation_id)
        ref = invocation.get('input_snapshot') if invocation else None
        if ref is None:
            errors.append('faithfulness actual input snapshot missing')
        else:
            path = Path(ref['path'])
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']:
                errors.append('faithfulness actual input snapshot mismatch')
            elif sample.user_input != path.read_text():
                errors.append('faithfulness user_input differs from actual generation input')
        contexts = [delivered.get(value) for value in sample.context_ids]
        if sample.context_ids != [c['context_id'] for c in delivered.values() if c['invocation_id']==sample.origin_invocation_id]:
            errors.append('faithfulness must preserve the full recorded delivered context sequence')
        if not contexts or any(c is None for c in contexts):
            errors.append('missing/unknown delivered context ID')
        elif any(c['invocation_id'] != sample.origin_invocation_id for c in contexts):
            errors.append('delivered context belongs to another invocation')
        elif [c['text'] for c in contexts] != sample.retrieved_contexts:
            errors.append('faithfulness contexts differ from actual delivered contexts')
    elif sample.context_kind == 'received_input':
        invocation = invocations.get(sample.origin_invocation_id)
        ref = invocation.get('input_snapshot') if invocation else None
        if invocation is None or invocation['invocation_type'] not in {'node', 'function'} or ref is None:
            errors.append('received input requires recorded node/function input')
        else:
            path = Path(ref['path'])
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
                errors.append('received input snapshot outside run')
            elif hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']:
                errors.append('received input hash mismatch')
            elif sample.retrieved_contexts != [path.read_text()] or sample.user_input != path.read_text() or sample.context_ids != ['received_' + sample.origin_invocation_id]:
                errors.append('received context differs from actual invocation input')
    elif sample.context_kind == 'retrieved':
        retrieval = retrievals.get(sample.origin_invocation_id)
        if retrieval is None:
            errors.append('origin is not a recorded retrieval')
        else:
            if sample.user_input != retrieval['query']:
                errors.append('retrieval user_input differs from actual query')
            expected_ids = [c['context_id'] for c in retrieval['candidates']]
            expected_texts = [c['text'] for c in retrieval['candidates']]
            if sample.context_ids != expected_ids or sample.retrieved_contexts != expected_texts:
                errors.append('retrieval candidates/order differ from recorded retrieval')
            if sample.evidence_ids != [c['evidence_id'] for c in retrieval['candidates']]:
                errors.append('retrieval evidence IDs differ from ranked candidates')
            if retrieval.get('error') is not None:
                errors.append('retrieval invocation failed')
    if sample.context_sha256 is not None:
        content = json.dumps(sample.retrieved_contexts, ensure_ascii=False, separators=(',', ':')).encode()
        if hashlib.sha256(content).hexdigest() != sample.context_sha256:
            errors.append('metric context hash mismatch')
    return errors
