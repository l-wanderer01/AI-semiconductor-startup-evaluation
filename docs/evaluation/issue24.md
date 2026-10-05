# #24 실제 문맥 지지·검색 진단·인용 검사

작성 기록: Codex가 위임받은 평가 기반을 구현했다. Agent 생성·검색 동작,
투자 점수·가중치·추천 정책을 변경하지 않는다. `recording.py`의 변경은 전역 보고서 호출에
실제로 들어간 기업별 출력의 관측 계보를 보존하는 것뿐이다.

## 지표가 답하는 질문

| 검사 | 입력과 의미 |
| --- | --- |
| #23 factual precision/recall 및 3상태 | 독립 검토 완료 reference에 비춰 사실이 맞고 필요한 기준 사실을 포함하는가 |
| RAGAS Faithfulness | 실제 생성 입력·response·실제 전달 문맥이 얼마나 일관되는가 |
| custom source_evidence grounded | 그 단계와 실제 상위 전달 계보에 연결된 원문 evidence가 주장을 지지하는가 |
| custom delivered_context grounded | 실제 전달한 중간 분석문/원문/함수 입력이 주장을 지지하는가 |
| Context Recall | 해당 query에서 검색한 문맥이 기준 주장들을 확보했는가 |
| reference 기반 Context Precision | 실제 ranked retrieved context에서 기준 답변에 유용한 결과가 앞에 있는가 |
| 인용 검사 | 그 주장에 붙은 출처의 보관 원문이 그 주장을 지지하는가 |

Faithfulness와 custom grounded는 사실 정확도가 아니다. 원문 자체가 틀리면 충실하게
따른 응답도 실제로 틀릴 수 있다. `1-Faithfulness`는 문맥 비지지 정도로만 저장하며
참/거짓·모순율이나 인용 오류율로 표시하지 않는다.

## 실제 입력과 계보

Faithfulness user_input은 해당 생성 invocation의 실제 input_snapshot 본문이다.
response는 #22의 고유 사실용 response이고, 전체 원문 및 제외 내역은 그대로 보존한다.
delivered_contexts의 ID·내용·순서를 그대로 전달한다. 함수/노드는 실제 수신한 snapshot을
received_input으로 구분한다. reference/reference_ids는 Faithfulness 입력에서 비워 두며
독립 정답을 새 문맥으로 추가하지 않는다. 수동 --samples 입력도 실제 생성 입력과 같아야 한다.

원문 지지는 해당 invocation의 evidence IDs와 실제 전달 문맥의 상위 invocation IDs를
따라가며 검사한다. 받은 함수 입력에 원문이 그대로 있으면 decoded 문자열의 정확한 일치로
연결한다. 같은 회사라는 이유로 실행 전체 evidence를 합치지 않는다. 다른 기업을 나타내는
source 메타데이터는 그 기업의 주장 지지로 통과시키지 않는다.

전역 보고서 기록에서는 기업별 출력이 실제 전달 텍스트에 포함됐는지 확인해 연결한다.
다른 기업 출력 전체를 무조건 연결하지 않는다. 기존 동결 run의 기록은 수정하지 않는다.
중간 분석 문맥의 상위 원문 계보가 빠진 이전 기록은 source_evidence 검사에서 missing으로
남길 수 있으며, 이를 0점이나 원문 사실 오류로 치환하지 않는다.

원문/실제 문맥/인용 대상 및 해당 기록을 평가 시작 시 읽어 고정한다. 평가 디렉토리에
별도 text snapshot을 저장하고 document ID·원본 snapshot·해시와 대응시킨다.
긍정과 반증의 quote/문자 offset은 그 보관 본문에 대조한다. snapshot 보관은 기존
run이나 보고서 원문을 덮어쓰지 않는다.

## custom 판정과 인용 정책

