# #22 원자 주장과 단계별 sample

동결된 기존 실행을 후처리해 Agent × 기업 × 영역 × invocation × 회차별 평가 입력을 만든다.
보고서 생성·검색·점수·추천·언어 보강 로직은 바꾸지 않는다. 추출은 별도 LangChain
structured-output LLM 호출이고, 실제 평가 기준에 따른 사실 판정은 #23/#24의 범위다.

## 구현과 저장 계약

| 파일 | 역할 |
| --- | --- |
| evaluation/claim_models.py | 추출 단위, 원자 주장 후보, 변환 감사, StageSample, coverage, 계보 후보, isolated 계약 |
| evaluation/claim_extraction.py | Markdown/표/JSON 원문 위치 대응, 고정 추출 프롬프트, LangChain adapter |
| evaluation/stage_samples.py | 동결 run → 중간/최종 sample 및 보관 파일, RAGAS 입력 변환, 재검증 |
| evaluation/claim_lineage.py | 실제 상위 전달·재평가·납품 관계와 추가 조사 전후 추적 |
| evaluation/ragas_claim_mapping.py | 공개 RAGAS callback의 claim 원문과 custom claim 후보 대응 |
| evaluation/claim_cli.py | 별도 추출 실행 |
| evaluation/label_cli.py | 저장된 추출 결과와 명시적 label의 누락/과분해/오분류 대조 |

보관 schema는 `0.3.0`, 추출기 `ko-atomic-v1`, 변환 규칙 `facts-only-exact-dedup-v1`,
역할 대응 `baseline-role-map-v1`이다. prompt 본문·SHA-256·출력 schema·모델 설정·원응답·오류를 보존한다.

새 출력 디렉토리에 `package.json`, `manifest.json`과 아래 JSONL을 저장한다.

- `samples.jsonl`: raw_response, facts용 response, 원문 위치/해시, invocation·agent/node·기업·영역·attempt·stage·mode·입력/reference 계보.
- `claims.jsonl`: sample별 고유 custom 사실 주장과 occurrence 위치. opinion/score/decision은 사실 분모에 넣지 않는다.
- `atoms.jsonl`: 제외한 의견/점수/결정을 포함한 모든 분해 후보, decoded quote, 원본 위치, 인용 출처 후보 및 미해결 인용.
- `extractions.jsonl`: 모든 본문/표/JSON 단위, 형식/metadata 제외 이유, 추출 원응답과 실패.
- `coverage.jsonl`: 실제 지원/미지원/누락/오류 단계와 기업별 coverage.
- `claim_links.jsonl`: 상위·하위 claim ID, 대응 이유, 오류 귀속 후보와 사람 검토 대기 상태.

전체 원문은 동결된 run의 snapshot으로 보존한다. raw_response는 영역에 속한 원문 단위이며
다른 영역을 포함한 전체 snapshot을 대체하지 않는다. `research_transitions`에는 전후 sample ID,
신규/중복 근거, 종료 사유, 관측된 점수 및 문장 변화, 아직 확정하지 않은 조사 성공을 기록한다.
기존 생성 run의 빈 claims 파일을 덮어쓰지 않는다. 평가 시 새 evaluation 디렉토리에 claims와
stage_samples를 복사하면서 evaluation_id를 새 값에 연결하고 추출 ID는 별도로 유지한다.

## 원문 위치와 변환 규칙

1. Markdown 행과 표 행의 문자 offset을 보존한다. 제목의 기업/영역과 표 헤더를 추출 문맥으로 전달한다.
   회사 열이 있는 표는 행의 기업으로 분리하고, reference 목록·제목·표 구분선은 이유를 남겨 제외한다.
2. JSON 출력은 문자열 leaf와 JSON pointer를 추출한다. JSON escape 및 surrogate pair의
   decoded 문자 위치를 원문 offset으로 대응한다. JSON 안의 보고서 Markdown도 행별 위치를 보존한다.
   숫자형 점수는 전체 원본에 그대로 있고 점수 변화 검사에서 독립적으로 읽는다.
3. 복합 문장은 각 사실의 quote를 유지하며 분해한다. 수치·통화·시점·비교 조건·부정을 보존하도록
   고정 프롬프트에 명시한다. 잘못된 quote/offset, 기업 ID, 수치 추가/누락, 선언한 조건 누락은 error다.
   의미 수준의 분해/의견 구분은 LLM 결과이므로 label 대조 및 사람 검토 대상이다.
4. 혼합 문장은 fact와 opinion/score/decision을 각각 감사 로그에 남긴다.
   facts용 response는 고유 fact statement만 순서대로 합친다. 제외 이유·수량·원문을 보존한다.
5. 같은 sample 안에서 NFKC·공백·끝 마침표만 정규화한 **정확히 동일한 문장**을 합친다.
   본문/표 occurrence는 모두 보존한다. 통화/수치/시점/비교 조건을 정규화해 합치지 않으며,
   의미가 같은 의역도 자동 합치지 않는다. sample 사이의 주장은 각 단계 분모에 별도로 유지한다.
