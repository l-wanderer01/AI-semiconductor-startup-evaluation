# #29 불변 baseline과 품질 중심 paired harness

`evaluation.paired_cli`는 #20~#28의 실제 생성 로그, #22 stage package, RAGAS adapter, 독립 사실·필수 정보·grounding 검사, workflow/rule 검사와 runtime을 연결한다. 생성 결과를 바꾸는 평가 gate나 가중 종합 점수는 추가하지 않는다. 기존 두 생성 경로는 experiment scope 밖에서 그대로 동작한다.

## 코드 변경 전 baseline

1. **제품 품질 수정 전에** 기존 진입점을 실행한다. 계측이 없는 이전 commit이면 계측 전용 diff를 적용하고 `--instrumentation-only --note ...`로 기록한다. 현재 저장소에는 #20~#28 계측이 이미 있다.
2. 종료된 run의 `run.json`, code/settings/environment/input/evidence snapshot, 호출·중간 결과, 원본 MD/PDF/JSON, 규칙/시간/비용 파일을 먼저 보존한다. 생성 실패도 분모에서 제거하지 않는다.
3. 아래 capture는 전체 생성 파일의 SHA-256 inventory를 외부 receipt에 기록한다. 생성 디렉터리의 `.frozen`과 기존 저장 계층은 덮어쓰기를 막고, receipt는 파일 수정·삭제·추가를 탐지한다. OS 관리자에 대한 보안 경계가 아닌 재현성 계약이다. `evaluations/`만 inventory에서 제외하므로 이후 후처리는 생성 원본을 변경하지 않는다.

```sh
.venv-evaluation/bin/python -m evaluation.paired_cli capture evaluation_runs/BASELINE_RUN \
  --output evaluation_runs/baseline-receipt.json
.venv-evaluation/bin/python -m evaluation.paired_cli export-search evaluation_runs/BASELINE_RUN \
  --output evaluation_runs/frozen-search.json
```

처음부터 고정 자료 실험을 시작하려면 `{"schema_version":"search-replay-v1","entries":[]}` 또는 검토된 검색 fixture를 먼저 만든다. 각 entry는 `operation="TavilySearchClient_search"`, `query`, `args=[]`, `options={"category":"technology"}`와 `results`(ResearchEvidence JSON 목록)를 가진다. 날짜/검색 옵션까지 exact match다. 기본 `days`를 명시적으로 전달한 호출은 fixture에도 명시한다. 같은 query의 반복 결과가 달라졌다면 export가 실패하며 사람이 고정 fixture를 선택해야 한다. 로컬 입력/문서도 양쪽에 동일한 내용을 공급한다.

## frozen/live 생성과 Agent 실행

```sh
.venv-evaluation/bin/python -m evaluation.paired_cli run \
  --entrypoint investment_pipeline.cli:main --kwargs evaluation_runs/generation-kwargs.json \
  --role baseline --data-mode frozen --search-snapshot evaluation_runs/frozen-search.json
# 원본 capture 후 제품 변경, 같은 입력으로 refactored 생성
.venv-evaluation/bin/python -m evaluation.paired_cli run \
  --entrypoint investment_pipeline.cli:main --kwargs evaluation_runs/generation-kwargs.json \
  --role refactored --data-mode frozen --search-snapshot evaluation_runs/frozen-search.json \
  --baseline-receipt evaluation_runs/baseline-receipt.json
```

kwargs 파일은 함수 인자를 모두 명시한다. 예:

```json
{"input":"data/sample_companies.json","output":"outputs/paired_report.md","domain":"AI semiconductor","live_research":true,"llm_enrichment":false,"polish_korean":false}
```

실제 입력 파일은 해당 데이터셋 경로로 교체한다. harness는 JSON의 Path 인자를 자동 변환한다. 검색 replay는 API 키/검색 활성화 여부와 무관하게 실제 typed 결과를 주입하고 누락된 query는 예외로 종료한다. 실패 후 실제 웹으로 넘어가지 않는다. generation API 비용에는 replay의 실제 캐시 비용 0만 포함한다. 라이브 실험은 `--data-mode live`와 replay 없는 별도 실행으로 수집하고 생성 함수의 `live_research=true`를 사용한다. agents 경로는 웹 검색을 지원하지 않는 현 정책을 manifest에 그대로 표시한다.

`module:function` entrypoint는 기존 RunRecorder integration을 사용하는 함수여야 한다. `agents.service` 사용은 해당 서비스를 초기화하고 `run`하는 작은 프로젝트 wrapper를 entrypoint로 제공한다. harness가 생성한 run 디렉터리 목록을 JSON으로 출력하며 recorder를 만들지 않은 entrypoint는 거부한다. 실행 원본 `experiment.json`에 role·자료 모드·entrypoint·인자·검색 snapshot 해시·baseline ID를 남긴다.

