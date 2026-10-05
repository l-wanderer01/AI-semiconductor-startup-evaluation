# README와 구현 차이 분석

분석일: 2026-10-04. 기준: 현재 작업 트리의 README 본문, Python 코드, 노트북 실행 셀, 프롬프트 및 저장된 JSON 결과.

## 핵심 결론

기존 표의 `LangGraph 미구현`, `문자열 검색만 구현`, `임계값 미확인`은 현재 코드 전체에 대한 설명으로는 맞지 않는다. 실제로 LangGraph, bge-m3 dense 검색, Qdrant hybrid 검색, 점수 기준 분기가 구현되어 있다. 더 큰 문제는 **두 실행 경로가 서로 다른 제품 동작을 제공하는데 README가 이를 구분하지 않는 것**이다.

| 구분 | app.py / 노트북 → agents | 별도 CLI → investment_pipeline |
| --- | --- | --- |
| 진입점 | `python app.py`, `notebooks/investment_report_service.ipynb` | `python -m investment_pipeline.cli` |
| 오케스트레이션 | LangGraph, Supervisor에서 6개 평가 함수를 순서대로 호출 | 외부 LangGraph + 기업별 Supervisor 순환 그래프 |
| 후보 | 로컬 Markdown에서 LLM으로 회사명을 최대 10개 추출 | 입력 JSON의 companies 전체를 읽음 |
| 데이터 수집 | 로컬 Markdown 검색 | 입력 JSON + 조건부 Tavily 웹 검색 |
| 검색 | bge-m3 dense + FAISS | bge-m3 dense + TF-IDF sparse + Qdrant RRF |
| LLM | gpt-4.1-mini, 분석·점수·보고서 생성에 사용 | gpt-4.1-mini, 기본 비활성화, 선택적 서술 보강 |
| 점수 | 6개 항목 1~5점 단순 합, 6~30점 | 5개 평가 결과의 가중합, 이론상 20~100점 |
| 투자 추천 | 기본 20점 이상, 최대 3개 | 기본 65점 이상, 최대 3개 |
| 결과 | 시각을 붙인 Markdown 및 평가·정책 JSON | 지정 경로 Markdown/PDF/상태 JSON |

근거: [app.py](../app.py), [노트북](../notebooks/investment_report_service.ipynb), [agents/service.py](../agents/service.py), [agents/models.py](../agents/models.py), [CLI](../investment_pipeline/cli.py), [graph.py](../investment_pipeline/graph.py), [retrieval.py](../investment_pipeline/retrieval.py).

## 1. 기존 네 가지 차이 재검증

| 이전 정리 | 현재 판정 | 확인 근거와 정확한 차이 |
| --- | --- | --- |
| LangGraph 기반 Agent ↔ Python 함수형 파이프라인 | 기존 판단 수정 필요 | 두 경로 모두 `StateGraph`를 생성·컴파일·invoke한다. 함수가 노드 구현이라는 사실은 LangGraph 미사용을 뜻하지 않는다. 실제 차이는 전문 평가가 병렬 실행되지 않는다는 점이다. |
| bge-m3 Hybrid Retrieval ↔ 토큰·문자열 검색 | 기존 판단 수정 필요 | CLI는 bge-m3 dense와 TF-IDF sparse를 Qdrant RRF로 합친다. app/노트북은 bge-m3 + FAISS dense 검색이다. 문자열 키워드 로직은 검색기 대신 주로 CLI의 점수 산정에 사용된다. |
| GPT 기반 분석 ↔ 결정론적 대체 로직 | CLI에 한해 확인 | CLI의 `enable_llm_enrichment=False`가 기본이며 점수는 입력 signal 또는 웹 증거의 키워드 규칙에서 나온다. LLM을 켜도 `EvaluationLLMOutput`에는 score 필드가 없고 `make_evaluation`은 signal을 그대로 점수로 반환한다. 반면 app/노트북은 실제 LLM 평가가 기본이고 동등한 전체 분석 fallback은 없다. |
| 임계값 라우팅 ↔ 임계값 불명확 | 미구현이 아니라 기준 불일치 | CLI는 `>=65`, app/노트북은 `>=20`을 명시적으로 적용한다. README 2절은 `>65 / <=65`, 5-3절은 65점을 Selective DD로 분류하므로 README 내부도 불일치한다. |

