"""명시적으로 작성한 #21 최초 자료를 새 디렉토리에 발행한다. 네트워크/LLM 없음.

실제 출처 발췌는 2026-10-05 확인한 1차 발표이고 사람 검토는 pending이다.
가상 원문과 기대 판정은 검증 코드 테스트용이며 실제 기업 성능으로 집계하지 않는다.
"""
from pathlib import Path

from .dataset import canonical_bytes, publish_dataset, reference_payload, sha256
from .dataset_models import FixedDataset

COLLECTED = '2026-10-05T04:42:11Z'
AS_OF = '2026-10-05'


def build_seed(*, synthetic: bool):
    """(typed bundle, bytes) 반환. 같은 입력은 위치에 무관하게 같은 해시가 된다."""
    files, sources, facts, items, cases, reviews = {}, [], [], [], [], []
    version = '1.0.0' if synthetic else '0.1.0-draft'
    reviewer = 'fixture-author:Codex' if synthetic else None

    def snapshot(name, content):
        raw = content.encode('utf-8') if isinstance(content, str) else canonical_bytes(content) + b'\n'
        files[name] = raw
        return {'path': name, 'sha256': sha256(raw), 'media_type': 'text/plain' if name.endswith('.txt') else 'application/json'}

    def add_case(case_id, company, section, source_text, url, published, title, locator,
                 fact_specs, assertions, tags, question, route=None, route_reason=None):
        source_id = 'source_' + case_id
        source_ref = snapshot('sources/' + case_id + '.txt', source_text)
        sources.append(dict(source_id=source_id, company_ids=[company], title=title, url=url,
                            published_date=published, collected_at=COLLECTED, snapshot=source_ref,
                            snapshot_kind='synthetic_fixture' if synthetic else 'original_excerpt',
                            provenance='synthetic_fixture' if synthetic else 'primary_source', original_locator=locator))
        refs, required_ids, fact_ids = [], [], []
        for index, (category, statement, value, unit, conditions) in enumerate(fact_specs, 1):
            fact_id, ref_id, item_id = (prefix + case_id + '_' + str(index) for prefix in ('fact_', 'reference_', 'required_'))
            refs.append(ref_id)
            fact_ids.append(fact_id)
            required_ids.append(item_id)
            facts.append(dict(fact_id=fact_id, reference_id=ref_id, company_id=company, section_id=section,
                              statement=statement, value=value, unit=unit, conditions=conditions, as_of_date=AS_OF,
                              source_as_of_date=published, source_ids=[source_id],
                              source_locations=[dict(snapshot=source_ref, start_offset=0, end_offset=len(source_text), quote=source_text)],
                              category=category, critical=True, required_item_ids=[item_id],
                              review_status='verified' if synthetic else 'pending', reviewer=reviewer,
                              reviewed_at=COLLECTED if synthetic else None,
                              review_kind='synthetic_fixture' if synthetic else None))
            items.append(dict(required_item_id=item_id, version=version, company_id=company, section_id=section,
                              description=category + ': ' + statement,
                              accuracy_rule='수치·단위·통화·하한 표현을 보존하고 PoC/계약/양산과 거래금액/매출을 구분한다.',
                              freshness_rule='질문의 사건 시점과 자료 기준일을 명시한다. 과거 라운드를 현재 단계로 일반화하지 않는다.',
                              evidence_rule='source_id 및 고정 원문 구간으로 뒷받침하고 비교 조건을 생략하지 않는다.',
                              applicability_rule='이 기업·영역·질문 범위에만 적용한다. 다른 기업 또는 전체 투자보고서의 정답으로 사용하지 않는다.',
                              unknown_information_policy='원문에 없는 값은 0이나 사실 없음으로 단정하지 않고 미확인이라고 응답한다.',
                              expected_unknown_response='제공된 자료로는 해당 수치 또는 상태를 확인할 수 없어 추가 근거가 필요합니다.',
                              reference_ids=[ref_id]))
        evidence_ref = snapshot('evidence/' + case_id + '.json', {'source_ids': [source_id]})
        # 검증된 fact만 사용하는 reference 스냅샷은 bundle 구성 후 채운다.
        cases.append(dict(case_id=case_id, company_id=company, section_id=section, as_of_date=AS_OF,
                          user_input=question, evidence_snapshot=evidence_ref,
                          reference_snapshot={'path': 'references/' + case_id + '.json', 'sha256': '0' * 64},
                          reference_ids=refs, required_item_ids=required_ids,
                          supported_metrics=['factual_precision', 'factual_recall', 'faithfulness', 'context_recall', 'context_precision']))
        reviews.append(dict(case_id=case_id, scope_description=question + ' 이외의 최신 정보·전체 보고서는 이 reference 범위 밖이다.',
                            completeness='complete' if synthetic else 'pending', reviewer=reviewer,
                            reviewed_at=COLLECTED if synthetic else None, review_kind='synthetic_fixture' if synthetic else None,
                            relevant_source_ids=[source_id], retrieval_applicability_reason='검색 호출과 이 원문 관련성 기준이 연결된 경우에만 적용한다.',
                            tags=tags, expected_assertions=[dict(statement=s, expected_verdict=v, reason=r, fact_ids=fact_ids) for s, v, r in assertions],
                            expected_route=route, route_reason=route_reason))

    if not synthetic:
        add_case('groq_2024_round', 'Groq', 'business',
                 'Fenwick Represents Cisco Investments in Groq’s $640M Series D Funding',
                 'https://www.fenwick.com/insights/experience/fenwick-represents-cisco-investments-in-groqs-640m-series-d-funding',
                 '2024-08-05', 'Cisco 투자 참여를 대리한 Fenwick의 거래 발표', '발표 제목 및 August 5, 2024 게시 날짜',
                 [('stage', 'Groq의 2024년 8월 발표된 투자 라운드는 Series D이다.', 'Series D', None, ['2024년 8월 사건에 한정']),
                  ('funding', 'Groq의 해당 Series D 라운드 조달 금액은 6억 4천만 미국 달러(USD 640,000,000)이다.', 640000000, 'USD', ['해당 라운드 금액', '누적 조달액·매출이 아님'])],
                 [('Groq가 해당 라운드에서 6,400만 달러를 조달했다.', 'contradicted', '640 million을 64 million으로 잘못 변환했다.')],
                 ['currency', 'funding', 'historical_date'], 'Groq의 2024년 8월 투자 라운드 단계와 해당 라운드 조달 금액을 설명하라.')
        add_case('tenstorrent_2024_round', 'Tenstorrent', 'business',
                 'closed over $693M in its Series D funding round at a pre-money valuation of $2B.',
                 'https://www.prnewswire.com/news-releases/tenstorrent-closes-693m-of-series-d-funding-led-by-samsung-securities-and-afw-partners-302319584.html',
                 '2024-12-02', 'Tenstorrent가 배포한 Series D 발표', '첫 문단 closed over부터 $2B까지; Dec 02, 2024 게시 날짜',
                 [('stage', 'Tenstorrent의 2024년 12월 2일 발표된 투자 라운드는 Series D이다.', 'Series D', None, ['2024년 12월 2일 사건에 한정']),
                  ('funding', 'Tenstorrent는 해당 Series D 라운드에서 6억 9,300만 미국 달러를 초과하는 금액을 조달했다고 발표했다.', 693000000, 'USD', ['strictly_greater_than', '해당 라운드 금액', '기업 발표이며 감사된 매출이 아님'])],
                 [('Tenstorrent가 2024년 8월에 정확히 6억 9,300만 달러를 조달했다.', 'contradicted', '발표 날짜와 초과 조건이 다르다.')],
                 ['time', 'funding', 'lower_bound'], 'Tenstorrent의 2024년 12월 2일 발표된 라운드 단계와 조달 금액을 하한 조건까지 설명하라.')
    else:
        definitions = [
            ('currency', 'business', '가상기업 FixtureChip은 2024-08-05 Series A에서 6억 4천만 미국 달러를 조달했다.',
             [('funding', 'FixtureChip은 2024년 8월 5일 Series A에서 USD 640,000,000을 조달했다.', 640000000, 'USD', ['해당 라운드'])],
             [('FixtureChip은 6,400만 달러를 조달했다.', 'contradicted', '10배 수치 오류'), ('FixtureChip은 6억 4천만 원을 조달했다.', 'contradicted', 'USD를 KRW로 바꿈')], ['numeric', 'currency']),
            ('time', 'business', '가상기업 FixtureChip은 2023년 Seed, 2024-08-05 Series A 투자 라운드를 완료했다.',
             [('stage', 'FixtureChip의 2024년 8월 5일 라운드는 Series A이며 2023년 라운드는 Seed였다.', 'Series A', None, ['2024-08-05 사건'])],
             [('FixtureChip의 2024년 8월 라운드는 Seed였다.', 'contradicted', '과거 라운드와 혼동')], ['time']),
            ('poc', 'traction', '가상기업 FixtureChip은 고객 2곳과 PoC 진행 중이다. 유료 계약은 체결하지 않았으며 양산 전이다.',
             [('commercial_status', 'FixtureChip은 고객 2곳과 PoC 중이며 유료 계약 미체결 및 양산 전이다.', 'PoC', None, ['고객 수는 PoC 참여 수', '유료 고객 수가 아님']), ('customers', 'FixtureChip의 PoC 참여 고객은 2곳이다.', 2, 'companies', ['PoC'])],
             [('FixtureChip은 유료 고객 2곳과 계약하고 양산을 시작했다.', 'contradicted', 'PoC를 계약 및 양산으로 바꿈')], ['poc_contract', 'production']),
            ('benchmark', 'technology', '가상기업 FixtureChip 내부 시험: ResNet-50 INT8 batch=1, 동일 전력 10W에서 비교기 A 대비 처리량 2배. LLM 또는 FP16 측정은 없음.',
             [('benchmark', 'FixtureChip의 자체 시험에서 ResNet-50 INT8 batch=1, 동일 전력 10W 조건으로 비교기 A 대비 처리량이 2배였다.', 2, 'ratio', ['ResNet-50', 'INT8', 'batch=1', '10W', '비교기 A', '자체 시험'])],
             [('FixtureChip은 LLM FP16 성능이 모든 GPU보다 2배 높다.', 'unverifiable', '측정 대상·정밀도·비교 장치 범위 밖'), ('FixtureChip 처리량은 2 TOPS이다.', 'unverifiable', '상대 배율에서 절대 TOPS 수치를 알 수 없음')], ['unit', 'benchmark_conditions']),
            ('unknown', 'business', '가상기업 FixtureChip 자료에는 매출 및 유료 고객 수가 공개되어 있지 않다.',
             [('revenue', '제공된 FixtureChip 자료로는 매출을 확인할 수 없다.', None, 'KRW', ['제공된 자료 범위만 의미함']), ('customers', '제공된 FixtureChip 자료로는 유료 고객 수를 확인할 수 없다.', None, 'companies', ['0명이라는 의미가 아님'])],
             [('FixtureChip의 연매출은 100억 원이다.', 'unverifiable', '매출 근거 없음'), ('FixtureChip의 유료 고객 수는 0명이다.', 'unverifiable', '미공개와 0을 혼동')], ['missing_evidence', 'unknown']),
            ('recommend', 'ranking', '가상 고정 정책: CLI 가중 점수 65 이상이면 추천 후보. FixtureChip의 확정 시험 점수는 70이다.',
             [('commercial_status', '가상 정책 시험에서 FixtureChip 점수는 70이고 추천 기준은 65 이상이다.', 70, 'points', ['CLI 경로', '실제 기업 투자판단 아님'])],
             [('고정 정책에서 FixtureChip은 추천 후보다.', 'verified', '70 >= 65')], ['recommend']),
            ('hold', 'ranking', '가상 고정 정책: CLI 가중 점수 65 이상이면 추천 후보. FixtureChip의 확정 시험 점수는 60이다.',
             [('commercial_status', '가상 정책 시험에서 FixtureChip 점수는 60이고 추천 기준은 65 이상이다.', 60, 'points', ['CLI 경로', '실제 기업 투자판단 아님'])],
             [('고정 정책에서 FixtureChip은 추천 후보다.', 'contradicted', '60 < 65')], ['hold']),
            ('market', 'market', '가상 시장 자료: 시험용 국내 시장 규모는 2024년 100억 원이다. 2025년 자료는 없다.',
             [('market', '가상 국내 시장의 2024년 규모는 100억 원이며 제공 자료에 2025년 수치는 없다.', 10000000000, 'KRW', ['국내', '2024', '가상 시장'])],
             [('가상 국내 시장의 2025년 규모는 100억 원이다.', 'unverifiable', '시장 수치 시점 외삽')], ['market', 'time']),
        ]
        for name, section, text, specs, assertions, tags in definitions:
            route = name if name in {'recommend', 'hold'} else None
            add_case('fixture_' + name, 'FixtureChip', section, text, None, '2024-08-05',
                     'Codex가 작성한 가상 검증 원문: ' + name, '전체 가상 원문 (외부 기업 자료 아님)', specs, assertions, tags,
                     '다음 가상 자료에 한정하여 FixtureChip의 ' + name + ' 내용을 설명하라.',
                     route=route, route_reason='CLI의 65점 기준을 고정한 분기 예제; 실제 Agent 점수 또는 투자 추천이 아니다.' if route else None)
        # 범위 밖 연도의 필수 항목은 N/A이며, 값 없음/0과 구분한다.
        excluded_id = 'required_fixture_market_2025_excluded'
        items.append(dict(required_item_id=excluded_id, version=version, company_id='FixtureChip', section_id='market',
                          description='2025년 시장 규모', critical=False, applicability='not_applicable',
                          not_applicable_reason='이 case는 제공된 2024년 자료 범위의 질문이며 2025년 규모 추정은 요구하지 않는다.',
                          accuracy_rule='2024년 값을 2025년으로 복사하지 않는다.', freshness_rule='2025년 기준의 자료가 별도로 필요하다.',
                          evidence_rule='2025년 수치를 추정하려면 해당 연도 원문이 필요하다.',
                          applicability_rule='2024년 자료 범위를 평가할 때 2025년 항목은 분모에서 제외한다.',
                          unknown_information_policy='제공되지 않은 2025년 수치를 임의로 채우지 않는다.',
                          expected_unknown_response='제공 자료로는 2025년 규모를 확인할 수 없습니다.', reference_ids=[]))
        cases[-1]['required_item_ids'].append(excluded_id)
    dataset = FixedDataset.model_validate(dict(
        purpose='synthetic_fixture' if synthetic else 'baseline', status='released' if synthetic else 'draft',
        manifest=dict(dataset_id='semiconductor-fixtures' if synthetic else 'semiconductor-baseline', version=version,
                      sha256='0' * 64, created_at=COLLECTED, reviewers=[reviewer] if synthetic else [], cases=cases),
        sources=sources, facts=facts, required_items=items, case_reviews=reviews, inventory={}))
    from .models import SnapshotRef
    for case in dataset.manifest.cases:
        case.reference_snapshot = SnapshotRef(**snapshot(case.reference_snapshot.path, reference_payload(dataset, case.case_id)))
    return dataset, files


def main():
    import argparse
    parser = argparse.ArgumentParser(description='최초 고정 데이터셋 생성; 존재하는 버전은 덮어쓰지 않음')
    parser.add_argument('--parent', type=Path, required=True)
    args = parser.parse_args()
    for synthetic in (False, True):
        dataset, files = build_seed(synthetic=synthetic)
        print(publish_dataset(dataset, files, args.parent))


if __name__ == '__main__':
    main()
