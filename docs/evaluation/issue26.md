# #26 버전별 결정론적 규칙 검사

`RunRecorder.finish()`가 생성 결과를 저장할 때 규칙 검사도 자동 실행한다.
제품 점수 함수·추천 함수·LLM을 정답으로 호출하지 않는다. 독립 명세는
`evaluation/policies/rules-v1.json`, 명시적 기대 사례는
`tests/fixtures/rule_cases_v1.json`이다. 실제 제품 상수는 **관측값**으로만
`scoring_policy_observed.json`에 기록하고 명세의 기대 가중치와 대조한다.

## 정책 구분과 미합의 범위

| 버전 | 목적 | 척도 / 공식 | 추천 |
|---|---|---|---|
| agents-baseline-v1 | 현재 동작 보존 | 6개 항목 합, 6~30 | 설정 임계값 이상, 기본 20 |
| investment_pipeline-baseline-v1 | 현재 동작 보존 | 항목 / 5 × Stage 비율 × 100, 20~100; 시장·트랙션 정수 평균 | 설정 임계값 이상, 기본 65 |
| investment_pipeline-target-v1 | #3·#7의 명시된 목표 검사 | 시장·트랙션 개별 기여, 설정값을 등급에도 적용 | 설정 임계값 이상, 기본 65 |

목표 버전은 #3의 Series A 시장5·트랙션1 → 23점 사례와 #7의 설정에 맞는
등급/추천 경계만 명시적으로 채택한다. 제품의 공식 실행 경로(#1), 0~19점·결측 처리와
README 환산식(#2), 충돌하는 가중치 표 선택(#4), 리스크 근거의 의미(#6)는
아직 합의된 정책으로 간주하지 않는다. 알려진 충돌은 `known_discrepancies`에
항상 기록한다. agents에 CLI 목표를 적용하거나 척도 간 점수를 직접 비교하지 않는다.
현재 동작을 보존하면서 목표 검사 실패가 나타나는 것은 정상적인 baseline 진단이다.

리스크 산술은 CLI `6 - risk_signal`, agents 높은 점수의 양의 기여를 검사한다.
리스크 악화·완화 **근거 의미**는 산술로 검증할 수 없어 이유와 함께 N/A를 기록한다.
수치가 의미적으로 올바르다는 판정으로 해석하면 안 된다.

## 검사와 입력

- 1~5 정수 항목 범위, 총점 범위, Stage/가중치별 값과 합, 개별 기여와 총점,
  리스크 역산, 등급을 독립 재계산한다. 반올림은 현재 Python half-even 규칙이며
  기여 비교 허용 오차는 절대 `1e-9`이다.
- 고정 입력 자료보다 **decision 노드의 실제 입력**을 사용한다. 리서치/enrichment
  이후 signal이 바뀐 경우에도 당시 관측값을 보존한다. 입력이 없으면 error이며
  최종 점수에서 원래 signal을 추정하지 않는다.
- 입력 순서를 동점 순서로 사용하는 안정 정렬, 임계값 포함, 최대 Top3,
  통과/보류/Watchlist 목록과 추천/보류 분기를 확인한다. agents의 설정 top_k도 반영한다.
- 기존 Markdown `Company/Final Score`, scorecard, `Rank/Company/DD Score` 표의
  회사·Stage·점수·등급·항목·순서를 확인하고 명시된 임계값 표기도 대조한다.
  CLI는 수치 표가 없으면 error, agents의 자유 형식 보고서는 지원 표가 없으면 N/A다.
  자연어 보고서를 임의 추론해서 수치 검사를 통과시키지 않는다.

## 저장과 K

`checks.json`은 기존 workflow 검사와 `rule:<policy_version>:<check_id>` 항목을 함께 저장한다.
각 규칙에는 run/evaluation/company/invocation ID, 정책 버전, 기대/실제 값, 이유와
`pass/fail/not_applicable/error` 상태가 있다. 규칙은 호출 기록에 연결되며 자연어
평가 결과와 독립적이다. 생성 실패·입력 누락도 성공 또는 0점으로 바꾸지 않는다.

`rule_policy.json`에는 명세 snapshot, `rule_conformance.json`에는 **정책 버전별**
counts와 K, 알려진 오류, 정책 SHA-256, 원본 결정·설정·보고서·검사 기록 해시를 저장한다.
원본/정책 snapshot 변조는 `evaluation validate` 및 재검사에서 거부한다.

`K = passed / applicable`, N/A는 분모에서 제외한다. error/missing/pending이 하나라도
있거나 적용 항목이 없으면 K는 null이며 완료율을 별도로 제공한다. 서로 다른 정책과
척도의 K를 평균하지 않는다. K는 정책 정합성 지표이며 투자 성공 예측력이 아니다.
`metrics.json.rule_counts`에는 baseline 규칙만 기록하고 목표 규칙은 별도 요약으로 유지한다.
기존 `workflow_status`는 워크플로우 실행 검사이며 규칙 정합성 판정과 별개이다.

RAGAS 후처리 결과 디렉터리에도 `rule_conformance.json`을 저장하여 원본 실행 요약과
checks SHA-256, evaluation ID를 연결한다. LLM 1.0점과 규칙 오류가 동시에 남을 수 있다.
RAGAS 점수는 K를 변경하지 않는다. #29 비교 구현에서는 같은 정책 버전별 K/counts와
기존 `ragas_results.jsonl`을 나란히 읽을 수 있다. 아직 #29 비교 실행기를 구현한 것은 아니다.

## 실행

```bash
.venv-evaluation/bin/python -m unittest discover -s tests -p 'test_evaluation_rules.py' -v
.venv-evaluation/bin/python -m evaluation validate evaluation_runs/<run_id>
.venv-evaluation/bin/python -m evaluation rules evaluation_runs/<run_id>
```

`rules`는 동결된 run을 읽기 전용 재검사하고 JSON을 stdout에 출력한다.
종료 코드 0은 전체 pass/N/A, 1은 규칙 fail, 2는 오류/입력·무결성 문제이다.
옛 실행에 실제 decision 입력이나 관측 가중치가 없으면 해당 항목은 error로 남긴다.
현재 정책 명세를 사용한 재검사와 당시 저장된 결과를 구분한다.

검증 사례: 모든 Stage의 시장5·트랙션1, 49/50/64/65/79/80 경계, 최소/중간/최대,
동점/전체 보류/Top3 순서 오류, 설정 변경, 역방향 리스크, 잘못된 가중합·표기,
입력 누락·범위 오류, 원본 변조, 실제 offline 그래프 기록 및 재검사.
외부 검색·유료 LLM 호출 없이 수행한다.
