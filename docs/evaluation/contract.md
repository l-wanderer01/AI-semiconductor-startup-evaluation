# Agent 평가 계약

- 계약 버전: 0.1.0
- 관련 이슈: #19, #20, #21, #22, #23, #24, #25
- 작성자: l-wanderer01
- 최종 수정일: 2026.10.05
- 상태: #20~#25 평가 기반에 적용 (실제 reference 사람 검토 및 baseline 평가 대기)

## 1. 목적과 범위

리팩토링 전후 Agent의 품질과 실행 결과를 같은 기준으로 비교하기 위해
실행 식별자, 자료, 중간 결과, 평가 지표, 상태 및 저장 규칙을 정의한다.

평가 대상:
- 최종 보고서의 사실 정확도와 근거 충실도
- 필수 정보의 포함 여부
- 점수 계산과 추천/보류 정책의 정합성
- 개별 Agent 중간 결과와 워크플로우 실행 조건
- 보고서 생성 비용과 시간

범위에 포함하지 않는 것:
- 실제 투자 수익 또는 기업 성공 가능성의 예측 정확도

대상 실행 경로:
- agents: app.py 및 노트북에서 사용하는 서비스
- investment_pipeline: CLI 기반 파이프라인

## 2. 용어와 식별자

| 필드 | 의미 | 예시 또는 규칙 |
|---|---|---|
| run_id | 전체 실행 한 번의 고유 ID | run_<UUID> 형태의 고유 ID |
| case_id | 전후 비교에서 유지하는 평가 사례 ID | 기업 × 영역 × 기준일 |
| company_id | 기업의 안정적인 식별자 | 기업명 변경과 구분 |
| section_id | 평가 영역 식별자 | technology, market 등 |
| agent_id | 논리적인 Agent 역할 | technology_evaluator |
| node_id | 실제 LangGraph 노드 이름 | technical_eval |
| operation_id | 동일 작업의 재시도를 묶는 ID | 작업마다 새 ID, 재시도에서는 유지 |
| invocation_id | 함수·노드·LLM 등의 개별 호출 ID | 호출마다 새 ID |
| parent_invocation_id | 부모 호출 ID | 최상위 호출은 null |
| previous_invocation_id | 재시도 또는 재평가가 이어받은 이전 호출 ID | 최초 호출은 null |
| evaluation_id | 품질 평가 한 번의 고유 ID | evaluation_<UUID>, 원본 run_id에 연결 |
| attempt | 같은 작업의 시도 번호 | 정수 1부터 시작 |
| evidence_id | 생성에 사용한 자료 식별자 | 원문 내용과 출처 식별 정보를 정규화한 SHA-256기반 ID |
| reference_id | 검토된 평가 기준 식별자 | 데이터셋에서 부여한 안정적인 ID, 내용 해시는 별도 저장 |

공통 시장 조사처럼 특정 기업에 속하지 않는 호출은 company_id=null로 기록하고,
적용 대상 또는 범위를 별도 기록한다.

API 실패 후 동일 작업을 재시도할 때는 operation_id를 유지하고 attempt를 증가시키며,
각 시도에 새로운 invocation_id를 부여한다. attempt는 operation_id 안에서 1부터 시작한다.
새 증거를 받아 수행하는 재평가는 새로운 operation_id와 invocation_id를 부여하고 attempt=1로 시작한다.
재시도와 재평가는 previous_invocation_id로 이전 호출에 연결하고, 연결 사유를 retry / reevaluation으로 기록한다.
parent_invocation_id는 호출의 부모·자식 관계만 나타내며 이전 시도와의 연결을 대신하지 않는다.

함수 실행, LangGraph 노드 실행, LLM 호출, 검색 호출은
호출 유형으로 구분한다.

## 3. 실행 메타데이터

각 실행에 다음 정보를 기록한다.

### 코드와 실행 조건

- run_id
- comparison_role: baseline / refactored / standalone
- execution_path: agents / investment_pipeline
- commit_sha
- dirty 여부
- dirty 변경 해시
- 계약 및 schema 버전
- 정책 버전
- 실행 시작·종료 시각
- 자료 기준일
- 요청한 출력 형식