6. 실패 호출·누락 출력·추출 실패는 error/missing이다. 사실 없는 의견/점수 출력은 N/A이며
   opinion rubric/점수 정책/추천 정책 대상으로 연결한다. 정상 빈 응답 또는 0점으로 치환하지 않는다.

최종 sample은 저장된 `report_original.md`와 내용이 일치하는 작성/보강 호출에서만 생성한다.
언어 보강 전 작성 출력과 보강 출력은 intermediate로 남긴다. 저장된 납품 본문을 다시 고치거나
납품 파일과 다른 출력의 위치를 붙여 넣지 않는다.

## 단계 역할 대응

| 논리 역할 | 서비스 경로 | CLI 경로 |
| --- | --- | --- |
| 시장 조사 | analyze_market | market_research |
| 기업 조사 | collect_company_contexts / _search_company | company_research |
| 기술 | evaluate_technology | technical_eval |
| 시장 | evaluate_market | market_eval은 market_traction으로 구별 |
| 사업 | evaluate_business | 별도 단계 미지원 |
| 팀·경쟁·리스크 | evaluate_team/competition/risk | team_eval/competition_eval/risk_eval |
| 판단 근거 | rank_companies | decision |
| 보고서 작성 | generate_investment_report / generate_hold_report | top_report / hold_report |
| 언어 보강 | 별도 단계 미지원 | polish_report_to_korean |
| 추가 조사 | 미지원 | technical_additional_research / market_additional_research |

각 역할 안의 실제 invocation은 합치지 않는다. LLM child의 역할은 부모 단계로 확인한다.
순수 routing·계산·저장·내보내기와 지원하지 않는 역할은 자연어 정확도 N/A이고
기존 workflow/checks 또는 후속 결정론적 규칙 검사 대상으로 남긴다.
CLI 시장/트랙션 결합과 서비스 시장 단계는 같은 논리 역할로 자동 동일시하지 않는다.

## 실제 입력, evidence 및 reference

user_input은 해당 호출의 실제 입력 snapshot이다. Faithfulness는 실제 전달 문맥을 사용한다.
직접 LLM 호출 문맥이 없는 함수/노드는 실제 받은 input_snapshot을 `received_input`으로 구별해
사용하며, validator가 입력 파일의 위치/해시/본문을 재대조한다. 검색 문맥이나 LLM prompt를 만들어내지 않는다.
Context Recall/Precision은 실제 연결된 검색의 query/rank/source ID와 순서를 보존한다.
검색 없는 구간은 unsupported_metric_reasons에 N/A 사유를 남긴다.
원문 evidence 지지와 전달/수신 문맥 지지는 서로 다른 검사 기준이다.

#21의 좁은 reference를 같은 기업·영역이라는 이유만으로 전체 단계에 자동 적용하지 않는다.
`--case-bindings`로 `invocation_id|company_id|section_id → case_id`를 명시해야 한다.
선택한 case와 기업/영역/기준일이 같아야 하고 #21의 검토 완료 데이터만 사용한다.
연결이 없으면 factual metric은 지원되지 않음 사유를 기록한다. 임의로 0점을 만들지 않는다.
해당 단계 출력 전체를 그 reference가 포괄하는지 검토하고 연결해야 한다.
현재 실제 기업 데이터셋은 여전히 사람 검토 pending이므로 이를 factual 평가에 바로 사용할 수 없다.

## 오류 전파 및 추가 조사

실제 delivered_context의 upstream_invocation_ids, 재평가 previous_invocation_id,
납품 artifact와 작성/보강 출력의 동일성을 사용해 대응 sample/claim을 연결한다.
기업·영역이 다른 주장이나 전달 관계 없는 같은 문장을 상위 원인으로 붙이지 않는다.
relation_key가 같은 속성이고 문장이 동일하면 inherited 후보, 상위에 없는 속성은 new 후보다.
같은 속성의 변경 문장이 verified reference 문장과 정확히 일치하면 corrected 후보다.
나머지는 unknown이며, 분해 누락·의역·모호한 원인 가능성을 기록한다.
어떤 후보도 자동으로 사실 판정 또는 책임 귀속을 확정하지 않는다. human_review_status는 pending이고
#27의 독립 review ID를 연결하도록 준비했다. original link/후보는 덮어쓰지 않는다.

추가 조사 전후의 sample, 새 증거와 중복 증거, 관측 score 필드를 보존한다.
관측 값이 없으면 점수 변화는 null이다. 문장이나 점수만 변했다고 조사 성공으로 판단하지 않으며
새 증거 없음은 그대로 `no_new_evidence`로 남긴다. 불확실성 해소/핵심 오류 변화는 #23 이후 판정을 연결한다.

## isolated와 end_to_end