주요 위치:

- LangGraph: `investment_pipeline/graph.py:23, 233, 265, 344, 357`, `agents/service.py:467`.
- 검색: `investment_pipeline/retrieval.py:73, 90, 197`, `agents/service.py:48`.
- LLM 조건과 점수: `investment_pipeline/config.py:28`, `investment_pipeline/llm.py:24`, `investment_pipeline/services.py:522`, `investment_pipeline/models.py:143`.
- 임계값: `investment_pipeline/graph.py:296`, `agents/models.py:18`, `agents/service.py:359`.

## 2. 추가 불일치

### A. 투자 판단에 직접 영향을 주는 차이

**A1. 주 실행 경로에 DD-Worthiness 100점 체계와 Stage 가중치가 없다.**

README는 0~100점, Seed~Series C+ 가중치를 시스템 기능으로 설명한다. 하지만 `app.py`가 호출하는 `agents`에서는 `CompanyEvaluation.total_score`가 여섯 항목을 단순 합산한다. Stage 필드와 가중치 계산이 없고 80/65/50점의 네 가지 판정도 구현하지 않는다. 노트북도 `recommendation_threshold=20`을 명시한다. 따라서 README의 점수표로 이 경로의 결과를 해석할 수 없다.

근거: `app.py:3, 11`, `agents/models.py:8, 34, 47`, `agents/service.py:309, 359`, 노트북 설정 셀.

**A2. README 점수 공식과 CLI 공식이 다르다.**

README: `Σ(score × weight × 10/3)`. CLI: `score / 5 × weight × 100`, 즉 `score × weight × 20`. weight를 합계 1인 비율로 해석하면 README 공식은 모든 항목이 5점이어도 약 16.67점이다. README는 weight 단위도 명확히 해야 한다. CLI는 최소 항목 점수가 1이므로 가중치 합이 1인 현재 설정에서 최저 총점은 20점이다. 0~19점을 생성할 수 있는 점수·결측 처리 규칙은 없다.

근거: `README.md:118`, `investment_pipeline/scoring.py:43`, `investment_pipeline/models.py:25, 84`.

**A3. 시장성과 트랙션을 합치는 방식이 여섯 항목의 개별 가중합과 다르다.**

CLI는 `round((market_signal + traction_signal) / 2)`에 `market_weight + traction_weight`를 적용한다. 서로 다른 가중치가 평균 점수에 묻히며 평균의 정수 반올림도 개입한다. Series A에서 시장성 5점·트랙션 1점이면 README 5-2의 개별 가중합 기여도는 23점, 실제 병합 기여도는 21점이다. 최종 순위와 추천 여부에 영향을 줄 수 있다.

근거: `investment_pipeline/graph.py:96`, `investment_pipeline/scoring.py:19`.

**A4. README의 Series A 기본 가중치 표가 동적 가중치 표 및 코드와 다르다.**

| 항목 | README 5-1 기본/Series A | README 5-2 및 CLI Series A |
| --- | --- | --- |
| 팀 | 20% | 25% |
| 시장 | 20% | 20% |
| 기술 | 20% | 25% |
| 트랙션 | 15% | 15% |
| 경쟁 우위 | 15% | 10% |
| 리스크 | 10% | 5% |

CLI의 Stage별 가중치 상수 자체는 README 5-2와 일치한다. 다만 문서의 두 표 중 어느 것이 공식 기준인지 정해야 하고, A3의 병합 문제는 별도로 남는다.

근거: `README.md:107, 125`, `investment_pipeline/scoring.py:8`.

**A5. CLI의 점수는 증거 내용의 정밀 평가보다 공통 키워드의 존재·문서 수에 좌우된다.**