dirty 변경 해시의 포함 범위:
- 추적 중인 파일의 변경:
    HEAD대비 staged와 unstaged 변경을 모두 포함한다.
    포함 대상의 경로와 변경 내용으로 재현 가능한 변경 해시를 만든다.

- 미추적 파일:
    실행 코드, 설정, 프롬프트, 문서 등 허용된 프로젝트 파일을 포함한다.
    경로와 내용 해시를 경로순으로 정렬해 기록한다.

- 비밀정보와 실행 산출물 제외 규칙:
    .env 계열, 인증 파일, 가상환경, 캐시, outputs/,
    evaluation_runs/는 코드 변경 해시에서 제외한다.
    실행에 사용된 입력과 자료는 별도 입력·자료 해시로 기록한다.

### 입력과 설정

- 입력 스냅샷 위치 및 해시
- 자료 스냅샷 위치 및 해시
- 생성 모델과 설정
- 프롬프트 버전 및 해시
- 검색·임베딩 설정
- 캐시 상태: cold / warm / mixed / unknown
- LLM·웹 조사 활성 여부
- fallback 사용 여부와 이유

기록할 설정은 허용 목록으로 관리한다.
API 키, 인증 헤더 및 기타 인증정보는 저장하지 않는다.

## 4. 실행 상태

다음 상태는 서로 독립적으로 기록한다.

| 상태 | 의미 | 제안 값 |
|---|---|---|
| generation_status | 요청 산출물 생성·저장 상태 | not_started / running / succeeded / failed |
| workflow_status | 필수 단계·분기·종료 조건 검사 상태 | not_checked / passed / failed / error |
| quality_evaluation_status | 품질 평가 진행 상태 | not_started / running / completed / partial / failed |

generation_status=succeeded: 필수 산출물이 모두 저장되고, 비어 있지 않으며, 형식에 맞게 읽을 수 있음.
workflow_status=passed: manifest에 정의한 필수 기업·평가·분기·종료 조건을 모두 통과함.
workflow_status=error: 검사기 오류로 판정을 완료하지 못함. 확인된 위반은 별도로 보존함.
quality_evaluation_status=completed: 적용 대상 필수 평가를 모두 완료함.
quality_evaluation_status=partial: 일부 완료됐지만 missing 또는 error인 평가가 남음.
quality_evaluation_status=failed: 평가 실패로 사용 가능한 필수 평가 결과가 없음.

fallback은 별도 필드로 기록한다.
fallback을 사용했다는 이유만으로 성공 또는 실패를 자동 결정하지 않는다.

전체 실행 성공 조건:
generation_status=succeeded이고 workflow_status=passed이면 보고서 생성 실행이 성공한 것으로 판정한다.

quality_evaluation_status는 별도로 기록한다.
품질 평가 완료 여부와 품질 점수의 높고 낮음은 구분한다.

예외가 없었다는 이유만으로 전체 실행을 성공으로 처리하지 않는다.

## 5. 자료 구분과 연결

| 자료 종류 | 의미 |
|---|---|
| source_evidence | 수집한 원문 자료 |
| retrieval_candidates | 검색에서 반환된 자료와 순위 |
| delivered_contexts | 실제 생성 호출에 전달한 문맥 |
| reference | 사람이 검토한 평가 기준 사실 |

각 자료에는 ID, 원문 또는 스냅샷 위치, 해시,
출처와 시점 정보를 가능한 범위에서 연결한다.

자료를 수집했다는 사실과 LLM에 전달했다는 사실을 구분한다.
reference를 생성 문맥에 자동으로 추가하지 않는다.

원문을 요약해서 전달한 경우:
- 원문 evidence ID를 보존한다.
- 전달된 요약문을 별도로 보존한다.
- 요약 생성 호출과 이후 사용 호출을 연결한다.

## 6. 호출과 이벤트 기록

호출마다 다음을 기록한다.

