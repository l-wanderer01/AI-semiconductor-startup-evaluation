# #21 고정 평가 데이터셋

현재 Agent를 기준으로 baseline을 측정하기 위한 데이터 계약과 변환 기능이다.
Agent의 프롬프트·검색·점수·추천 판단은 변경하지 않았다. 실제 점수 산출과
고정 자료를 넣어 Agent를 실행하는 runner는 후속 이슈의 범위다.

## 현재 상태

| 작업 | 상태 |
| --- | --- |
| 출처, 원문 구간, 사실, 필수 항목, 검토, 적용 범위 스키마 | 구현 |
| manifest, 상대 경로, SHA-256, 새 버전 발행 및 덮어쓰기 방지 | 구현 |
| verified 사실만 reference로 변환, 기업·영역·기준일 검증 | 구현 |
| 실제 run 응답 구간 및 검색/전달 문맥 연결 | 구현 |
| 한국어 수치·시점·PoC/계약·단위/조건·미확인·추천/보류 사례 | 가상 검증 데이터 8건 |
| 대표 기업의 원문 발췌 수집 | Groq·Tenstorrent 2건, 기준 사실 4개 |
| 실제 기업의 사실 및 reference 완전성에 대한 사람 검토 | **미완료: pending** |
| 전체 보고서 baseline 측정 | 아직 수행하지 않음 |

따라서 #21의 사람 검토 acceptance criterion은 아직 완료되지 않았다.
가상 데이터에서 얻은 수치를 실제 Agent의 투자 분석 정확도로 발표하지 않는다.

## 구성

```text
data/evaluation/
  semiconductor-baseline/0.1.0-draft/
    dataset.json        # manifest, facts, required_items, case_reviews, inventory
    sources/*.txt       # 1차 출처의 짧은 원문 발췌 (전체 웹페이지가 아님)
    evidence/*.json     # 생성용 고정 자료의 source_id 목록
    references/*.json   # verified 사실의 텍스트와 fact/source/검토 메타데이터
  semiconductor-fixtures/1.0.0/
    ...                # 실제 기업과 분리한 가상 검증 사례
```

`evaluation/models.py`의 기존 공통 모델을 `dataset_models.py`에서 확장한다.
`dataset.py`가 파일 간 무결성/범위 검증, 발행, reference 변환, 실제 run 연결을 담당한다.
`dataset_cli.py`는 오프라인 관리 명령이고 `seed_dataset.py`는 최초 자료의 재현 가능한 생성기다.

`manifest.sha256`은 `dataset.json`을 canonical JSON으로 직렬화한 값에서
그 해시 필드 자체만 제외해 계산한다. 사실·검토·정책·모든 inventory 해시는 포함한다.
실제 파일의 UTF-8 바이트 해시는 inventory와 SnapshotRef 양쪽에서 확인한다.
원문 offset은 UTF-8 바이트가 아닌 Python 문자열의 **문자 인덱스**, 끝은 제외한다.
경로는 데이터셋 디렉토리 기준 상대 경로이며 경로 탈출 및 심볼릭 링크를 거부한다.

## 실제 기업 자료의 범위

두 기업은 기존 `data/live_ai_semiconductor_companies.json`에서 선정했다.
자료는 2026-10-05에 확인했고, 각 질문은 **2024년의 특정 투자 라운드**에 한정했다.
case/fact의 `as_of_date`는 2026-10-05, `source_as_of_date`와 `published_date`는
각 2024년 발표 날짜다. 과거 사건의 날짜와 평가 기준일을 혼동하지 않는다.

