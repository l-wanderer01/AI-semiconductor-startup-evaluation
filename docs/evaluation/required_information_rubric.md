# 필수 항목 작성 기준

이 문서는 #25의 항목 정의를 검토할 때 사용하는 작성 기준이다.
실행 시 점수의 기준은 사람 검토·발행된 고정 데이터셋의 `DatasetRequiredItem`이다.
문서의 키워드 목록 자체로 pass를 주지 않는다. 기존 생성 보고서를 reference로 채택하지 않는다.

| 영역(section_id) | 필요한 설명·값 | 정확성·시점·근거 조건 |
|---|---|---|
| technology | 제품·아키텍처, 대상 워크로드, benchmark 성능·효율·한계 | 모델·정밀도·batch·전력·비교 장치, 측정 시점과 1차 원문 |
| market | 목표 시장·고객, 시장 규모·성장 가정 | TAM/SAM/SOM 구별, 통화·기준 연도·예측 기간·산정 근거 |
| team | 핵심 경영·기술 인력, 관련 경력·역량 | 기준일의 실제 직책·경력, 검토한 1차 자료; 추정 인원수 금지 |
| traction | 고객·PoC·유료 계약·매출·양산 상태 | 계약/PoC 구분, 누적/기간 매출·통화, 고객 증거와 기준일 |
| competition | 비교 대상, 차별점과 경쟁 열위 | 같은 워크로드·측정 조건, 상대 기업 원문과 양쪽 비교 근거 |
| risk | 기술·사업·조달·경쟁 위험, 근거와 추가 실사 과제 | 위험 원인·조건과 확인 가능한 사실을 구별하고 미확인 사항 명시 |

각 영역에서 여러 독립 항목을 만든다. 하나의 긴 항목으로 여러 주제의 누락을 숨기지 않는다.
business 등 기존 sample 영역을 사용할 때는 item/case/claim의 영역을 동일하게 유지하며,
새 영역과 임의로 합치지 않는다. 전체 보고서는 필요한 모든 영역에 case binding을 지정한다.
추천·보류의 형식 차이는 생성 정책을 바꾸지 않고 사전 정의한 applicability에 반영한다.
보류 보고서에도 필요한 기술·시장·트랙션·리스크 설명은 임의로 제외하지 않는다.

각 항목에 다음 값을 정의한다.

1. `required_item_id`, `version`, `company_id`, `section_id`: 안정적인 범위와 데이터셋 버전.
2. `description`: 제공해야 하는 값/설명/조건. 예: 유료 계약 여부와 PoC 고객 수를 구별하여 설명.
3. `accuracy_rule`: 숫자·단위·통화·비교 조건과 상태의 정합성.
4. `freshness_rule`: 사건/관측 시점·기준일을 명시하고 과거 사실을 현재로 일반화하지 않음.
5. `evidence_rule`: 어떤 1차 출처·원문 구간 및 보고서 출처 표기가 필요한지 구체화.
6. `applicability_rule`, `applicability`, `not_applicable_reason`: 발행 전에 검토한 적용/제외 규칙.
   해당 회사에 공개 자료가 없다는 이유는 제외 사유가 아니다.
7. `unknown_information_policy`, `expected_unknown_response`: 자료 부족 시 요구하는 설명.
   예: “기준일의 유료 계약 여부는 확인되지 않았다. 고객 발표나 계약 증거를 추가 확인해야 한다.”
8. `reference_ids`: 검토한 핵심 fact의 reference ID. fact의 `required_item_ids`에도 역연결한다.
   제공 자료에 정보가 없으면 빈 reference 목록과 불확실성 정책을 명시한다.
9. `critical`: 투자 판단에 영향을 주는 핵심 항목 여부.

case의 `required_item_ids`와 `reference_ids`에도 항목과 핵심 사실을 연결한다.
source 원문·quote·offset·시점과 case 범위를 사람 검토한 뒤 새 버전으로 발행한다.
지표가 낮다는 이유로 적용 제외나 불확실성 정책을 사후 수정하지 않는다.
rubric을 바꾸면 기준 버전과 해시가 달라지므로 이전 결과와 동일 기준 비교로 취급하지 않는다.