- operation_id, invocation_id 및 parent_invocation_id
- previous_invocation_id 및 연결 사유
- 호출 유형, agent_id, node_id
- company_id, section_id, attempt
- 실제 입력·출력 스냅샷 위치 및 해시
- 입력·출력 schema 버전
- evidence ID와 전달 문맥
- 모델·프롬프트 버전
- 시작·종료 UTC 시각
- monotonic 기준 경과시간
- 오류 유형 및 메시지

입력 스냅샷은 호출 전에 기록한다.
같은 state 객체가 수정되어도 원래 입력을 보존한다.

이벤트 유형:
- start
- end
- failure
- retry
- skip
- fallback
- route_selected
- state_updated

재시도와 재평가 결과는 이전 결과와 연결하고 덮어쓰지 않는다.

## 7. 평가 단위와 RAGAS 입력

기본 평가 단위:
- 기업 × 평가 영역
- 시장 개요는 별도 case
- 중간 결과는 Agent 역할과 호출 회차를 추가로 구분

sample 연결 정보:
- sample_id
- run_id, evaluation_id, case_id
- company_id, section_id
- agent_id, node_id, operation_id, invocation_id, attempt
- stage: intermediate / final
- mode: isolated / end_to_end

RAGAS 입력:
- user_input: 해당 단계의 실제 요청
- response: 평가 대상 출력
- retrieved_contexts: metric별로 아래 연결 규칙에 따라 선택한 문맥
- reference: 같은 기업·영역·기준일의 검토된 기준 사실

metric별 문맥 연결 규칙:
- Faithfulness는 평가 대상 생성 호출에 실제 전달된 delivered_contexts를 사용한다.
  user_input은 실제 생성 input_snapshot 본문이며 reference를 문맥에 추가하지 않는다.
  최종 보고서 작성 시 중간 분석 결과가 전달됐다면 해당 분석문을 보존하고 원문 evidence와의 계보를 연결한다.
  #22의 함수/노드 평가에서는 실제 수신한 input_snapshot을 received_input으로 구별해서 사용할 수 있다.
  원문 위치·해시·본문을 실제 호출 입력에 대조하며 검색/LLM 문맥을 만들어내지 않는다.
- Context Recall/Precision은 실제 검색 호출의 query, 순위 및 원문 source ID가 연결된
  retrieval_candidates를 사용한다. 검색 결과의 순서를 보존한다.
  이 두 metric의 user_input은 실제 RetrievalRecord.query로 지정한다.
- 중간 분석문이나 여러 기업의 문맥 합집합을 실제 검색 결과로 대체하지 않는다.
- 같은 retrieved_contexts 필드라도 metric에 따라 별도 입력을 만들고,
  context_kind, 원인 invocation_id, context/evidence ID 및 문맥 해시를 기록한다.
- reference는 검토된 평가 기준으로 유지하고 생성 문맥에 추가하지 않는다.

필요 입력이 누락된 경우:
- 사실 정확도 평가 대상에 reference가 없으면 missing으로 기록한다.
- 출력이 있어야 하는 단계에서 response가 누락되면 missing으로 기록한다.
- 검색이 없는 계산·라우팅 단계의 검색 품질 지표는 not_applicable이다.
- 검색이 실행됐지만 검색 로그가 누락됐다면 missing이다.
- 평가 API 실패 또는 잘못된 입력 형식은 error이다.
- 각 상태에 reason을 기록하며 값은 null로 저장한다.

## 8. 지표 정의

### 공통 평가 진행 상태와 판정

개별 주장·필수 정보 항목·규칙 검사·RAGAS metric의 evaluation_status는
pending / completed / missing / error / not_applicable로 기록한다.
이 상태는 실행 전체의 quality_evaluation_status와 구분한다.

- pending: 적용 대상이지만 아직 평가를 완료하지 않음.
- completed: 평가가 정상 완료됨. 낮은 점수나 위반 판정도 포함함.
- missing: 평가에 필요한 입력이 누락되어 평가할 수 없음.
- error: 입력 형식 오류 또는 평가·검사 실행 오류로 완료하지 못함.
- not_applicable: 사전 적용 규칙상 평가 대상이 아님. 제외 사유를 기록함.

