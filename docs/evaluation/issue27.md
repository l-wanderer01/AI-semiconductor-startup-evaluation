# #27 평가기 고정·한국어 사람 검토·신뢰도 검증

생성 Agent와 프롬프트·투자 정책은 바꾸지 않는다. RAGAS 0.4.3의 기존 5개
single_turn_ascore metric을 유지하고 adapter를 0.6.0으로 버전 관리한다.
자동 점수는 평가기 출력이다. 검토 없는 evaluator confidence를 실제 정확도로 표시하지 않는다.

## 고정 조건과 한국어 지원

`evaluation.json`은 실행 ID, 생성 run ID, 이전 평가 ID·재평가 사유, 데이터셋·reference·입력·근거
해시와 평가 모델/설정/schema/adapter 버전, metric별 mode·atomicity·coverage, 프롬프트 hash를 기록한다.
LLM adapter 클래스와 ragas/langchain-core/langchain-openai/openai/pydantic 설치 버전,
retry·timeout·cache·temperature 정책도 manifest와 configuration hash에 포함한다.
핵심 의존성은 requirements-evaluation.txt에 설치된 검증 버전으로 고정한다.
새 평가 환경은 기존 evaluation.setup_environment 절차로 준비한다.
기존 manifest는 새 필드 기본값으로 읽을 수 있지만 새 설정 hash와의 비교는 양쪽 재평가가 필요하다.

