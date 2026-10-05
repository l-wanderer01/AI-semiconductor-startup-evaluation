# #25 사실 재현율과 필수 정보 충족률

이 평가는 고정 데이터셋의 `required_items`와 `manifest.cases[].required_item_ids`를
실제 단계 sample에 연결한다. 생성 Agent, 점수·추천 정책, 생성 프롬프트는 바꾸지 않는다.
현재 실제 기업 데이터는 사람 검토 대기 중이다. 가상 테스트 결과는 실제 baseline 점수가 아니다.

## 세 지표의 차이

| 지표 | 검사 대상 | 분모 |
|---|---|---|
| RAGAS Context Recall | 실제 query로 검색한 문맥에 reference 사실이 있는가 | RAGAS 내부 reference 분해 |
| RAGAS Factual Recall | 동일 case의 응답이 reference 사실을 재현하는가 | RAGAS 내부 reference 분해 |
| required_information_coverage | 필수 항목의 값·설명·시점·근거 규칙을 만족하는가 | 고정 적용 필수 항목 수 |

RAGAS 0.4.3의 recall을 #23처럼 precision과 동일 응답·reference로 실행한다.
RAGAS 점수는 `ragas_results.jsonl`에도 독립 저장하고 `recall_coverage_results.jsonl`에서
case/sample/claim/reference ID로 필수 항목 결과와 연결한다.
공개 API가 보장하지 않는 native 분해 사실 수는 `ragas_recall_denominator=null`과 사유로 저장한다.
reference 사실 수나 사용자 추출 주장 수를 그 분모에 대입하지 않는다.
필수 항목 버전·분자·분모·자료 해시는 별도 필드로 보존한다.

짧은 답변의 검증 주장 비율이 1이어도 항목이 빠지면 충족률은 내려간다.
많은 정확한 추가 사실이나 높은 recall도 필수 리스크 누락을 보상하지 않는다.

## 판정 정책

- `fulfilled`: 모든 연결 reference가 #23 독립 판정에서 verified이며,
  rubric의 정확성·설명·시점·근거 조건도 충족한다. 단순 제목·키워드 언급은 실패한다.
- `missing`: 미언급, 설명·시점·출처 부족, 필수 reference 일부 누락 또는 독립 확인 불가.
- `incorrect`: 항목과 연결된 명시적 모순, 잘못된 값·설명 또는 정보 부족 상태에서 값 단정.
- `not_applicable`: 고정 데이터셋에서 사전에 제외한 항목만 허용하고 사유를 저장한다.
  응답이나 평가 LLM이 적용 제외를 결정할 수 없다.

자료에 기준 사실이 없는 **적용 항목**도 분모에 남긴다.
`unknown_information_policy`와 `expected_unknown_response`의 의미에 따라
대상 정보의 미확인 상태·추가 확인 필요성을 정확히 설명해야 한다.
원문에 없는 값을 0 또는 정보 없음으로 단정하지 않는다. 정확히 같은 문구를 강제하지 않는다.
기준 자료가 미공개 상태를 명시한 경우에는 그 상태 자체도 독립 사실 판정과 연결한다.

검사는 sample의 `raw_response`(기업·영역별 실제 절·표)를 사용한다.
RAGAS는 기존 원자 사실 `response`를 사용하므로 두 입력의 차이는 stage package에 보존된다.
의견으로 분류된 불확실성 설명은 사실 주장 수가 0이어도 rubric 검사 대상이다.
근거 quote가 실제 원문에 있고 선택한 claim의 원문 구간과 연결되는지 검사한다.
주장·기준 ID가 다른 sample/기업/영역에서 넘어오면 거부한다.
의미·출처 표기·설명 요건은 고정 프롬프트의 구조화 LLM 제안이고 사람 검토 상태는 pending이다.
정책 검사는 잘못된 ID/quote/불완전 사실 판정을 거부하지만 실모델의 모든 의미 오판을 보증하지 않는다.

## 집계 범위

