"""실제 전달 관계에 한정한 주장 대응. 오류 귀속은 후보이며 사람 검토를 덮어쓰지 않는다."""
from .claim_extraction import normalize_statement
from .claim_models import ClaimLink


def connect_claims(samples, claims, atom_by_claim, invocations, contexts, research_records, score_values=None):
    invocation_by_id = {i['invocation_id']: i for i in invocations}
    contexts_by_id = {c['context_id']: c for c in contexts}
    links, research = [], []
    for sample in samples:
        record = invocation_by_id[sample.invocation_id]
        delivered = [contexts_by_id[c] for c in sample.delivered_context_ids]
        upstream_ids = {i for c in delivered for i in c['upstream_invocation_ids']}
        previous_id = record.get('previous_invocation_id')
        if previous_id:
            upstream_ids.add(previous_id)
        # 납품 원본과 작성/보강 전후를 연결하되, 다른 기업·영역의 결과는 비교하지 않는다.
        if sample.stage == 'final':
            upstream_ids.add(sample.invocation_id)
        upstream = [s for s in samples if s.invocation_id in upstream_ids and s.sample_id != sample.sample_id and
                    (s.company_id, s.section_id) == (sample.company_id, sample.section_id) and
                    (s.stage == 'intermediate')]
        sample.upstream_sample_ids = [s.sample_id for s in upstream]
        previous = next((s for s in upstream if s.invocation_id == previous_id), None)
        sample.previous_sample_id = previous.sample_id if previous else None
        current_claims = [c for c in claims if c.sample_id == sample.sample_id]
        for downstream in current_claims:
            same_slot = [c for c in claims if c.sample_id in sample.upstream_sample_ids and
                         atom_by_claim[c.claim_id].relation_key == atom_by_claim[downstream.claim_id].relation_key]
            if not same_slot:
                downstream.error_origin_candidate = 'new' if upstream and all(s.evaluation_status != 'error' for s in upstream) else 'unknown'
                downstream.origin_reason = '상위 추출에서 같은 속성을 찾지 못한 신규 후보; 누락 가능성 및 사실 정확도는 별도 검토' if upstream else '실제 전달된 대응 상위 주장 없음'
                continue
            for parent in same_slot:
                repeated = normalize_statement(parent.atomic_statement) == normalize_statement(downstream.atomic_statement)
                corrected = not repeated and sample.reference is not None and normalize_statement(downstream.atomic_statement) in {
                    normalize_statement(t) for t in sample.reference.splitlines() if t.strip()}
                candidate = 'inherited' if repeated else ('corrected' if corrected else 'unknown')
                reason = ('실제 상위 주장과 동일한 내용; 오류 여부는 기준 사실 판정 이후 확인' if repeated else
                          '같은 속성의 내용이 verified reference 문장과 일치하는 방향으로 변경됨; 수정 후보' if corrected else
                          '동일 속성의 내용이 변경되었으나 정정/신규 오류를 자동 확정할 수 없음')
                basis = 'reevaluation' if parent.invocation_id == previous_id else ('report_artifact' if sample.stage == 'final' else 'actual_delivered_context')
                links.append(ClaimLink(upstream_claim_id=parent.claim_id, downstream_claim_id=downstream.claim_id,
                                       relation='repeated' if repeated else 'changed', basis=basis, candidate=candidate, reason=reason))
                downstream.upstream_claim_ids.append(parent.claim_id)
                # 여러 입력이 서로 다르면 하나의 원인으로 자동 확정하지 않는다.
                existing = downstream.error_origin_candidate
                downstream.error_origin_candidate = candidate if existing == 'unknown' else (existing if existing == candidate else 'unknown')
                downstream.origin_reason = reason
    for record in research_records:
        before = [s for s in samples if s.company_id == record.get('company_id') and s.section_id == record['section_id'] and
                  s.invocation_id != record['invocation_id'] and invocation_by_id[s.invocation_id]['ended_at'] and
                  invocation_by_id[s.invocation_id]['ended_at'] <= invocation_by_id[record['invocation_id']]['started_at']]
        after = [s for s in samples if s.invocation_id == record.get('reevaluation_invocation_id')]
        before = sorted(before, key=lambda s: invocation_by_id[s.invocation_id]['ended_at'])
        newest = before[-1] if before else None
        before_scores = (score_values or {}).get(newest.invocation_id) if newest else None
        after_scores = {s.invocation_id: (score_values or {}).get(s.invocation_id) for s in after}
        research.append({'invocation_id': record['invocation_id'], 'company_id': record.get('company_id'), 'section_id': record['section_id'],
                         'before_sample_ids': [newest.sample_id] if newest else [], 'after_sample_ids': [s.sample_id for s in after],
                         'new_evidence_ids': record['new_evidence_ids'], 'duplicate_evidence_ids': record['duplicate_evidence_ids'],
                         'evidence_outcome': record['outcome'], 'termination_reason': record.get('termination_reason'),
                         'text_changed': None if not newest or not after else any(newest.response != s.response for s in after),
                         'uncertainty_resolved': None, 'critical_error_change': None,
                         'before_scores': before_scores, 'after_scores': after_scores,
                         'score_changed': None if not before_scores or not after_scores or not all(after_scores.values()) else any(before_scores != v for v in after_scores.values()),
                         'success_verdict': 'not_established',
                         'reason': '추가 증거·내용 판정·점수 독립 검사 전에는 문장/점수 변화로 조사 성공을 판정하지 않는다.'})
    return links, research