개별 Agent도 기존 `traced`/`RunRecorder`를 사용하는 wrapper에서 **새 run을 실제 실행**한다. 이미 존재하는 end_to_end run의 mode만 isolated로 바꾸지 않는다. #22 `IsolatedContract`의 고정 입력·증거·reference·평가기 hash와 target invocation ID를 사용해 stage package를 만든다. end_to_end는 중간 입력 변화도 비교 열에 남긴다. 동일 논리 역할/영역이 여러 번 실행되면 case binding으로 회차를 구별해야 한다. 모호한 동일 pairing key는 거부한다.

isolated runner의 settings에 `evaluation_mode="isolated"`와 `evaluator_configuration_sha256`을 넣는다. 후자는 `evaluation.paired_cli.evaluator_fingerprint(adapter, factual_judge, support_judge, coverage_judge)`로 실제 adapter 및 custom judge 조합의 해시를 계산한다. 동일 해시를 양쪽 계약에 고정하되 입력/evidence 인덱스와 target invocation ID는 각각의 실제 run에서 읽는다. 실제 고정 증거 실행의 원문 내용이 같아도 수집 시각/절대 경로가 다른 인덱스 해시는 변화를 표시하고 원문 무결성과 내용 동일성을 별도로 검증한다.

## 동일 평가기와 재개

#22 명령으로 양쪽 stage package를 먼저 준비한다. 기업/영역/case binding, role mapping, 추출기 설정, 변환 버전을 일치시킨다. reference는 #21의 사람 검토/발행 완료 데이터셋을 사용한다.

```sh
.venv-evaluation/bin/python -m evaluation.claim_cli evaluation_runs/BASELINE_RUN \
  --dataset data/evaluation/DATASET --case-bindings evaluation_runs/baseline-bindings.json \
  --output evaluation_runs/baseline-package
.venv-evaluation/bin/python -m evaluation.claim_cli evaluation_runs/AFTER_RUN \
  --dataset data/evaluation/DATASET --case-bindings evaluation_runs/after-bindings.json \
  --output evaluation_runs/after-package
.venv-evaluation/bin/python -m evaluation.paired_cli evaluate-pair \
  evaluation_runs/BASELINE_RUN evaluation_runs/AFTER_RUN \
  --baseline-package evaluation_runs/baseline-package --after-package evaluation_runs/after-package \
  --dataset data/evaluation/DATASET --baseline-receipt evaluation_runs/baseline-receipt.json \
  --model gpt-4.1-mini --max-retries 1 --output evaluation_runs/paired-attempt-1.json
```

이 명령은 동일 adapter와 동일 custom judge 설정으로 RAGAS factual_precision/recall/faithfulness와 지원되는 검색 진단, 독립 사실 판정, 실제 문맥·근거·인용 검사, 필수 정보 검사를 양쪽에 수행한다. 비용은 각 목적의 runtime으로 분리된다. 가격/usage 미확인은 null이고 일부 알려진 금액을 전체 비용으로 합산하지 않는다.

중단/평가 실패 시 journal에 생성된 평가 ID와 실패 사유를 보존한다. 같은 명령에 `--resume evaluation_runs/paired-attempt-1.json --reason "timeout 재시도" --output evaluation_runs/paired-attempt-2.json`을 추가한다. 재개도 **양쪽 새 ID**로 재평가한다. 성공/실패했던 기존 평가를 덮어쓰지 않으며 previous ID를 연결한다. reference·package 내용·모델/프롬프트/분해/재시도 설정 변경은 resume가 거부한다. 새 reference는 양쪽 package를 새로 준비하고 새 attempt로 BOTH 평가한다. isolated는 새 계약의 evaluator hash까지 일치시켜야 한다.

## JSON·Markdown·CSV 비교와 반복 실험

```sh
.venv-evaluation/bin/python -m evaluation.paired_cli compare \
  evaluation_runs/BASELINE_RUN/evaluations/BASELINE_EVAL \
  evaluation_runs/AFTER_RUN/evaluations/AFTER_EVAL \
  --data-mode frozen --baseline-receipt evaluation_runs/baseline-receipt.json \
  --output evaluation_runs/comparison-1
.venv-evaluation/bin/python -m evaluation.paired_cli campaign evaluation_runs/campaign.json \
  --output evaluation_runs/campaign-report.json
```