- Groq: [투자 참여를 대리한 Fenwick의 2024-08-05 발표](https://www.fenwick.com/insights/experience/fenwick-represents-cisco-investments-in-groqs-640m-series-d-funding).
  제목을 원문 발췌로 저장했다. 해당 라운드의 Series D와 USD 640 million만 검토 후보로 삼는다.
- Tenstorrent: [기업이 배포한 2024-12-02 발표](https://www.prnewswire.com/news-releases/tenstorrent-closes-693m-of-series-d-funding-led-by-samsung-securities-and-afw-partners-302319584.html).
  첫 문장의 짧은 구간을 저장했다. Series D와 USD 693 million **초과**만 후보로 삼는다.

발표의 거래 금액을 매출로, 라운드 금액을 누적 투자액으로, 과거 라운드를 현재 단계로 바꾸지 않는다.
발췌 밖의 매출·고객 수·벤치마크에 대해서는 이 reference가 정답을 제공하지 않는다.
이 두 case를 전체 투자 보고서의 FactualCorrectness 기준으로 사용하면 안 된다.
후속 데이터 확대 시 영역별 reference 완전성을 다시 검토해야 한다.

현재 실제 기업 facts와 case 검토가 pending이므로 reference `text`는 빈 문자열이다.
`excluded_reference_ids`에 후보 사실을 남긴다. 자동 수집을 사람 검증으로 표시하지 않았다.

## 먼저 확인할 명령

프로젝트 루트에서 실행한다. 외부 LLM/검색 API를 호출하지 않는다.

```bash
.venv-evaluation/bin/python -B -m evaluation.dataset_cli validate data/evaluation/semiconductor-baseline/0.1.0-draft
.venv-evaluation/bin/python -B -m evaluation.dataset_cli validate data/evaluation/semiconductor-fixtures/1.0.0 --ready --allow-synthetic
.venv-evaluation/bin/python -B -m evaluation.dataset_cli reference data/evaluation/semiconductor-fixtures/1.0.0 fixture_poc --allow-synthetic
```

실제 데이터셋에 `--ready`를 붙이면 검토 미완료로 실패하는 것이 정상이다.
가상 데이터는 명시적인 `--allow-synthetic` 없이는 baseline 입력으로 사용할 수 없다.

## 사람이 해야 할 검토와 새 버전 발행

검토 대상은 [review_checklist.md](review_checklist.md)에 정리했다.

1. 원본 draft를 보존하고 별도 작업 디렉토리로 복사한다.
2. 웹 원문·게시 날짜·저장 발췌를 대조한다. 각 fact의 statement/value/unit/conditions를 검토한다.
3. 동의한 사실만 `review_status=verified`, `review_kind=human`으로 변경하고,
   실제 검토자 ID 및 UTC `reviewed_at`을 기록한다. 확인하지 않은 사실은 pending으로 남긴다.
   잘못된 사실은 수정하거나 rejected로 기록한 작업본을 별도 draft 버전으로 보존한다.
4. `case_reviews`에서 질문 범위와 reference 완전성/출처 관련성을 확인한다.
   완료한 case만 `completeness=complete`, `review_kind=human`, 검토자/시각을 채운다.
   verified 사실이 있어도 범위 검토가 끝나지 않으면 사용할 수 없다.
5. 새 버전으로 발행한다. CLI가 verified 사실만으로 reference를 재생성하고 검토자 목록/해시를 계산한다.
   pending/rejected 사실을 계속 포함한 case는 released로 발행할 수 없다.
   해당 case를 제거해 범위를 줄이거나 추가 검토를 완료한다.

예시 명령의 `/tmp/issue21-review`는 별도로 준비한 검토 작업본 경로다.

```bash
.venv-evaluation/bin/python -B -m evaluation.dataset_cli publish /tmp/issue21-review --parent data/evaluation --version 1.0.0
.venv-evaluation/bin/python -B -m evaluation.dataset_cli validate data/evaluation/semiconductor-baseline/1.0.0 --ready
```

자료를 갱신할 때 기존 released 버전을 수정하지 않는다. 새 출처 스냅샷과
그 SHA-256/원문 구간을 facts에 연결하고 기준일·질문·필수 항목 정책을 재검토한다.
변경된 사실과 case의 검토를 pending으로 되돌린 후 다시 검토하고 새 버전으로 발행한다.
새 버전의 해시는 비교 보고서에 남기며 버전이 다른 결과를 동일 입력 비교로 취급하지 않는다.

## 실제 run과 RAGAS 연결

`RunCaseBinding`은 case_id/company_id/section_id/as_of_date, generation_invocation_id,
response_location, 선택적 retrieval_invocation_id를 가진다.
응답은 그 호출의 `output_snapshot` 또는 `agent_outputs.jsonl`의 snapshot에 있는
원문 구간만 사용한다. 다른 보고서를 붙여 넣거나 텍스트를 교정해서 평가하지 않는다.
원본 JSON의 문자열이 이스케이프되어 있으면 저장된 문자 구간을 정확히 지정해야 한다.
응답을 자연어로 재구성하는 추출 로직은 여기서 추가하지 않았다.

생성 run의 평가 기준일과 case 기준일이 같아야 한다. 날짜가 다르면
평가 파일의 날짜만 변경하지 말고 새 데이터셋 버전과 생성 run을 준비한다.
영역은 실제 호출과 부모 호출에서 확인한다. service `evaluate_business`는 business,
`evaluate_technology`와 CLI `technical_eval`은 technology 등 알려진 이름만 대응한다.
CLI에는 별도 business 평가 단계가 없어 이 최초 business case를 임의로 CLI market에 연결하지 않는다.

실제 생성 근거와 고정 발췌의 내용 해시가 동일해야 한다. 같은 URL의 다른 내용은 거부한다.
로컬 파일과 URL이 달라도 내용이 동일하면 dataset source_id와 run source_id/evidence_id를
별도 계보로 연결해 저장한다. 실제 검색 결과 대신 데이터셋 원문을 끼워 넣지 않는다.

- Factual precision/recall: 같은 case의 verified facts 텍스트를 reference로 사용한다.
- Faithfulness: 해당 생성 호출에 실제 전달한 문맥을 기록 순서대로 사용한다.
- Context recall/precision: 연결된 실제 검색 호출의 query·rank·source_id와 후보 순서를 보존한다.
  검색 호출이 없으면 `preparation.json`/`dataset_preparation.json`의 applicability에 N/A와 사유를 남긴다.
  검색은 했지만 후보가 0개이면 실제 빈 결과를 유지하고 #20 adapter의 빈 문맥 정책으로 N/A가 된다.

검토 완료 후 오프라인 입력 준비:

```bash
.venv-evaluation/bin/python -B -m evaluation.dataset_cli prepare data/evaluation/semiconductor-baseline/1.0.0 evaluation_runs/RUN_ID --bindings /tmp/bindings.json --output /tmp/issue21-prepared
```

새 출력 디렉토리에 `samples.jsonl`, `preparation.json`, `bindings.json`을 저장한다.
생성 run은 변경하지 않는다. RAGAS 실제 평가에는 데이터셋 검증 경로를 권장한다:

```bash
.venv-evaluation/bin/python -B -m evaluation evaluate evaluation_runs/RUN_ID --dataset data/evaluation/semiconductor-baseline/1.0.0 --bindings /tmp/bindings.json --model gpt-4.1-mini
```

마지막 명령은 외부 OpenAI evaluator 호출을 사용한다. dataset 버전/해시는 자동으로 읽고,
평가 디렉토리에 검증된 데이터셋·bindings·검색 기록·N/A 사유를 보존한다.
#20의 수동 `--samples` 경로도 유지하지만 그 경로는 사람이 지정한 reference의 dataset 범위를 검증하지 않는다.

## 검증

```bash
.venv-evaluation/bin/python -B -m unittest discover -s tests -p 'test_evaluation_*.py' -q
```

새 테스트는 미검토/가상 reference 사용 차단, 기업·영역·시점 오염,
원문/해시/offset 변조, 임의 보고서 reference 복사, reference 완전성,
경로 탈출·심볼릭 링크, 버전 덮어쓰기, 실제 응답/검색/문맥 연결 및 run 불변성을 확인한다.

2026-10-05 검증 결과: 기존 45개와 신규 17개를 포함한 **62개 테스트 통과**.
두 체크인 데이터셋의 구조/해시 검증 및 생성기와 산출물의 해시 일치도 확인했다.
실제 데이터셋의 ready 검증은 사람 검토 미완료로 의도대로 실패했다.
유료 LLM/검색 호출 및 실제 Agent baseline 점수 산출은 수행하지 않았다.

현재 데이터는 **평가 계약과 변환 기능을 검증하기 위한 시작점**이다.
2개 기업의 좁은 과거 라운드 질문으로 전체 보고서 정확도·최신성·투자 판단 개선을 주장하지 않는다.
실제 보고서의 각 영역과 질문에 맞는 검토된 reference 확대가 후속 baseline 측정의 전제다.