내용 판정은 verdict, 수치 결과는 value에 기록한다.
evaluation_status가 completed가 아니면 verdict와 value는 null로 둔다.
completed여도 주장 수가 0인 비율 등 값이 정의되지 않는 경우에는 value=null과 사유를 기록한다.
입력 누락 상태 missing과 필수 내용 누락 판정 verdict=missing은 서로 구분한다.

### 8.1 프로젝트 자체 사실 판정

evaluation_status=completed일 때의 verdict:
- verified: 기준 자료가 주장을 확인함
- contradicted: 기준 자료가 주장과 모순됨
- unverifiable: 적용 가능한 근거로 확인할 수 없음

평가 API 실패와 미판정은 위 세 판정에 포함하지 않는다.

- N_total: 평가 대상 주장 수
- N_judged: 세 판정 중 하나가 완료된 주장 수
- N_pending: 미판정 주장 수

불변 조건:
- verified + contradicted + unverifiable = N_judged
- N_judged + N_pending = N_total

완전 평가 시:
- 사실 확인율 = verified / N_total
- 모순율 = contradicted / N_total
- 검증 불가율 = unverifiable / N_total

판정 완료율 = N_judged / N_total

미판정이 있을 때 최종 세 비율은 null로 두고 partial 상태를 기록한다.
분모가 0인 지표는 null이며 not_applicable 사유를 기록한다.

핵심 사실 모순 수와 핵심 사실 검증 불가 수는 별도 저장한다.

### 8.2 RAGAS 지표

다음 원점수를 별도 필드로 저장한다.

- factual_precision
- factual_recall
- faithfulness
- context_recall
- context_precision

RAGAS 점수와 프로젝트 자체 사실 판정율을 동일 값으로 취급하지 않는다.
검색 지표는 검색 진단 결과로 구분한다.

RAGAS가 제공하지 않는 주장 수·분자·분모는 추정하지 않는다.
1-faithfulness를 거짓 정보 비율로 표시하지 않는다.

### 8.3 필수 정보와 규칙 검사

- 필수 정보 충족률 = 정확히 충족한 항목 수 / 적용 대상 항목 수
- 규칙 정합성 = 통과한 검사 수 / 적용 대상 검사 수

필수 정보 항목의 verdict:
fulfilled / missing / incorrect / null
정상 판정은 evaluation_status=completed이며, 미판정·오류·적용 제외이면 verdict=null이다.

규칙 검사의 verdict:
pass / fail / null
정상 판정은 evaluation_status=completed이며, 미판정·오류·적용 제외이면 verdict=null이다.
checks.json의 status는 completed의 verdict인 pass/fail 또는
evaluation_status인 pending/missing/error/not_applicable로 표현한다.

누락이나 검사 오류를 not_applicable로 처리하지 않는다.
미완료 시 최종 집계값의 처리 규칙:
- 적용 대상 항목 중 미판정 또는 검사 오류가 있으면 최종 집계값은 null이다.
- 완료된 항목의 결과와 개수는 보존하고 판정 완료율을 별도로 기록한다.
- 완료율 = 판정 완료 항목 수 / 적용 대상 항목 수
- not_applicable은 사전 적용 규칙과 제외 사유가 있는 경우에만 분모에서 제외한다.
- 적용 대상의 pending/missing/error는 분모에 유지하며 판정 완료 수에는 포함하지 않는다.
- 필수 정보의 verdict=missing은 누락이 확인된 완료 판정이므로 미판정과 구분한다.

### 8.4 비용과 시간

별도 기록:
- 보고서 생성 비용·시간
- RAGAS 평가 비용·시간
- custom 평가 비용·시간

생성 시작 경계:
- agents: 서비스 생성 이전의 요청 실행 시작 시점.
- investment_pipeline: CLI main 진입 시점.
- 노트북: 서비스 초기화부터 포함하는 실행 구간 시작 시점.

생성 완료 경계:
- 경로별 필수 산출물의 저장과 파일 검증이 완료된 시점.
- 실패 실행은 실패까지의 경과시간을 별도로 기록한다.
- 보고서 저장 후 수행하는 품질 평가 시간은 생성 시간에 합산하지 않는다.