`ko-support-judge-v1`의 별도 LangChain structured-output judge가 제공된 모든 document를
한 번씩 비교한다. 실제 원문만 제공하며 독립 reference·외부 지식은 사용하지 않는다.
supported/unsupported/contradicted 제안을 원문 quote/offset, 기업·시점·비교 조건,
숫자·통화·단위·상하한 정책에 다시 대조한다. 숫자가 같아도 PoC/계약 같은 의미 모순을
지우지 않는다. 출처 묶음에 지지와 반증이 함께 있으면 conflict_document_ids를 남기고
grounded=false로 보수적으로 처리한다. 이 의미 비교는 모델 판정이며 사람 검토 상태는 pending이다.

원문 밖 quote, 잘못된 offset, URL/인용 marker만 있는 지지 quote, 비교 대상 ID 누락/중복,
파싱/timeout 오류는 error이며 grounded=null이다. 실제 입력/계보 누락은 missing이다.
정상적으로 자료를 검사했지만 내용이 없거나 조건이 맞지 않으면 grounded=false다.

인용은 #22의 atom과 원문 위치를 사용한다. 명시한 marker 또는 바로 뒤에 붙은 marker를
연결하며, 행 전체에만 있는 후보 인용은 ambiguous/missing으로 남겨 사람 검토를 요구한다.
`[SOURCE:source_id]`, URL, `[1]`과 그 원문 footnote 정의를 대응시킨다.
URL이나 source ID가 존재한다는 이유로 통과하지 않으며 실제 source 본문을 별도로 검사한다.

- supported: 원문 지지와 supporting_evidence_ids 및 원문 구간 있음.
- contradicted: 같은 조건의 명시 반증. 반증 quote/원문 위치를 보존함.
- unsupported: 내용 없음·조건/단위/대상 불일치·상충 근거 묶음.
- inaccessible: 해당 인용의 사용 가능한 보관 원문이 없음. live HTTP 접근 실패나 URL 상태를 추정한 결과가 아님.
- missing/error: 결합 범위 불명확·출력 실패 또는 평가 오류. 완료된 내용 판정과 구별함.

인용 분모는 **고유 (claim_id, cited_source_id) 쌍**이다. 같은 출처의 marker/URL 반복은 합친다.
인용 없는 주장 수는 별도로 기록하고 인용 지지율 분모에서 제외한다.
inaccessible은 적용 가능한 인용의 비지지 항목으로 분모에 포함한다.
missing/error/ambiguous가 있으면 최종 support_rate는 null이다. 인용 없는 sample도 비율은 null이다.
필요한 URL을 자동으로 다시 요청하거나 새 검색으로 baseline 근거를 바꾸지 않는다.

## 검색 진단과 버전

RAGAS `0.4.3`, adapter `0.4.0`, 입력 계보 정책 `actual-request-query-v2`,
custom 정책 `actual-context-citation-v1`을 고정한다. `Faithfulness`, `LLMContextRecall`,
`LLMContextPrecisionWithReference`의 실제 prompts/schema·모델·API 설정과 원점수를 보관한다.
custom prompt 및 모델 설정도 evaluator configuration 해시에 포함하므로 isolated 고정 계약은
결합 해시와 같아야 한다. 기존 #23 custom-factual과 함께 실행할 수 있다.

Context Recall/Precision user_input은 생성 질문이나 dataset case 질문을 복사하지 않고
**실제 RetrievalRecord.query**를 사용한다. 한 검색 invocation의 후보 ID·순위·source ID·본문
순서를 보존한다. query/본문 순서/evidence IDs가 다른 입력 또는 실패 검색은 검증에서 거부한다.
같은 sample에 여러 검색이 연결되면 invocation별로 별도 평가하고 합집합을 만들지 않는다.
중간 요약을 검색 후보로 바꾸지 않는다. reference 또는 연결된 검색이 없으면 N/A 사유를 남긴다.

공식 개념 설명: [Faithfulness](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/faithfulness/),
[Context Recall](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/context_recall/),
[Context Precision](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/context_precision/).
호환성은 최신 문서가 아닌 설치된 0.4.3의 기존 single_turn_ascore API로 검증한다.