한국어 처리 방식은 실제 설치된 0.4.3 `get_prompts()`/`set_prompts()`를 사용해
instruction에 한국어 지시를 추가하고 prompt.language/metric.language를 korean으로 지정한다.
기존 영어 few-shot 예제는 유지한다. 자동 번역·한국어 성능 보증이 아니다.
원문 숫자·통화·단위·시간·비교 조건·기업명·PoC/계약 구분을 보존하도록 지시한다.
모든 instruction/example/input-output schema를 `_prompts.json`에 저장한다.
공식 [metrics API](https://docs.ragas.io/en/v0.4.0/references/metrics/)를 참고하되,
0.4.3 설치 코드와 실제 metric을 deterministic LLM으로 실행한 테스트를 호환성 근거로 사용한다.

RAGAS BaseRagasLLM은 온도 미지정 시 n=1에 0.01, n>1에 0.3을 사용하며 wrapper가
ChatOpenAI.temperature를 덮어쓴다. 기존 manifest의 0.0 표기는 실제 기본 호출과 달랐다.
OpenAI adapter 기본 모델 설정을 실제 n=1 값 0.01로 기록하고 두 온도 정책을 manifest에 남긴다.
API 모델명만으로 공급자의 내부 모델 변경까지 방지할 수 없으므로 엄격한 재현이 필요하면
`--model`에 공급자가 지원하는 고정 snapshot ID를 명시한다.

평가 metric 재시도는 adapter max_retries, RAGAS RunConfig 재시도,
metric별 재시도와 prompt parse 재시도를 별도로 기록한다. cache는 사용하지 않는다.
0점은 정상 완료 점수이며 missing/error/null과 구별한다. NaN/Inf는 error로 재시도하고
끝까지 실패하면 null로 보존한다. 네트워크/API 오류를 unverifiable로 바꾸지 않는다.
각 attempt의 invocation ID, 원응답/callback/오류·재시도 연결은 기존 trace/events에 남긴다.

`metrics.json`의 ragas_runtime/custom_runtime은 생성 비용과 독립이다.
RAGAS loop 시간과 custom 평가 시간을 분리하고, 공급자 사용량·가격표를 확인하지 못한 비용은
null과 사유로 저장한다. 기존 run의 생성 usage/cost는 그대로 보존한다. 평가 시간을 생성 시간에 합산하지 않는다.

## 검토 계획과 기준

```bash
.venv-evaluation/bin/python -m evaluation.review_cli plan \
  evaluation_runs/<run_id>/evaluations/<evaluation_id> \
  --seed ko-review-v1 --per-stratum 2 --output review_plan.json
```

핵심 주장 전수 + metric별 수치/통화/단위/날짜/기술/상용 상태 층화 표본을
seed+target ID 해시로 선택한다. 표본이 없는 층은 missing_strata로 공개한다.
현재 정규식 태그는 검토 대상을 찾는 보조 도구다. 검토자는 원문을 보고 층 누락을 확인하고
추가 대상을 검토할 수 있다. 표본 통계를 전체 기업·전체 보고서 정확도로 확장하지 않는다.

계획의 automatic_result, source_sha256, evaluation_id, target_id, target_kind를 그대로 복사해
다음 구조의 ReviewDecision JSON을 사람이 작성한다. human provenance는 실제 사람이 검토한 경우만 사용한다.

```json
{
  "review_id": "review-001",
  "evaluation_id": "evaluation_example",
  "target_id": "claim_example.custom_factual",
  "target_kind": "claim",
  "reviewer": "reviewer-name",
  "reviewed_at": "2026-10-05T00:00:00Z",
  "automatic_result": {},
  "source_sha256": "계획에 있는 실제 64자리 hash",
  "human_result": "contradicted",
  "reason": "같은 기업·같은 시점의 USD 1억을 1억 원으로 오기; 출처/원문 위치",
  "status": "confirmed",
  "provenance": "human",
  "previous_review_id": null
}
```

위 JSON의 automatic_result와 hash는 설명용 자리표시자이며 그대로 제출하면 거부한다.

- 독립 사실 판정은 verified/contradicted/unverifiable 3상태만 사용한다.
  동일 기업·속성·사건·시점·조건과 source 원문을 확인한다.
  자료가 미공개라고 명시한 경우 계약 성립을 단정할 수 없다. 같은 사건에 대한 명시적 부정은 contradicted다.
- 실제 전달 문맥/원문 근거 지지는 true/false 이진 라벨로 검토하며 사실 정확도와 따로 집계한다.
- RAGAS metric 검토는 `human_result={"score":0.5,"error":true,"error_type":"unit_mismatch"}`처럼
  연속 점수와 판정 오류를 기록한다. score는 생략/null 가능하며 임계값으로 클래스를 만들지 않는다.
  입력·reference·trace의 native 주장 분해/판정을 함께 읽고 reason에 오류 위치를 기록한다.
- 추출 target은 원문 unit을 읽고 `human_result={"facts":["기준 원자 주장"],"kinds":["fact"]}`를 작성한다.
  원자 주장 누락/과분해/오분류를 exact statement+kind 기준으로 비교하고 missing/unexpected 목록을 남긴다.
  의미 동등한 다른 문구는 검토자가 직접 확인한다. API 오류의 빈 추출은 완료 누락 판정으로 바꾸지 않는다.

```bash
.venv-evaluation/bin/python -m evaluation.review_cli record \
  evaluation_runs/<run_id>/evaluations/<evaluation_id> --decision review.json
.venv-evaluation/bin/python -m evaluation.review_cli report \
  evaluation_runs/<run_id>/evaluations/<evaluation_id> \
  --plan review_plan.json --output reliability.json
```

검토 ledger는 `<run>/reviews/<evaluation_id>.jsonl`이다. 동결된 자동 evaluation 파일은 수정하지 않는다.
원판정과 source hash, 확정/제안 결과, override 이유·검토자·UTC 시각·이전 review ID를 모두 보존한다.
쓰기 잠금과 원자적 발행으로 이전 행을 유지한다. 수정은 최신 review ID를 previous_review_id로 연결한다.
proposed는 gold가 아니다. 동일 검토자의 수정은 최신 값이 유효하고, 서로 다른 사람의 불일치는
확정 집계에서 제외한다. adjudicated에는 충돌한 두 사람의 review ID를 resolves_review_ids에 기록하고
독립적인 제3 검토자가 원문을 보고 확정한다. 이후 새로운 불일치가 발생하면 다시 미조정으로 표시한다.

3상태 또는 명시적 이진 라벨에만 사람 행/자동 열 혼동행렬과 라벨별 precision/recall을 제공한다.
분모가 0이면 null이다. 사람 검토가 없는 경우 sample_agreement=null이며 validated_accuracy는 항상 null이다.
표본 agreement는 실제 human 라벨이 있는 완료 쌍에만 제공한다. synthetic_fixture는 별도 수로 남기고
실제 사람 성능 집계에서 제외한다. 자동 missing/error는 쌍 집계에서 제외하며 완료 비율과 상태 수에 남긴다.
자동 완료 비율의 분모는 not_applicable을 제외한 적용 대상 수이며 분모가 0이면 null이다.
연속 RAGAS metric은 원점수·사람 점수·오류 사례만 보존하며 confusion_matrix=null이다.

## 평가기 변경 및 전후 재평가

```bash
.venv-evaluation/bin/python -m evaluation.reevaluation_cli check \
  <baseline_evaluation_directory> <after_evaluation_directory>
.venv-evaluation/bin/python -m evaluation.reevaluation_cli rerun \
  <baseline_evaluation_directory> <after_evaluation_directory> \
  --model <evaluation_model_snapshot> --max-retries 1 \
  --reason '평가기 설정 변경 회귀' --output reevaluation_pair.json
```

동일 새 adapter로 baseline/after를 모두 새로운 evaluation ID에 실행한다.
동일 데이터셋·reference와 서로 다른 생성 run을 요구하며 기존 결과·검토 ledger는 그대로 보존한다.
한쪽만 재평가한 결과 비교는 configuration hash 불일치로 거부한다.
custom 평가를 실행한 기존 평가의 재평가에는 동일 기능을 `--custom-factual`, `--custom-grounding`,
`--required-information`으로 명시하고 factual에는 `--reference-dataset`을 지정한다.
custom 검사를 조용히 제거할 수 없다. API 오류로 일부 미완료이면 complete=false와 양쪽 상태를 출력한다.
이 명령은 유료 평가 API를 호출한다. 생성 Agent는 다시 실행하지 않는다.

isolated package는 원래 평가기 설정에 고정되어 있으므로 일괄 rerun이 암묵적으로 lock을 수정하지 않는다.
같은 고정 입력·근거에서 새 설정에 pin한 두 stage package를 #22 절차로 만들고,
기존 `evaluation evaluate --previous-evaluation-id ... --resume-reason ...`로 양쪽을 재평가한 후 check한다.
end_to_end에서 입력/근거 차이는 input_or_evidence_changed로 공개한다.

## 검증과 현재 한계

```bash
.venv-evaluation/bin/python -B -m unittest discover -s tests -p 'test_evaluation_*.py' -q
```

fixture `tests/fixtures/evaluator_ko_v1.json`은 Codex가 작성한 가상 기대값이며 human_reviewed=false다.
실제 한국어 평가 모델 성능·사람 검토 결과로 해석하지 않는다. 테스트는 고정 RAGAS 실제 API와 deterministic LLM,
검토 이력·수정·불일치·조정·분모·표본·연속 점수·API 오류·재평가 불변성을 검사한다.
실제 기업 baseline/after, 유료 모델 및 실제 검토자 라벨 수집은 아직 실행하지 않았다.
실제 정확도 확인에는 검토 완료 reference·native metric trace와 독립적인 사람 검토가 필요하다.