측정 범위:
- 위 경계는 애플리케이션 실행 기준이다.
- Python 프로세스 시작과 모듈 import 시간은 포함하지 않으며,
  필요하면 별도 프로세스 전체 시간을 기록한다.

가격표 버전·기준일·통화·계산 방법을 기록한다.
usage 또는 가격을 확인할 수 없으면 비용은 0이 아니라 null로 기록한다.

## 9. 경로별 워크플로우 계약

경로별 manifest에 다음을 정의한다.

- 후보 기업 집합
- 필수 Agent 역할과 평가 영역
- 역할과 실제 노드·함수의 매핑
- 허용 route와 추천/보류 분기
- 점수 척도와 정책 버전
- 반복 상한 및 종료 조건
- 요청 산출물 목록
- 단계별 enabled / supported 여부

미지원 단계와 실행해야 했지만 실패한 단계를 구분한다.
필수 단계의 실패를 not_applicable로 숨기지 않는다.

검사 결과에는 다음을 기록한다.
- check_id
- expected / actual
- status
- reason
- 원인 invocation_id

추가 조사 기록:
- trigger와 질문
- 새 검색 호출
- 신규·중복 evidence ID
- 입력 또는 state 변경
- 재평가 호출
- 종료 사유

새 증거가 없는 경우의 정책:
- 신규 evidence ID가 0건이면 조사 결과를 no_new_evidence로 기록한다.
- 안내 문장 추가와 중복 자료 재사용을 신규 증거 확보로 계산하지 않는다.
- 실제 검색, 신규 증거 확보, 입력 반영, 재평가 여부를 각각 검사한다.
- 현재 동작 보존 검사와 목표 추가 조사 정책 준수 검사를 구분한다.
- 신규 증거 수집이 필수인 정책에서는 안내 문장만 생성한 경우 fail이다.
- 실제 종료·재평가 동작은 baseline 코드 그대로 기록한다.

안내 문장 추가와 실제 증거 수집을 구분한다.
점수 상승만으로 추가 조사 성공을 판단하지 않는다.

## 10. 저장 계약

저장 위치:
evaluation_runs/<run_id>/

| 원본 실행 디렉터리의 파일 | 내용 |
|---|---|
| run.json | 실행 메타데이터, 생성·워크플로우 상태, 종료 당시 품질 평가 상태, 산출물 목록 |
| events.jsonl | 호출·분기·실패·재시도 이벤트 |
| evidence.json | 원문·검색 후보·전달 문맥과 연결 정보 |
| agent_outputs.jsonl | 개별 Agent 중간 출력 |
| decisions.json | 점수·추천·ranking 결과 |
| checks.json | 생성 실행 종료 시 수행한 워크플로우 검사 |
| metrics.json | 생성 비용·시간 및 수집 완료 상태 |
| report_original.md | 실제 최종 생성 보고서 |

요청된 PDF·JSON 등 추가 산출물도 경로와 해시를 기록한다.

최초 품질 평가부터 별도 evaluation_id를 부여하여 다음 위치에 저장한다.
evaluation_runs/<run_id>/evaluations/<evaluation_id>/

| 평가 디렉터리의 파일 | 내용 |
|---|---|
| evaluation.json | 원본 run_id, 평가·reference 버전, 평가 상태, 이전 evaluation_id 및 재개 사유 |
| events.jsonl | 평가 호출·오류·재시도 이벤트 |
| claims.jsonl | 원자 주장과 판정 |
| checks.json | 품질 평가 과정의 정책·필수 정보·워크플로우 재검사 결과 |
| metrics.json | 품질 지표와 분모·완료 상태, 평가 비용·시간 |
| ragas_samples.jsonl | metric별 평가 입력 sample |
| ragas_results.jsonl | metric별 평가 결과와 오류, 지원 범위 내 원응답·trace |

평가 결과는 원본 run_id와 자료 해시를 참조한다. 생성·평가 파일은 위치로 구분한다.
생성 종료 후 최신 quality_evaluation_status는 evaluation.json에 기록하며,
원본 run.json의 종료 당시 상태를 소급 변경하지 않는다.

