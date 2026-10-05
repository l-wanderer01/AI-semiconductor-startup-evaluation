# 이슈 #20 구현 및 검증 기록

#20은 실제 평가 결과를 저장·추적할 수 있는 기반이다. 검증된 데이터셋(#21),
보고서/중간 결과의 자동 sample 생성·주장 추출(#22), 프로젝트 사실 판정(#23),
인용 검사(#24), 필수 정보 검사(#25), 점수 공식의 독립 재계산(#26),
사람 검토와 평가기 신뢰도(#27), 실제 비용 계산(#28), 대표 baseline 비교(#29)는 후속 작업이다.

## 구현 범위

| 요구사항 | 구현·검증 위치 |
|---|---|
| 지표별 분모, 0/미완료/N/A 분리 | contract.md, models.py, 모델 테스트 |
| 생성·workflow·품질 평가 상태 분리 | RunManifest, EvaluationManifest, recording.py |
| run ID, commit, dirty 상태/계측 변경 해시, 입력·설정·자료·프롬프트 버전 | RunRecorder, code_snapshot.json, settings_snapshot.json |
| 최초 저장, JSONL, 원자적 갱신, 잠금, 동결 | storage.py, 저장 테스트 |
| 함수/노드/LLM/검색 및 부모 호출, 기업별 중간 결과 | recording.py, integration.py, 두 제품 경로의 계측 decorator |
| 실제 검색 순위와 실제 LLM 입력 분리 | retrievals.jsonl, delivered_contexts.jsonl, 실제 프롬프트 snapshot |
| 경로별 역할/단계·분기·필수 산출물·종료 조건 | workflow.py, workflow.json, policy_snapshot.json |
| 누락·기업 혼입·분기 오류·반복·추가 조사·저장 실패 | checks.json, 추가 조사 기록, 기록/진입점 테스트 |
| 파일 간 ID 및 실제 snapshot 해시 검증 | validation.py, validation.json |
| RAGAS 버전 및 metric/API 고정 | requirements-evaluation.txt, ragas_adapter.py |
| metric별 입력 검증·점수/오류/재시도/trace 저장 | RagasAdapter, evaluate_inputs, 실제 RAGAS fixture 테스트 |
| 생성 원본 보존 및 독립 후처리 평가 | evaluations/<evaluation_id>/, 동결/재평가 테스트 |

## 실행 환경

기존 `.venv`를 읽기 전용으로 참조하는 `.venv-evaluation`을 사용한다.
평가 환경에 추가 설치된 의존성은 기존 Agent 환경을 수정하지 않는다.
준비 스크립트는 generation site-packages를 .pth로 참조하므로 기존 .venv 위치를 유지해야 한다.
생성 의존성은 requirements.txt, 추가 의존성은 requirements-evaluation.txt로 고정한다.

```sh
.venv/bin/python -m evaluation.setup_environment
.venv-evaluation/bin/python -m pip check
.venv-evaluation/bin/python -B -m unittest discover -s tests -p 'test_evaluation_*.py' -v
```

테스트는 실제 RAGAS 0.4.3과 고정 응답 LLM, 실제 두 LangGraph 경로,
실제 PDF 저장/파싱을 사용한다. OpenAI·Tavily 요청이나 embedding 다운로드를 수행하지 않는다.
이 테스트의 통과는 평가 기반의 동작 검증이며 실제 보고서 정확도 또는 한국어 평가기 신뢰도 측정은 아니다.

## 두 진입점의 수집

```sh
.venv-evaluation/bin/python app.py
.venv-evaluation/bin/python -m investment_pipeline.cli --help
```

app.py/노트북의 InvestmentAnalysisService는 생성자 시작부터 기록한다.
생성자만 호출하고 run을 호출하지 않은 경우 running 상태로 남으며 성공 실행으로 취급하지 않는다.
두 번째 service.run은 새 실행 ID를 만든다. 재사용된 embedding/vectorstore의 초기화 시간은
두 번째 실행에 포함되지 않으며 cache 상태가 확인되지 않으면 unknown이다.

CLI는 main 진입부터 요청 형식인 Markdown·PDF·JSON 저장까지 기록한다.
모듈 import 시 수행된 초기화 시간은 이 경계 밖이며 #28에서 전체 요청 경계를 확장할 수 있다.
현재 경과시간에는 계측·저장 오버헤드도 포함되므로 계측 전 실행의 성능 수치와 바로 비교하지 않는다.
직접 run_pipeline()만 호출하면 기존처럼 결과를 반환한다. 기록이 필요하면
RunRecorder.activate() 안에서 호출하거나 CLI를 사용한다.

service의 recommendation threshold·top_k와 pipeline의 Stage 정책·threshold는 그대로 사용한다.
CLI의 polish_korean 옵션과 실제 동작이 다른 기존 동작도 수정하지 않았다.
LLM enrichment가 켜져 있을 때 수행되는 polish 호출을 실제 실행대로 기록한다.

## 생성 디렉터리

`evaluation_runs/<run_id>/`에 다음을 보존한다.

- run.json, input/request/settings/code/environment snapshot
- workflow.json, policy_snapshot.json, checks.json, validation.json
- events.jsonl, invocations.jsonl, agent_outputs.jsonl
- evidence.json 및 원문, retrievals.jsonl, delivered_contexts.jsonl
- 호출별 입력·출력·실제 프롬프트 snapshot
- additional_research.json, decisions.json, metrics.json
- report_original.md 및 요청된 PDF/JSON과 service의 기존 중간 산출물 복사본

실제로 호출되지 않은 파일 종류는 생성하지 않을 수 있다. 미실행 claims 파일은 비어 있으며
품질 상태는 not_started다. metrics의 품질·비용 필드는 null이고 0점/0원으로 대신하지 않는다.

종료 시 파일 참조와 해시를 검증한 뒤 동결한다. generation_status는 산출물 생성 조건,
workflow_status는 coverage·분기·추가 조사 등 실행 조건을 나타낸다.
생성 성공이 workflow 성공을 의미하지 않는다.
동결은 이 저장 API의 수정 방지 규칙이며 OS 파일 권한을 잠그는 기능은 아니다.

```sh
.venv-evaluation/bin/python -m evaluation validate evaluation_runs/<run_id>
```

추가 조사는 현재 기술/시장 노드가 안내문만 생성하므로 supported=false와
no_new_evidence로 기록하며 workflow 검사에서 실패를 드러낸다.
입력에 안내문이 반영되는 것과 새 증거가 확보되는 것은 별도다.
현재 경로를 바꾸지 않고 뒤따른 재평가 호출 ID도 보존한다.

## RAGAS API와 지원 범위

RAGAS 0.4.3의 SingleTurnSample + single_turn_ascore 경로를 고정했다.
라이브러리가 해당 클래스를 `ragas.metrics`에서 deprecated로 재노출하므로
고정 버전의 구현 모듈을 명시적으로 import한다. 버전 변경 시 호환성 테스트가 필요하다.

| 프로젝트 지표 | 채택 구현 | 입력 문맥 |
|---|---|---|
| factual_precision | FactualCorrectness(mode=precision) | 검증 reference, 생성 문맥 없음 |
| factual_recall | FactualCorrectness(mode=recall) | 검증 reference, 생성 문맥 없음 |
| faithfulness | Faithfulness | 실제 전달 문맥 |
| context_recall | LLMContextRecall | 실제 검색 결과 순위 |
| context_precision | LLMContextPrecisionWithReference | 실제 검색 결과 순위 |

FactualCorrectness는 atomicity=high, coverage=high를 고정한다.
원래 예시·schema를 유지하면서 한국어 숫자/단위/조건 보존 지시를 명시적으로 추가하고,
실제 prompt instruction·예시·schema·언어와 해시를 저장한다.
한국어 사람 검토를 통과했다고 주장하지 않는다. 그 검증은 #27의 작업이다.

single_turn_ascore는 원점수 float를 반환한다. 공개 callbacks가 제공한 원응답과
중간 chain 출력은 보존하지만, raw/custom claim ID 대응과 분자·분모의 자동 추출은
미지원으로 명시한다. 점수에서 주장 수를 역산하지 않는다.

API 오류는 error/null, 입력 누락 또는 잘못된 lineage는 missing/null,
빈 적용 대상은 not_applicable/null, 정상 0은 completed/0으로 기록한다.
라이브러리 NaN은 completed/null + undefined 사유로 보존한다.
평가기 재시도는 호출별 attempt/previous ID로 trace와 이벤트에 기록한다.
adapter_settings.json에 timeout·adapter retry·library retry·prompt parser retry 설정을 함께 고정한다.
service의 SDK 내부 재시도는 공개되지 않으므로 개별 시도를 만들어내지 않는다.

## 준비된 입력의 후처리 평가

RagasSampleInput JSONL은 #21/#22에서 실제 자료와 reference로 준비한다.
검색 지표에는 retrieval invocation ID와 실제 context ID/순서가 필요하고,
Faithfulness에는 원래 LLM invocation의 delivered context ID/내용을 사용해야 한다.
원래 생성 run의 입력·evidence 해시와 일치해야 한다.

다음 명령은 OpenAI 평가 API를 호출한다. 자동 테스트에서는 실행하지 않는다.

```sh
.venv-evaluation/bin/python -m evaluation evaluate evaluation_runs/<run_id> \
  --samples prepared_samples.jsonl \
  --dataset-version <version> \
  --reference-sha256 <verified_reference_sha256>
```

평가마다 `evaluations/<evaluation_id>/`에 evaluator configuration·실제 프롬프트,
evaluation.json, events.jsonl, ragas_samples/results/traces.jsonl과 metrics.json을 저장한다.
custom 주장·정책 검사를 수행하지 않았으므로 해당 결과를 만들어 채우지 않는다.
평가 완료/부분 완료/실패 후 동결하고 원본 run.json과 보고서는 수정하지 않는다.
재평가는 --previous-evaluation-id와 --resume-reason으로 연결한다.
강제 종료나 파일 쓰기 장애로 남은 running 평가는 성공이 아니며,
새 ID로 재실행하고 이전 ID를 연결한다. 기존 결과를 자동 재사용하는 기능은 #29에서 구현한다.

## 현재 검증의 한계

실제 외부 API를 통한 보고서 baseline은 아직 수집하지 않았다.
실제 LLM·검색 비용과 전 요청 경계 계측은 #28에 남아 있다.
기업명·원문/호출 ID·문맥 순서와 저장 결과의 구조적 혼입은 검사하지만,
기업이 다른 사실을 자연어로 주장하는지의 의미 판정은 #23/#24에서 수행한다.
자동으로 연결한 상위 출력은 실제 입력에 문자열이 나타나는 경우의 추적 후보이며
오류 전파의 인과 판정이 아니다.
