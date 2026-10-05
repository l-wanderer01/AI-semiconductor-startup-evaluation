# #23 독립 사실 정확도와 핵심 오류 평가

작성 기록: Codex가 위임받은 평가 기반을 구현했다. 보고서 생성 Agent, 검색,
투자 점수·가중치·추천 정책은 변경하지 않는다.

## 평가 기준

`verified`는 독립 자료가 주장을 지지하는 경우, `contradicted`는 같은 대상과 조건의
명시적 반증이 있는 경우, `unverifiable`는 근거 부족·범위 밖·출처 충돌이다.
생성 문맥이나 RAGAS의 비지지 결과를 거짓 판정으로 사용하지 않는다.
평가 오류/미판정은 세 상태와 별개의 `error`/`missing`이며 분모에서 숨기지 않는다.

독립 자료는 #21의 검토 완료·발행된 데이터셋이다. 원문 파일 해시와 quote/offset을
다시 검증한다. #22 패키지와 같은 데이터셋 해시여야 하며, 기업·영역·기준일이 맞는
verified fact만 사용한다. 미래 자료와 pending/rejected fact는 제외 이유 및 ID를 남긴다.
해당 case의 좁은 RAGAS reference에 없는 추가 사실도 **같은 고정 데이터셋 안의 독립
검토 완료 fact**로 지지할 수 있다. 새로운 외부 자료가 필요하면 검토 후 새 데이터셋
버전과 #22 패키지를 만들어야 한다. 실시간 검색이나 생성 보고서를 정답으로 추가하지 않는다.

## 구현 파일

| 파일 | 역할 |
| --- | --- |
| evaluation/factual_models.py | 비교 제안 및 근거·버전·원문 구간을 갖는 판정 모델 |
| evaluation/factual.py | LangChain structured-output judge, 조건/수치 정책, 집계, 두 RAGAS 모드 연결 |
| evaluation/ragas_adapter.py | 같은 evaluation ID로 RAGAS/custom 판정 저장, 완료 상태 계산 |
| evaluation/__main__.py | 기존 evaluate 명령의 선택적 --custom-factual 경로 |
| tests/test_evaluation_factual.py | 한국어 가상 정책 사례와 실제 설치된 RAGAS API 연동 |