저장 규칙:
- 같은 run_id의 기존 실행을 덮어쓰지 않는다.
- JSONL 기록은 이력을 추가한다.
- 생성 실행 중 run.json을 갱신하고, 종료 상태와 산출물 목록 확정 후 동결한다.
- 종료 후 재평가는 원본 실행과 연결된 별도 평가 ID로 보존한다.
- 미실행 평가를 빈 정상 결과 또는 0점으로 저장하지 않는다.
- 저장 실패도 실행 실패 정보로 남긴다.

동시 실행 및 중단 시 저장 정책:
- 실행별 고유 디렉터리를 생성하며, 기존 디렉터리가 있으면 재사용하지 않는다.
- 같은 run의 writer는 기록을 직렬화하여 JSONL 행이 섞이지 않도록 한다.
- run.json은 실행 중 임시 파일 작성 후 원자적 교체로 갱신한다.
- 정상 종료 또는 실패 확정 후 원본 실행 디렉터리의 파일을 동결한다.
  evaluations/ 아래에 새로운 평가 디렉터리를 추가하는 것은 허용한다.
- 평가 중에는 evaluation.json을 원자적 교체로 갱신하고 JSONL에 이력을 추가한다.
  평가가 completed/partial/failed로 종료되면 해당 평가 디렉터리의 파일을 동결한다.
- 중단 시 이미 기록된 이벤트와 산출물을 보존한다.
- 종료 이벤트 없이 중단된 실행은 성공으로 간주하지 않는다.
- 생성 재실행은 새 run_id를 사용한다.
- 품질 평가 재개·재평가는 새로운 evaluation_id로 수행하고 이전 evaluation_id에 연결한다.
  이전 결과를 재사용하면 sample별 원래 evaluation_id와 결과 위치를 기록한다.

## 11. 평가기 버전 계약

채택 버전은 RAGAS 0.4.3이며 SingleTurnSample + single_turn_ascore API를 사용한다.
metric별 실제 프롬프트와 지원 한계는 [구현 기록](issue20.md)에 명시한다.

다음 정보를 기록한다.

- RAGAS 버전과 채택 API
- metric 클래스와 모드
- 평가 모델 및 설정
- 프롬프트 해시와 언어 설정
- atomicity / coverage
- retry / cache 설정
- adapter 버전
- trace 지원 범위

자동 평가 결과와 사람 검토 결과는 별도 보존한다.

평가기 또는 reference가 변경되면 전후 결과를 동일 조건으로 재평가하고,
이전 결과도 보존한다.

## 12. 계약 검증 사례

| 사례 | 기대 기록 |
|---|---|
| 보고서 저장 성공, 기업 평가 누락 | generation 성공, workflow 실패 |
| 평가 API 실패 | 품질 평가 미완료, 점수 null |
| 근거 부족 판정 완료 | unverifiable, 평가 오류와 구분 |
| 필수 검사에서 기업 간 자료 혼입 발견 | workflow 실패, 원인 호출 연결 |
| 추가 조사에서 안내 문장만 생성 | 신규 증거 0건, 정책에 따라 검사 |
| 요청 PDF 저장 실패 | generation 실패 |
| LLM fallback 사용 | fallback 이유와 실제 결과 기록 |
| 동시 실행 | 서로 다른 run_id, 파일 충돌 없음 |
| 평가 대상 주장 0건 | 비율 null, 적용 제외 사유 기록 |

#### 사례 1 : 보고서 저장은 됐지만 기업 평가가 누락됨
조건 :
- 평가 대상 기업은 A, B 두 개이다.
- 두 기업 모두 기술 평가가 필수다.
- B기업의 기술 평가 결과가 없다.
- 요청한 보고서와 상태 파일은 모두 저장되었다.
- 품질 평가는 아직 시작하지 않았다.

기대 결과 :
- generation_status: succeeded
- workflow_status: failed
- quality_evaluation_status: not_started
- 실패 검사: B기업의 필수 기술 평가 결과 누락
- 전체 실행 성공: false

#### 사례 2 : 일부 품질 평가의 API 호출 실패
조건:
- 보고서 생성과 워크플로우 검사는 성공했다.
- factual_precision 평가는 완료됐다.
- faithfulness 평가는 API 오류로 완료되지 않았다.