`_score_from_evidence`는 customer/partner/founder/patent/benchmark/funding 등의 단어가 들어간 문서 수를 모든 평가 영역에 공통 적용한다. 해당 단어가 있는 문서가 2개면 4점, 4개면 5점이다. 기업 검색 기본값은 항목당 3건이므로 현재 검색 결과 수 설정에서는 4건을 요구하는 5점 분기에 도달하기 어렵다. TRL 단계, TAM $1B, 매출·고객 검증 수준을 구조화해 점수에 반영하는 별도 규칙은 확인되지 않는다.

리스크에도 같은 함수를 적용한 뒤 `6 - risk_signal`로 뒤집는다. 따라서 긍정·부정 문맥과 무관하게 funding 같은 단어가 있는 문서가 많으면 리스크 평가 점수가 낮아질 수 있다. 이는 내용상 위험도를 측정했다는 해석을 뒷받침하지 못한다.

근거: `investment_pipeline/services.py:270, 309, 621, 641`, `investment_pipeline/config.py:34`, `investment_pipeline/graph.py:140`.

**A6. 임계값 설정과 등급 분류의 기준이 독립되어 있다.**

CLI의 필터는 `settings.selective_dd_threshold`를 쓰지만 `to_recommendation`은 80/65/50을 하드코딩한다. 임계값 설정을 변경하면 보고서 분기와 등급이 어긋날 수 있다. 기본값에서는 README 5-3의 등급 경계와 일치한다.

근거: `investment_pipeline/config.py:26`, `investment_pipeline/graph.py:300`, `investment_pipeline/scoring.py:47`.

### B. 에이전트·조사 기능의 차이

**B1. 병렬 조율은 두 경로 모두 구현되어 있지 않다.**

README 3절은 5개 평가 에이전트의 병렬 조율을 설명한다. `agents` Supervisor는 기술→시장→사업→팀→리스크→경쟁 평가를 순서대로 호출하고, 각 함수 안에서 기업을 순회한다. CLI도 기업을 for-loop로 처리하고 기업별 Supervisor가 한 번에 하나의 평가 노드를 선택한다.

근거: `README.md:55`, `agents/service.py:255, 299`, `investment_pipeline/graph.py:26, 282`.

**B2. 추가 조사·재평가는 실질적인 증거 보완으로 연결되지 않는다.**

CLI의 기술/시장 평가가 4점 미만이면 추가 조사 노드로 이동하는 순환 구조는 존재한다. 그러나 노드는 고정된 조사 안내 문장만 반환한다. 새 Tavily 호출, 문서 확보 또는 signal 갱신이 없다. 재평가는 같은 signal을 사용하므로 점수가 바뀌지 않는다. app/노트북 경로에는 이 추가 조사 루프 자체가 없다.

근거: `README.md:162, 164`, `investment_pipeline/graph.py:32, 69, 91, 96, 116`, `agents/service.py:467`.

**B3. 후보 10개 자동 수집은 실행 경로에 따라 제한적이거나 없다.**

CLI의 `list_candidates` 노드는 실행 메타데이터만 만들고 후보 발굴을 하지 않는다. JSON의 회사 수를 10개로 제한하거나 검증하지도 않는다. app/노트북은 로컬 Markdown에서 회사명을 최대 10개 추출하므로 웹에서 새로운 후보를 발굴하는 기능은 아니다. 부족한 후보를 10개까지 채우거나 스타트업 적격성을 검증하는 로직도 없다. README 8절의 '현재 회사 리스트 직접 입력'은 CLI와는 맞지만 app/노트북의 회사명 추출 기능을 설명하지 못한다.

근거: `README.md:157, 183`, `investment_pipeline/services.py:97`, `investment_pipeline/graph.py:268`, `agents/service.py:78, 233`.

**B4. '14개 전문 에이전트'는 경로 공통의 실행 구성을 나타내지 않는다.**

README 7-1에는 14개 역할이 있다. CLI는 이와 유사한 역할을 그래프 노드로 나누지만 list_candidates와 추가 조사의 실제 역할이 축소되어 있다. app/노트북 그래프는 9개 노드이며 Supervisor 안의 평가 함수는 6개이고, 추가 조사 에이전트는 없다. 노드 수와 에이전트 수를 동일시하기보다 각 역할의 구현·실행 범위를 문서화해야 한다.

