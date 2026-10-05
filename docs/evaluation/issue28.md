# #28 생성·평가 시간과 API 비용 계측

## 요청 경계

- `app.py`: `main()` 진입부터 agents 초기화(문서 로딩, 로컬 embedding, FAISS 구축), 검색, 기업 평가, 보고서 생성, 요청한 Markdown과 상태 JSON의 저장·검증까지 포함한다. 서비스 생성자가 별도 사용되면 생성자 진입이 첫 요청의 시작이며 초기화 실패도 저장한다. 이미 초기화된 서비스의 다음 `run()`은 초기화 비용을 재청구하지 않는 warm 실행이다.
- `investment_pipeline.cli`: CLI 함수 진입부터 입력 읽기, 검색/RAG 초기화와 embedding, 기업 평가, 언어 보강, Markdown·PDF·상태 JSON 저장·검증까지 포함한다. PDF 실패나 요청 형식 누락은 실패다.
- 완료 시각은 모든 요청 파일이 저장된 직후 고정한다. 실패는 `completed_at=null`이고 실패 시점까지의 경과 시간만 남긴다. UTC `started_at`/`ended_at`과 `time.monotonic()` 기반 `duration_seconds`를 함께 기록한다.
- `run.json.duration_seconds`는 기존 계약의 실행 기록·후처리까지 포함하는 전체 시간이다. 사용자 결과물 생성 비교에는 `metrics.json.generation_runtime.duration_seconds`를 사용한다. `finish()`의 workflow/rule 검사는 `custom_runtime`에 별도 기록한다.
- 동결한 run 아래 `evaluations/<evaluation_id>/`의 RAGAS·custom 평가를 저장한다. 생성 시간·비용·파일은 후처리 평가로 바뀌지 않는다. 현재 사용자 전달 경로에는 평가 gate가 없어 `delivery_gate_delay_seconds=null`이다. 추후 gate를 도입하면 그 지연을 따로 계측해야 한다.

## 저장 구조와 비교

생성 run의 `metrics.json`은 `generation_runtime`, `custom_runtime`을 포함한다. 독립 평가의 `metrics.json`은 `ragas_runtime`, `custom_runtime`을 포함한다. 각 목적에 대해 다음 파일을 함께 보존한다.

| 파일 | 내용 |
| --- | --- |
| `generation_runtime.json` / `ragas_runtime.json` / `custom_runtime.json` | 상태, UTC 경계, monotonic 시간, API 비용, 통화, 호출별 usage, 캐시 상태 |
| `*_runtime_usage.json` | 모델·metric·호출 ID·attempt·관측 retry·cache hit/miss·input/output/cached token·호출 시간·계산식 |
| `*_runtime_prices.json` | 실제 사용한 가격표의 기준일·버전·통화·출처·모델별 단가 |

`generation_runtime.status`와 `cache_state`(`cold`, `warm`, `mixed`, `unknown`), `run.json.llm_enabled`/`fallback_used`/`fallback_reasons`를 동일 입력·코드·설정의 run 사이에서 비교한다. 웹 검색의 파일 캐시 hit는 실제 요청 비용 0, miss는 실제 호출로 기록한다. 검색 비활성화는 호출을 만들지 않고 fallback을 남긴다. 검색 호출 실패는 사용료 청구 여부를 추정하지 않는다. 로컬 embedding은 token 수를 추정하지 않으며 API 비용만 0이다. 로컬 CPU/GPU·전기료는 이 비용 지표 범위 밖이다. 캐시를 사용하지 않거나 관측할 수 없는 LLM/embedding 호출은 `cache_hit=null`이다.

## 가격표와 비용 계산

`EVALUATION_PRICE_TABLE`에 검증한 JSON 가격표 경로를 지정한다. 기존 생성·평가 환경의 의존성 추가는 없다. 모델 key는 **`provider/model`의 정확한 이름**이며 날짜가 포함된 응답 모델명을 사용하면 그 key도 가격표에 포함한다.