campaign manifest는 모든 시도 실행을 담은 JSON 배열이다. 각 행에 `attempt_id`, `data_mode`, `baseline_run`, `after_run`, `baseline_receipt`, 선택적 `baseline_evaluation`/`after_evaluation`와 실패/미평가 `reason`을 기록한다. 동일 run 재사용을 반복 횟수로 부풀리지 못하며 frozen/live는 별도 그룹이다. 성공률 분모는 평가 완료 run만이 아니라 manifest의 **전체 시도 run**이다. 각 그룹의 생성 시간/비용은 실행 commit·설정·경로·성공/실패·fallback·cold/warm/mixed/unknown·통화·가격표별로 분리한다. 실제 반복 횟수, 관측 수, 중앙값과 **20개 이상 관측된 시간에만 nearest-rank p95**를 출력한다. 평가 운영도 생성 보조 지표와 별도 그룹이다. 품질 분석은 각 attempt의 독립 paired report로 남긴다.

- pairing key: case_id × logical_role × company × section × intermediate/final × isolated/end_to_end × attempt. reference hash, dataset/evaluator configuration이 다르면 중단한다. sample preparation 버전도 양쪽 동일해야 한다.
- frozen은 실제 입력 snapshot과 evidence 내용/출처/기업/시점이 같아야 한다. evidence 수집 시각과 run별 절대 파일 경로로 인해 달라지는 index hash는 그대로 표시하되 내용 동일성 검사와 구분한다. live/end_to_end 입력 변화는 숨기지 않는다.
- RAGAS 주 지표·custom 모순/검증 불가·핵심 오류·주장 수·판정 완료율·필수 정보·rule_conformance는 독립 열이다. 값 0, null, error, missing, unsupported, N/A는 구분한다. 비율 diff는 %p, 건수는 건이다. context 지표는 실제 검색별 diagnostics에서 읽는다.
- 기업/영역별 sample 평균과 기업/영역 macro, 완료 상태별 수, 전체 custom claim counts/micro, 기업별 claim share·50% 초과 편중 표시를 분리한다. RAGAS raw counts가 없으면 claim-weighted를 만들지 않는다. 중간/최종 주장은 합산하지 않는다.
- workflow manifest/hash, 필수 기업/평가 coverage, route·분기·저장/파싱 검사, 호출 실패·재시도·fallback, 미지원 단계와 조사 기록을 보존한다. 추가 조사는 새 검색·신규 증거·입력 갱신·재평가·종료를 확인하며 no_new_evidence와 정책을 표시한다. 점수 상승은 조사 성공의 필수 조건이 아니다.
- error lineage의 inherited/new/corrected는 후보이며 실제 사실 판정·원본 근거 경로를 함께 표시한다. 자동 귀속을 확정 인과로 표현하지 않는다. 최종 단계에서 수정된 주장을 중간 오류와 별도로 볼 수 있다.
- 독립 품질 축이 개선되어도 workflow 실패가 있으면 `overall_agent_success=false`다. 미평가·미짝지어진 sample은 숨기지 않고 `quality.complete=false`로 표시한다. 전체 성공 판정은 비용/속도와 독립이다.

## 이번 작업의 baseline과 검증 범위

품질 코드 수정 전 `02153af83b54a9951116d3a347193eeaf0068e2f`에서 `run_8cb0820e88b34dd2a4b620c291be7e6c` 합성 오프라인 baseline을 저장했다. 원본·32개 생성 파일의 receipt는 로컬 `evaluation_runs/issue29-prechange-receipt.json`에 있다. 실제 투자 품질/사람 검토 데이터가 아니며 필수 Agent coverage가 부족해 workflow FAILED다. 생성 변경을 한 것으로 주장하지 않으며 제품 알고리즘은 이번 작업에서 변경하지 않았다.

재현 가능한 synthetic demonstration과 예시 보고서는 별도 `tests.issue29_demo`로 생성한다. 실제 RAGAS 0.4.3과 고정 LangChain 응답, 한국어 synthetic reference/claim oracle를 사용하며 **실모델 품질 검증 결과로 해석하지 않는다**. 원본 중간 결과·report 저장 실패/기업 누락·혼입·추가 조사 안내문·route 오류·오류 정정 검증은 기존 #20/#22/#26 테스트를 함께 실행한다.

검토용 [합성 예시 Markdown·JSON·CSV 및 사전 baseline receipt](examples/issue29/README.md)를 저장소에 포함했다. 전체 runtime 원본은 위 재현 명령으로 별도 새 실행 ID에 만든다.

```sh
.venv-evaluation/bin/python -m tests.issue29_demo
.venv-evaluation/bin/python -m unittest discover -s tests -q
git diff --check
```

온라인 LLM/Tavily 평가와 실제 투자 품질 개선 주장은 수행하지 않았다. 실행하려면 승인된 가격표·자격증명·검토 완료 dataset을 준비한다. 환경 변수나 인증 헤더 원본은 manifest에 넣지 않는다.