근거: `README.md:12, 155`, `investment_pipeline/graph.py:222, 333`, `agents/service.py:299, 467`.

### C. 기술 스택·운영 설명의 차이

**C1. GPT-4o 및 멀티모달 설명은 현재 설정·입력과 다르다.**

두 경로의 기본 모델은 `gpt-4.1-mini`이다. 확인한 LLM 호출은 텍스트 문맥과 텍스트 프롬프트이며 이미지·음성 입력을 처리하는 경로가 없다. README에는 사용 모델과 사용 중인 기능을 구분해 적어야 한다.

근거: `README.md:77`, `agents/models.py:15`, `investment_pipeline/config.py:24`, `agents/service.py:132`, `investment_pipeline/llm.py:28`.

**C2. FlagEmbedding / bge-m3 sparse·ColBERT 설명은 구현과 다르다.**

CLI는 SentenceTransformer로 dense 벡터만 생성하고 sparse는 scikit-learn TF-IDF로 만든다. bge-m3 native sparse, ColBERT, FlagEmbedding 호출은 확인되지 않는다. `sparse_embedding_model='Qdrant/bm25'` 설정도 검색 구현에서 사용하지 않는다. app/노트북은 HuggingFaceEmbeddings + FAISS dense 검색이다. 따라서 bge-m3 기반 검색이 없다는 기존 판단은 부정확하지만 README가 시사하는 전체 기능을 사용한다는 설명도 부정확하다.

근거: `README.md:81, 89`, `investment_pipeline/retrieval.py:19, 85, 90, 115`, `investment_pipeline/config.py:36`, `agents/service.py:48`.

**C3. RDB와 Qdrant In-Memory 저장 구조는 코드와 다르다.**

애플리케이션 소스에서 RDB 연결, 테이블/ORM 모델 또는 CRUD 경로는 확인되지 않는다. SQLAlchemy가 requirements에 있다는 사실만으로 RDB 사용을 확인할 수는 없다. CLI의 Qdrant는 `QdrantClient(path=...)`로 로컬 디스크 저장을 사용하며 payload에 메타데이터를 함께 넣는다. app/노트북은 FAISS이다. JSON 입력·캐시·출력을 포함한 실제 저장 구조로 설명을 수정해야 한다.

근거: `README.md:91`, `investment_pipeline/retrieval.py:65, 181`, `investment_pipeline/tavily.py:39`, `agents/service.py:53, 386`.

**C4. '최신 자료'를 항상 수집한다는 설명은 보장되지 않는다.**

CLI는 Tavily 키와 live_research 설정이 있어야 검색하며 키가 없으면 빈 증거를 반환한다. 파일 캐시는 만료·갱신 조건 없이 재사용한다. 회사 검색 요청의 days는 1825, 시장은 3650이다. app/노트북은 로컬 Markdown을 사용한다. 최신성 보장이나 특허·뉴스·IR별 수집기를 갖춘 상태로 설명하기 어렵다.

근거: `README.md:35`, `investment_pipeline/tavily.py:31, 35, 43`, `investment_pipeline/services.py:335, 360`, `agents/service.py:78`.

**C5. 실행 재현을 위한 설정·의존성이 누락되어 있다.**

CLI 설계 문서 경로 기본값은 특정 개발자의 `/Users/angj/Downloads/...` 절대 경로이며 현재 환경에는 없다. `get_knowledge_base`는 파일이 없으면 None을 반환한다. 이 때문에 LLM을 켜더라도 `make_evaluation`의 설계 문서 문맥 기반 보강은 실행되지 않을 수 있다. bge-m3는 CLI에서 offline/local_files_only로 로드하므로 모델 캐시 사전 준비가 필요하다. 모델이 없을 때 토큰 검색으로 대체하는 fallback은 없다. CLI PDF 생성이 import하는 `reportlab`도 requirements.txt에 없다. README는 두 실행 방법, 환경 변수, 문서 경로 및 모델 준비 방법을 안내하지 않는다.