## 실행과 저장 파일

새 CLI 선택 사항은 --custom-grounding과 --grounding-model이다. 신규 의존성·환경변수는 없다.

```bash
.venv-evaluation/bin/python -B -m evaluation evaluate evaluation_runs/RUN_ID \
  --stage-package /path/to/stage-package \
  --custom-grounding --grounding-model gpt-4.1-mini
```

실제 모델 명령은 RAGAS와 custom 평가용 OpenAI 호출을 수행한다. temperature=0,
timeout=120, provider retries=0이며 생성 Agent는 다시 실행하지 않는다.
reference 없는 #22 패키지도 문맥/인용 검사에 사용할 수 있다. 이 경우 manifest의
dataset_version은 no-reference-context-only-v1이고 reference_sha256은 reference 부재를
명시한 JSON marker의 해시다. 실제 정답 자료가 있다는 의미가 아니다. factual 및 검색 지표는
reference가 준비될 때까지 N/A다. 데이터셋이 있으면 기존 baseline 사람 검토 계약을 유지한다.

새 evaluation 디렉토리에 기존 RAGAS 결과와 다음을 저장한다.

- grounding_documents.jsonl: 원문/전달/received_input 유형, 실제 IDs, 원본·보관 snapshot 대응 및 본문 해시.
- grounding_assessments.jsonl: claim/sample/case/basis별 grounded, 지지 evidence/context IDs,
  지지·반증 원문 구간, 비교 제안·실효 관계·이유·raw output·평가기/정책 버전.
- citation_assessments.jsonl: marker 결합 범위, 출처별 인용 판정·원문·이유·버전 및 검토 상태.
- grounding_metrics.json: sample별 두 basis의 분모·지지율 및 인용 분모/상태 count/support_rate.
- grounding_diagnostics.jsonl: 실제 Faithfulness와 1-Faithfulness, source/upstream 계보,
  검색 invocation·query·ranked candidates·Context Recall/Precision 원점수와 미지원 사유.
- custom_grounding_configuration.json: 고정 custom prompt/schema/model 및 정책 해시.

설정·집계 snapshot 및 diagnostics 행은 기존 SnapshotPayload의 data 안에 본문을 저장한다.
판정/document 행은 모델을 직접 직렬화한다. sample의 intermediate/final을 분리해 보존한다.
grounding/citation 입력 누락이나 평가 오류가 있으면 전체 evaluation 상태도 completed가 아니다.

## 검증과 실제 평가의 남은 작업

한국어 가상 사례로 참이지만 문맥에 없음, 잘못된 요약 반복, 틀린 원문 충실 인용,
필요 근거 누락, 단위/조건/기업 불일치, URL만 있는 가짜 지지, 반증, 보관 본문 없음,
footnote 대응, 출처 반복과 ambiguous 결합, 실패 generation 및 evaluator error를 검증한다.

설치된 RAGAS API와 deterministic fixture LLM으로 관련 문서가 2위일 때 Context Precision=0.5,
1위일 때 약 1, 기준 근거 누락 시 Context Recall=0을 확인한다. 실제 run의 query·순위·source ID와
교체 차단, 기록 보관·동결 및 원본 불변도 검증한다. fixture는 Codex가 작성한 기대값이며
실제 모델 정확도나 사람 검토 결과가 아니다.

```bash
.venv-evaluation/bin/python -B -m unittest discover -s tests -p 'test_evaluation_*.py' -q
```

실제 Agent baseline, 유료 API 평가 및 한국어 모델 판정의 사람 표본 검토는 실행하지 않았다.
문맥 지지와 원문 지지/독립 사실 판정이 서로 다른 주장을 우선 검토하고, 확정 결과는 #27의
HumanReviewRecord로 원판정을 덮어쓰지 않고 연결해야 한다.