end_to_end는 실제 상위 출력을 받은 기존 실행이다.
isolated는 애초 settings에 `evaluation_mode=isolated`로 기록한 **별도 단독 실행**이어야 한다.
고정 입력/evidence/reference 해시, dataset 버전, evaluator_configuration 해시,
role_mapping 버전 및 target invocation ID를 IsolatedContract로 지정한다.
다른 의미 역할을 실행한 전체 run을 isolated로 이름만 바꾸는 것은 거부한다.
후처리 evaluator의 configuration이 고정 계약과 다르면 평가 실패로 저장한다.
이 이슈는 이미 실행된 단독 run의 adapter를 제공한다. Agent를 다시 실행하는 runner는 #25 범위다.

## RAGAS 내부 trace

RAGAS 0.4.3, FactualCorrectness precision/recall의 atomicity=high, coverage=high와
각 metric 프롬프트를 고정한다. metric별 prompt input/output callback을 실제로 저장한다.
공개 callback에 분해 입력과 원문 claim 문자열이 있으면 metric/attempt/callback별 raw ID를 부여하고
custom claim과 정확한 문자열 일치 후보 및 원문 위치를 연결한다.
response/reference 입력이 같아 방향을 구분할 수 없으면 ambiguous로 남기고 확정 연결하지 않는다.
공개 trace가 없으면 unsupported이며 custom 추출을 RAGAS 내부 결과로 표시하지 않는다.
mapping은 partial 후보이고 의미 동등성·분모·분자·metric 간 공통 claim 집합을 추정하지 않는다.
같은 내부 분해에 중복이 있으면 raw trace에는 중복을 유지한다.

개념 근거: [Factual Correctness](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/factual_correctness/),
[Faithfulness](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/faithfulness/).
실제 구현 호환성은 최신 문서의 API가 아닌 설치된 0.4.3과 테스트로 검증한다.

## 사용

추출 명령은 별도 OpenAI LLM을 사용한다. 아래 placeholder는 실제 동결 run/새 출력 경로로 바꾼다.

```bash
.venv-evaluation/bin/python -B -m evaluation.claim_cli evaluation_runs/RUN_ID --output /tmp/issue22-samples --model gpt-4.1-mini
```

검토된 reference를 연결할 때는 `--dataset DATASET_DIRECTORY --case-bindings /tmp/case-bindings.json`을 추가한다.
단독 run에는 `--mode isolated --isolated-contract /tmp/isolated-contract.json`을 사용한다.
원본 생성 run은 변경하지 않으며 이미 있는 출력 디렉토리는 거부한다.

명시적 label과 custom/native 결과 대조는 외부 API 호출 없이 실행한다.

```bash
.venv-evaluation/bin/python -B -m evaluation.label_cli --labels tests/fixtures/claim_labels_ko.json --package /tmp/issue22-samples/package.json --output /tmp/issue22-label-comparison.json
```

native 결과가 있으면 `--ragas-mappings EVALUATION_DIRECTORY/ragas_claim_mappings.jsonl`을 추가한다.
metric별/attempt별/분해 callback별로 따로 대조하며 RAGAS 점수 분모로 재사용하지 않는다.
체크인 label은 Codex가 수작업으로 명시한 가상 회귀 오라클로 `human_reviewed=false`다.
이를 사람 평가 또는 실제 LLM 정확도 검증 결과로 발표하지 않는다.
실제 평가에서는 검토자의 label과 저장된 LLM 추출을 대조해 누락/과분해/오분류를 확인한다.

사람 검토 baseline 데이터가 포함된 package의 실제 RAGAS 후처리:

```bash
.venv-evaluation/bin/python -B -m evaluation evaluate evaluation_runs/RUN_ID --stage-package /tmp/issue22-samples --model gpt-4.1-mini
```

package 파일 해시, 원문 위치, source hash, facts response 구성, reference 범위와 내용, run 계보를
재대조한 뒤 새 evaluation에 custom claims·stage samples·raw/native 대응 기록을 저장한다.
실제 API를 사용한다. 오류/미지원 sample은 stage_package에 계속 보존되며 임의 점수로 채우지 않는다.

## 검증

```bash
.venv-evaluation/bin/python -B -m unittest discover -s tests -p 'test_evaluation_*.py' -q
```

외부 API 없이 한국어 복합/혼합/의견/점수/표/시점/벤치마크 label, JSON escape 위치,
납품/보강 전후, 중복 occurrence, 기업별 coverage, 실패/누락, 변조,
inherited/new/corrected 후보, 단독 계약, 추가 조사 및 실제 RAGAS 0.4.3 callbacks를 검증한다.
실제 Agent의 baseline 점수 또는 유료 LLM의 일반 추출 품질은 이 PR에서 측정하지 않았다.

2026-10-05 검증 결과: 전체 평가 테스트 **82개 통과**(기존 62개 + #22 신규 20개).
보강 전/후 가상 보고서 smoke test에서도 보관→원문 재검증→label 대조를 실행했다.
4개 sample, 13개 고유 주장, 15개 occurrence, 9개 label 대상 단위가 보존되었고
custom label 대조의 누락/예상 밖 분해는 없었다. 오라클 재생 결과이며 실제 LLM 정확도 수치가 아니다.