고정 정책 `independent-three-state-v1`, judge `ko-reference-judge-v1`, adapter `0.3.0`이다.
후속 #24까지 통합한 실행은 adapter `0.4.0`과 실제 입력 계보 정책을 사용한다.
문맥·인용 검사 옵션 및 결합 설정은 [#24 구현 기록](issue24.md)을 참조한다.
custom prompt 본문·SHA-256·출력 schema·모델 설정을 저장하고, 기존 RAGAS 설정 해시와
합쳐 evaluator configuration 해시를 만든다. isolated 계약도 이 결합 해시와 같아야 한다.
기존 RAGAS 전용 실행은 custom judge를 호출하지 않는다.

## 판정 정책

LangChain judge는 모든 적용 가능한 독립 fact를 한 번씩 비교한다. 누락·중복 fact ID,
원문에 없는 수치 quote, 구조화 출력 파싱 실패는 평가 error다. 각 비교에 기업·속성·사건·시점·
조건의 일치 여부와 한국어 이유를 기록한다. 자연어 의미 비교는 모델 판정이므로
`human_review_status=pending`을 유지한다. 자동 판정을 사람이 확정한 결과로 표시하지 않는다.

| 상황 | 처리 |
| --- | --- |
| reference에 없음 | 독립 fact 전체를 검토하고, 지지/반증 없으면 unverifiable |
| 같은 라운드 USD 금액의 10배 오류 | 수치 정책으로 contradicted |
| 같은 사건 USD를 KRW로 기재 | contradicted; 환율을 임의 가정하지 않음 |
| 배율을 TOPS로 기재 | 단위 직접 변환 불가 → unverifiable |
| 6억 4천만 달러와 USD 640,000,000 | Decimal로 명시 단위를 변환해 동등성 검사 |
| "초과" 기준에서 하한과 정확히 같은 금액 주장 | contradicted; quote가 초과를 생략해도 기준 조건을 적용 |
| 하한만 있는데 정확한 더 큰 금액 주장 | unverifiable; 하한에서 확정값을 만들지 않음 |
| 다른 관측 연도/benchmark 모델·정밀도·전력·비교 장치 | 비교 범위 밖 → unverifiable |
| 같은 사건의 날짜 오류 | 다른 관측 기간과 구별해 명시 반증으로 contradicted |
| PoC와 유료 계약/양산 혼동 | 상태 반증을 유지; 고객 수가 같아도 모순을 지우지 않음 |
| 매출/고객 수 미공개 | 0이 아님; 임의 수치에는 unverifiable |
| 기업/속성/라운드가 다른 자료 | 해당 비교는 지지/반증에 사용하지 않음 |
| 지지와 반증 출처 충돌 또는 같은 조건의 기준 값 충돌 | unverifiable + conflict_fact_ids; 최신/다수 출처를 임의 우선하지 않음 |

숫자 검사는 quote의 명시 단위·통화·한글 배수·상하한을 사용한다. 다중 독립 수치,
모호한 단위, 근삿값은 확정 비교하지 않는다. 숫자 fact의 quote 값은 검토 완료 fact.value와
대조한다. 자연어 속성/사건/조건 매칭과 날짜 오류/다른 기간의 구별은 judge와 표본 검토 대상이다.
이 정책은 모든 자연어 오판을 결정론적으로 탐지한다는 보장이 아니다.

## 저장 결과와 해석

새 evaluation 디렉토리에 기존 RAGAS 파일과 아래 파일을 함께 저장한다.
설정·메타데이터·집계 snapshot 및 factual_sample_results 행은 기존 SnapshotPayload 계약에 따라
`data` 안에 본문을 저장한다. fact_assessments 행은 판정 모델을 직접 직렬화한다.

- `fact_assessments.jsonl`: claim/sample/case ID, 세 상태 또는 error/missing,
  reference IDs, 근거 원문 구간, 비교 제안·최종 이유·raw output·평가 버전·기준일·충돌/제외 ID.
- `custom_factual_configuration.json`: 고정 정책·prompt·schema·모델 및 설정 해시.
- `independent_reference_dataset.json`: 판단에 사용한 검토 완료 독립 자료 메타데이터.
- `independent_sources.json`: 원본 source ID/상대 snapshot과 평가 디렉토리 보관 snapshot의 대응.
  원문 파일은 평가 디렉토리에 별도 복사한다. 근거 구간은 원본 source를 가리키며 이 대응표로 찾는다.
- `custom_factual_metrics.json`: sample/case/기업×영역/stage별 N, judged/pending,
  verified/contradicted/unverifiable count 및 rate, 핵심 모순/검증 불가 count와 claim IDs.
  final/intermediate를 분리해 같은 내용의 단계별 반복으로 최종 보고서 분모를 부풀리지 않는다.
- `factual_sample_results.jsonl`: claim/case/sample과 custom 집계 및 두 RAGAS mode의 원점수,
  진행 상태·response/reference 해시·설명을 연결한다. metric 설정은 evaluation.json의 evaluator에 있다.

`custom_verified_over_n`과 `ragas.factual_precision.result.value`는 다른 지표다.
RAGAS는 metric별 내부 분해와 case reference 범위의 일치도를 사용한다.
custom은 #22에 저장한 고유 주장과 독립 자료를 사용한다. 따라서 reference에 없는 올바른
추가 사실, 중복 처리, 분해 granularity에 따라 값이 달라질 수 있다.
RAGAS recall도 별도 키에 유지한다. F1은 이번 구현에서 추가하지 않는다.
개념 근거: [RAGAS Factual Correctness](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/factual_correctness/).
호환성은 최신 문서가 아닌 설치된 `ragas==0.4.3`의 기존 adapter와 실제 metric API로 검증한다.

미판정·judge error·추출 error/missing이 있는 집계는 partial이며 세 상태 비율은 null이다.
N은 저장된 고유 사실 주장 수다. 추출/입력 실패로 실제 전체 주장 수를 알 수 없으면
denominator_complete=false와 incomplete_input_sample_ids를 기록하며 숨은 주장 수를 추정하지 않는다.
핵심 오류 목록은 완료된 판정에서 발견한 ID만 포함하므로 partial에서는 최종 전체 오류 수로
해석하면 안 된다. 정상 N=0은 not_applicable이고 비율은 null이며 사실이 없는 응답을 100% 정확도로 표시하지 않는다.
RAGAS 또는 custom이 미완료이면 evaluation.json도 completed로 만들지 않는다.

## 실행

사람 검토가 끝난 실제 데이터셋과 #22 패키지를 준비한 후 아래 placeholder를 바꾼다.
이 명령은 기존 생성 run을 수정하지 않고 별도 RAGAS/custom OpenAI 평가 호출을 수행한다.

```bash
.venv-evaluation/bin/python -B -m evaluation evaluate evaluation_runs/RUN_ID \
  --stage-package /path/to/stage-package \
  --custom-factual --reference-dataset /path/to/reviewed-dataset \
  --model gpt-4.1-mini --judge-model gpt-4.1-mini
```

두 judge는 temperature=0, timeout=120, provider retries=0을 사용한다.
custom이 필요 없으면 기존 evaluate 명령을 그대로 사용한다. CLI는 가상 fixture를 실제 baseline으로
사용하도록 허용하지 않는다. 테스트에서만 명시적으로 allow_synthetic=True를 사용한다.

## 검증과 한국어 표본 검토

한국어 가상 사례의 기대 판정과 저장 결과를 코드로 대조한다. 아래는 정책 표본이며
외부 실제 기업 또는 실제 LLM 정확도 측정 결과가 아니다. 작성/정책 검토는 Codex이며
사람 검토 완료 라벨이나 투자 정답으로 제시하지 않는다.

| 한국어 표본 | 기대 판정 | 검토 이유 |
| --- | --- | --- |
| "USD 640,000,000" vs "6억 4천만 미국 달러" | verified | 명시 통화·금액 동등 |
| "USD 64,000,000" vs 기준 "USD 640,000,000" | contradicted | 같은 사건의 10배 오류 |
| reference가 단계만 포함하나 독립 자료가 금액을 지지 | verified | case reference 누락은 거짓이 아님 |
| "연매출 100억 원"이나 매출 자료 없음 | unverifiable | 근거 부족과 오류를 분리 |
| "유료 계약 고객 2곳" vs PoC 2곳·유료 계약 미체결 | contradicted | 숫자 동등성으로 상태 모순을 지우지 않음 |
| "미공개 유료 고객 수=0" | unverifiable | 미공개는 0이 아님 |
| 다른 연도/벤치마크 조건으로 확장 | unverifiable | 자료 범위를 넘어서는 주장 |
| 같은 사건의 잘못된 날짜 | contradicted | 사건 식별 후 날짜 반증 |
| 같은 범위의 출처끼리 값이 충돌 | unverifiable | 기준 충돌 확정 유보 |

실제 baseline 실행 전에는 사람이 이 표의 정책과 실제 모델 판정 표본을 함께 검토해야 한다.
특히 same_event/same_time/same_conditions와 source 충돌 이유를 확인한다. 확정 기록은 #27의
HumanReviewRecord로 원판정을 덮어쓰지 않고 별도 연결한다. 현재 실제 데이터셋의 사람 검토는
pending이고, 유료 API를 사용하는 실제 baseline·실모델 품질 표본 검토는 이번 변경에서 실행하지 않았다.

```bash
.venv-evaluation/bin/python -B -m unittest discover -s tests -p 'test_evaluation_*.py' -q
```

테스트는 hand-authored comparison proposal과 deterministic fixture LLM을 사용한다.
설치된 RAGAS 0.4.3의 precision/recall API 호출, 동일 입력 해시, 원점수 보존, 독립 source 복사,
같은 evaluation ID, 별도 custom 키, 오류 분모 및 final/intermediate 분리를 검증한다.