sample, case, company, section, company_section, report별로 저장한다.
중간 결과와 최종 보고서는 stage로 분리한다. 같은 필수 항목의 반복은 한 번만 센다.
같은 항목에서 정답과 오답이 공존하면 incorrect가 우선하며 오답을 숨기지 않는다.
미완료/오류가 있으면 충족률은 null이고 진행 수·미판정 항목을 남긴다.

기업·영역·보고서 집계는 해당 고정 데이터셋의 전체 적용 항목을 분모로 유지한다.
연결 sample이 없는 항목은 `unrepresented_required_item_ids`로 표시하며 확정률을 출력하지 않는다.
미연결은 실제 내용 누락이라고 단정할 수 없으므로 missing과 구별한다.
그 경우 sample 검사가 완료되어도 evaluation 전체 상태는 partial이다.
불확실성을 해소하려면 #22의 실제 원문 구간에 대한 case binding을 완성한다.

`total_claim_count`는 해당 범위의 고유 **사실 주장** 수다. 의견·점수·결정은 이 수에 포함하지 않는다.
원문 절·표와 제외 atom은 stage package에도 보존되므로 짧은 답변 여부를 함께 검토할 수 있다.
전체 보고서의 평가 범위는 고정 데이터셋에 정의한 항목까지다.
기존 자금조달 전용 draft를 기술·시장·팀·트랙션·경쟁·리스크 전체 평가로 해석하면 안 된다.
전체 baseline을 위해 [필수 항목 작성 기준](required_information_rubric.md)을 따라 자료와 항목을 확장하고
사람 검토한 새로운 데이터셋 버전을 발행해야 한다. 기존 불변 파일은 수정하지 않는다.

## 실행

검토 완료 baseline 데이터셋과 그 데이터셋으로 작성한 #22 stage package를 사용한다.

```bash
.venv-evaluation/bin/python -m evaluation evaluate evaluation_runs/<run_id> \
  --stage-package <stage_package_directory> \
  --custom-factual --reference-dataset <reviewed_dataset_directory> \
  --required-information \
  --model gpt-4.1-mini --judge-model gpt-4.1-mini --coverage-model gpt-4.1-mini
```

선택적으로 `--custom-grounding`을 함께 지정할 수 있다.
필수 항목 검사에는 독립 사실 검사가 필요하며 reference 없는 문맥 평가로 대체하지 않는다.
추가 의존성은 없다. 실행하면 평가 모델 호출 비용이 발생한다.
isolated 모드는 coverage 설정까지 포함한 evaluator 설정 해시와 일치해야 한다.

생성 run을 수정하지 않고 새 evaluation 아래에 저장한다.

- `required_information_assessments.jsonl`: 항목 verdict, claim/fact/reference/source ID,
  원문 위치, 고정 적용 여부·제외 이유, 버전·정책, 구조화 제안·raw 출력.
- `required_information_metrics.json`: 위 집계와 missing/incorrect/unjudged/미연결/제외 목록.
- `recall_coverage_results.jsonl`: 동일 case/sample의 RAGAS recall과 별도 충족률.
- `custom_coverage_configuration.json`: 프롬프트·구조화 스키마·모델 설정 및 해시.

네이티브 assessment JSONL과 달리 metrics 및 paired JSONL은 기존 저장 규약의 `data` wrapper를 사용한다.
timeout/구조화 파싱 오류는 error와 null 점수로 보존하고 완료로 처리하지 않는다.
입력 손상 등 전체 검사 실패도 failed manifest와 `.frozen`을 남긴다.

## 검증

```bash
.venv-evaluation/bin/python -B -m unittest discover -s tests -p 'test_evaluation_*.py' -q
```

가상 구조화 제안과 실제 고정 RAGAS API를 사용하는 오라클 기반 회귀 테스트다.
완전 충족, 축약·키워드, 틀린 값, 자료 부족·명시적 불확실성, 시점·근거 누락,
높은 recall/추가 사실과 별도 리스크 누락, 표 원문, 고정 제외, 반복·충돌,
기업 범위·오류·미연결, 보관 파일·설정 해시·불변 run을 검사한다.
실제 Agent baseline 생성 및 유료 평가 모델의 정확도·사람 일치도 검증은 아직 실행하지 않았다.
