"""RAGAS 공개 prompt callbacks에서 읽은 claim만 연결한다. 분모/중복을 추정하지 않는다."""
from .claim_extraction import normalize_statement, stable_id


def map_raw_claims(trace, sample, custom_claims):
    """같은 metric/attempt/callback의 개별 claim ID와 exact-text 후보 대응을 보존한다."""
    records = []
    for attempt in trace.raw_response.get('attempts', []):
        starts = {c['run_id']: c for c in attempt.get('callbacks', []) if c['kind'] == 'chain_start'}
        for end in attempt.get('callbacks', []):
            if end['kind'] != 'chain_end':
                continue
            start = starts.get(end['run_id'])
            if start is None:
                continue
            data = start.get('input', {}).get('data', {})
            if not isinstance(data, dict):
                continue
            input_text = data.get('response', data.get('answer'))
            orientations = [name for name in ['response', 'reference'] if input_text is not None and input_text == getattr(sample, name)]
            source = orientations[0] if len(orientations) == 1 else ('ambiguous' if orientations else 'unknown')
            if source == 'unknown':
                continue
            output = end.get('output', {}).get('output', [])
            if not isinstance(output, list):
                continue
            for model in output:
                if not isinstance(model, dict):
                    continue
                values = model.get('claims', model.get('statements', []))
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                    continue
                for index, statement in enumerate(values):
                    raw_id = stable_id('ragas_claim', trace.evaluation_id, sample.sample_id, trace.metric_name, attempt['attempt'], end['run_id'], index)
                    matched = [c.claim_id for c in custom_claims if c.sample_id == sample.sample_id and source == 'response' and
                               normalize_statement(c.atomic_statement) == normalize_statement(statement)]
                    records.append({'raw_claim_id': raw_id, 'metric_name': trace.metric_name.value, 'sample_id': sample.sample_id,
                                    'attempt': attempt['attempt'], 'callback_run_id': end['run_id'], 'index': index,
                                    'source': source, 'statement': statement, 'custom_claim_ids': matched,
                                    'mapping_basis': 'exact_normalized_text_candidate', 'original_locations': [
                                        p.model_dump(mode='json') for c in custom_claims if c.claim_id in matched for p in c.occurrences]})
    trace.raw_claim_ids = [r['raw_claim_id'] for r in records]
    trace.custom_claim_mapping = {r['raw_claim_id']: r['custom_claim_ids'] for r in records if r['custom_claim_ids']}
    # 같은 문자열도 의미 대응을 자동 확정하지 않으며 다른 metric의 분모로 재사용하지 않는다.
    trace.mapping_status = 'partial' if records else 'unsupported'
    trace.unavailable_reason = ('공개 callback의 원문 일치 후보만 연결; metric별 의미 대응/분모/중복 규칙은 미확정' if records else
                                'claim 분해 입력/출력을 연결할 공개 callback 없음; custom 추출을 내부 결과로 대체하지 않음')
    return records
