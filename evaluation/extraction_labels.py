"""선언한 기대 label과 추출 결과의 누락·과분해·오분류를 별도로 기록한다."""
from .claim_extraction import normalize_statement


def compare_labels(expected, actual):
    """statement+kind 다중집합 대조. 의미 평가는 별도 사람 검토로 남긴다."""
    from collections import Counter
    wanted = Counter((normalize_statement(s), k) for s, k in zip(expected['facts'], expected['kinds'], strict=True))
    observed = Counter((normalize_statement(a['statement']), a['kind']) for a in actual)
    matched = sum((wanted & observed).values())
    return {'expected_count': sum(wanted.values()), 'observed_count': sum(observed.values()), 'matched_count': matched,
            'missing': [list(v) for v in (wanted - observed).elements()], 'unexpected': [list(v) for v in (observed - wanted).elements()],
            'precision': matched / sum(observed.values()) if observed else None,
            'recall': matched / sum(wanted.values()) if wanted else None,
            'basis': 'exact_statement_and_kind', 'semantic_human_review': 'pending'}