```json
{
  "version": "organization-approved-2026-10-05",
  "as_of_date": "2026-10-05",
  "currency": "USD",
  "source": "가격표 확인 출처 또는 내부 승인 문서",
  "models": {},
  "search_per_call": {}
}
```

`models`의 각 값은 `input_per_million`, `output_per_million`, `cached_input_per_million`이라는 비음수 유한 단가를 갖는다. `search_per_call`은 제공자별 성공한 uncached 요청의 단가다. 실제 사용하는 검색 depth·요금제·계약에 맞는 단가를 넣는다. 기본 정책 `unpriced-api-fees-v1`은 외부 가격을 추정하지 않고 로컬 실행과 캐시 hit의 API 비용만 계산한다.

LLM 비용은 `((input-cached)×input단가 + cached×cached단가 + output×output단가)/1,000,000`이다. cached tokens는 input tokens에 포함된다. input/output/cached usage 또는 정확히 일치하는 가격이 없으면 해당 비용은 `null`이며 사유를 남긴다. 집계 중 하나라도 비용이 미확인이면 총비용도 `null`이다. 알려진 호출만 더해 전체 비용으로 제시하지 않는다. 호출이 없는 완료된 범위는 API 비용 0이다.

## 채택 버전과 callback 범위

`ragas==0.4.3`, `langchain-core==1.2.18`, `langchain-openai==1.1.11` 환경에서 `single_turn_ascore(..., callbacks=[...])` → `PydanticPrompt` → `LangchainLLMWrapper.agenerate_prompt`의 callback 전달을 로컬 설치 코드에서 확인했다. 실제 RAGAS와 deterministic LangChain `BaseChatModel`을 연결한 테스트로 여러 내부 LLM 호출의 usage 전달도 확인했다.

`on_chat_model_start`/`on_llm_start`, `on_llm_end`/`on_llm_error`, `on_retry`를 수집한다. 구조화 출력의 parsed 결과에서 usage가 사라져도 LLM callback의 `LLMResult`/`AIMessage.usage_metadata` 또는 `token_usage`를 사용한다. 반환 응답과 callback의 같은 usage는 중복 집계하지 않는다. callback을 지원하지 않는 평가 adapter는 attempt별 미확인 usage를 남긴다.

현재 선택 가능한 다섯 metric(`factual_precision`, `factual_recall`, `faithfulness`, `context_precision`, `context_recall`)은 LLM 기반이며 embedding 호출을 하지 않는다. embedding을 호출하는 metric을 추가할 때는 해당 provider의 embedding usage observer를 연결해야 한다. 현재 사용하지 않는 embedding 비용을 임의로 추가하지 않는다.

RAGAS adapter의 재시도는 `attempt`와 기존 trace의 `previous_invocation_id`로 연결된다. 공개 callback의 retry 수만 기록하고 SDK 내부에서 숨긴 재시도는 `retry_visibility=provider_internal_unknown`으로 표시한다. 이 경우 관측 retry 0을 실제 retry 0의 보증으로 해석하면 안 된다. LangChain factual/grounding/coverage judge 호출은 `custom_evaluation`로만 계측한다. metric의 품질 점수나 원래 생성 결과는 변경하지 않는다.

usage observer에는 API 키나 원문 입력을 저장하지 않는다. 가격표 출처·오류·기존 trace를 포함한 모든 저장은 기존 secret redaction을 거친다.

## 검증

```sh
.venv-evaluation/bin/python -m unittest discover -s tests -q
git diff --check
```

외부 API 없이 실제 두 그래프의 결과 유지, PDF 저장 및 실패, 초기화 실패, 완료 경계 이후 시간 제외, cold/warm 검색, 구조화 callback usage, cached token 계산, usage 누락, failed 호출·RAGAS retry, 실제 RAGAS callback 전달, 생성 run 불변성, API 키 redaction을 검증한다. 테스트 가격과 모델은 synthetic fixture이며 실제 공급자 가격·비용 측정 결과로 제시하지 않는다. 유료 API와 실제 모델 다운로드를 포함한 온라인 벤치마크는 수행하지 않는다.