기대 결과:
- generation_status: succeeded
- workflow_status: passed
- quality_evaluation_status: partial
- faithfulness 값: null
- faithfulness 평가 상태: error
- API 오류를 unverifiable 판정으로 변환하지 않는다.

## 13. 미정 사항과 변경 이력

미정 사항:
- RAGAS 채택 버전과 API: 0.4.3 / SingleTurnSample.single_turn_ascore로 확정하고 호환성 테스트를 추가했다.
- metric별 빈 문맥 및 trace: 빈 적용 대상은 N/A, 공개 callbacks 원응답을 보존한다.
  #22에서는 실제 분해 callback이 있는 경우 raw/custom claim 원문 일치 후보와 위치를 partial로 연결한다.
  의미 대응과 분자/분모는 자동 확정하지 않는다. [단계 sample 및 주장 추적](issue22.md)을 참조한다.
- 경로별 workflow manifest와 필수 산출물 목록: evaluation/workflow.py와 issue20.md에 확정했다.
- company_id는 현재 호출 기록의 기업명과 정확히 일치하는 고정 ID를 데이터셋에 저장한다.
  최초 실제 자료는 Groq, Tenstorrent를 사용한다. 명칭/동일 기업 병합이 바뀌면 새 데이터셋 버전과 명시적인 ID 대응이 필요하다.
- reference_id와 fact_id는 case/논리적 사실별로 부여하고, dataset 버전 및 내용 해시와 함께 식별한다.
  의미가 다른 사실은 새 ID를 부여한다. 검토자·범위·시점·원문 변경은 새 버전으로 발행한다.
- #21 데이터셋 파일 계약은 schema_version 0.2.0으로 구분하며 기존 run 계약 0.1.0을 변경하지 않는다.
  verified 사실과 complete 범위 검토를 모두 충족한 released 데이터만 baseline 평가에 사용한다.
  실제 기업 자료의 사람 검토는 아직 pending이다. [데이터셋 사용 절차](issue21.md)를 참조한다.
- 가격표 출처와 버전 관리 방식: #28 비용 계측에서 확정한다.

| 버전 | 변경 내용 | 이유 |
|---|---|---|
| 0.1.0-draft | 최초 초안 | #20 평가 계약 설계 |
| 0.1.0-draft 수정 | 개별 평가 상태·판정 분리, 재시도 식별자, metric별 문맥, 생성·평가 저장 수명주기 명확화 | 계약 리뷰 1~4번 반영 |

| 0.1.0 | 두 경로 계측·workflow·참조 검증·RAGAS 0.4.3 adapter 구현 | #20 실행·저장 기반 완성 |
| #22 sample schema 0.3.0 | 원자 주장·원문 위치·단계 sample·후보 계보·received_input 확장 | run 계약 0.1.0과 별개로 후처리 보관 계약을 버전 관리 |
| #23 판정 정책 independent-three-state-v1 | 독립 기준 자료의 3상태·핵심 오류·미완료 집계 및 RAGAS precision/recall 연결 | [정책·사용·표본 검토](issue23.md), 실제 사람 검토 및 baseline 평가 대기 |
| #24 실제 입력 계보 actual-request-query-v2 | 원문/전달 문맥 지지와 인용을 구분하고 실제 검색 query·순위에 진단을 연결 | [정책·사용·검증](issue24.md), 실제 baseline 및 사람 표본 검토 대기 |
| #25 필수 항목 fixed-required-information-v1 / adapter 0.5.0 | 고정 적용 항목·원문 rubric 검사와 RAGAS recall을 독립 저장, 미연결 범위 보존 | [정책·사용·검증](issue25.md), 실제 baseline 및 사람 표본 검토 대기 |

## 버전별 규칙 정합성 (#26)

[규칙 검사 정책·K·저장·재검사](issue26.md)를 참조한다.
현재 동작 보존과 명시된 목표 준수를 정책 버전별로 분리하고,
`checks.json`과 `rule_conformance.json`에 저장한다. RAGAS 점수와 K를 합치지 않는다.