근거: `investment_pipeline/config.py:19`, `investment_pipeline/services.py:67, 537`, `investment_pipeline/retrieval.py:12, 73`, `investment_pipeline/pdf_export.py:6`, `requirements.txt`, README 전체.

**C6. README의 최종 PDF는 존재하지만 기본 진입점의 출력은 아니다.**

`outputs/final_ai_semiconductor_report.pdf`는 실제 저장소에 있다. 다만 app/노트북 경로는 타임스탬프 Markdown/JSON을 만들고 PDF 변환을 호출하지 않는다. CLI는 기본 `outputs/final_report.md`와 같은 이름의 PDF/JSON을 생성한다. README에 적힌 파일은 저장된 예시 결과로 구분하고 생성 명령을 명시해야 한다.

CLI 템플릿의 추천·보류 보고서 주요 목차는 README 9절과 대체로 일치한다. app/노트북 보고서는 LLM 프롬프트로 생성되어 같은 구조를 코드로 강제하지 않는다.

근거: `README.md:14, 191`, `agents/service.py:426, 450`, `investment_pipeline/config.py:18`, `investment_pipeline/cli.py:39`, `investment_pipeline/reporting.py:204, 308`.

**C7. 공수 90% 절감·주관적 판단 배제는 현재 근거로 검증할 수 없다.**

README의 90% 절감 수치를 뒷받침할 전후 측정 자료·벤치마크는 확인하지 못했다. app/노트북은 LLM 판단으로 점수를 생성하고 CLI는 사전에 입력한 signal 및 키워드 규칙을 사용하므로 '주관적인 판단을 배제'했다는 강한 표현도 코드만으로 입증되지 않는다. 목표 효과와 측정된 결과를 구분해야 한다.

근거: `README.md:12, 57`, `agents/service.py:255, 309`, `investment_pipeline/services.py:270, 560`.

## 3. 확인 방법과 범위

- 진입점부터 그래프, 검색, 점수 계산, 분기, 보고서 저장까지 정적으로 추적했다.
- 저장된 최종 JSON은 `score_threshold=65`, `high_priority_threshold=80`, `branch=top3`이고 EnCharge AI·Etched·Groq가 선정되어 있다. 이는 CLI 형식의 결과이며 현재 모든 실행 환경에서 동작한다는 증명은 아니다.
- 외부 API·모델 로딩 없이 AST로 실제 점수 함수를 추출해 확인했다. 항목 1/3/5점의 총점 환산은 20/60/100이고 등급 경계 49/50/64/65/79/80은 README 5-3과 일치했다.
- Series A 시장성 5·트랙션 1의 개별 기여 23점과 병합 기여 21점을 확인했다.
- 동일한 funding 키워드 문서가 2개일 때 risk_signal=4, 4개일 때 5이며 역산된 리스크 점수는 각각 2, 1이다. 문서 수 4개 사례는 함수 규칙 검증용이고 기본 검색 결과 최대 3개와 구분한다.
- 전체 파이프라인/API 호출, 패키지 신규 설치, PDF 재생성은 수행하지 않았다. README 그림의 내부 표기까지 검증한 분석은 아니다.
- README와 구현 코드는 수정하지 않았다. 이 분석 문서만 추가했다.

## 4. 정리 우선순위

1. 공식 실행 경로를 정하고 두 경로의 점수 범위·임계값·Stage 반영 여부를 명확히 설명한다.
2. 점수 공식, Series A 가중치, 시장성/트랙션 결합, 리스크 점수 해석을 일관되게 정한다.
3. 65점 포함 여부를 통일하고 설정 변경 시 등급과 분기가 함께 바뀌도록 기준을 정한다.
4. 병렬 실행, 추가 조사, 후보 수집의 현재 구현 범위와 향후 계획을 구분한다.
5. 모델·검색기·저장소·출력 경로를 실제 코드 기준으로 문서화하고 재현 절차를 보완한다.

README 8절에서 향후 계획으로 명시한 Series C 보정 계수 0.6, 기업 유형별 평가, 고유 정보와 도메인 정보의 정합성 검사 루프는 현재 구현되지 않은 확장 계획으로 분류한다. 완료 기능의 누락으로 계산하지 않는다.
