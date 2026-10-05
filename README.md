# AI Semiconductor Startup Investment Evaluation Agent

> **AI 반도체 스타트업 10개를 자동으로 분석하고, 투자할 가치가 있는 Top3를 골라 보고서를 작성하는Multi-Agent 운영 지원 시스템**
> 

---

## 1. Summary

**"복잡한 AI 반도체 투자 심사, AI가 데이터로 증명합니다.”**

본 프로젝트는 AI 반도체 스타트업의 기술적 복잡성과 시장 변동성을 정밀하게 분석하기 위해 설계된 **LangGraph 기반 Multi-Agent 운영 체제**입니다. 14개의 전문 에이전트가 협업하여 데이터 수집부터 최종 투자 추천 보고서 작성까지의 전 과정을 자동화하며, 주관적인 판단을 배제한 **DD-Worthiness Score**를 통해 투자 의사결정을 지원합니다.

최종 Output 보고서: `outputs/final_ai_semiconductor_report.pdf`

---

## 2. Overview

### The Problem: AI 반도체 투자의 높은 진입장벽

| 문제 | 설명 |
| --- | --- |
| 🧩 **기술적 난해함** | TRL(기술성숙도) 및 아키텍처 우위성에 대한 전문적 분석 부재 |
| ⚖️ **평가의 비일관성** | 심사역마다 서로 다른 기준(기술 vs 시장)으로 인한 평가 편향 발생 |
| ⏱️ **리소스 과다** | 10개 이상의 초기 스타트업을 심층 분석하는 데 수주일의 시간 소요 |

### The Solution: 14-Agent Orchestration

본 시스템은 **Supervisor Design Pattern**을 채택하여 분석의 깊이와 속도를 동시에 확보했습니다.

| 핵심 기능 | 상세 내용 |
| --- | --- |
| 📊 **Stage-Aware 가중치** | 투자 단계(Seed ~ Series C+)별 동적 가중치를 적용하여 공정한 벤치마킹 수행 |
| 🔎 **Hybrid Analysis** | `bge-m3` 임베딩 기반 RAG를 통해 최신 특허·뉴스·IR 자료를 정밀 검색 |
| 🏅 **DD-Worthiness Scoring** | 기술력·시장성·팀·경쟁우위·리스크 5대 지표를 0~100점으로 정량화 |

### Decision Logic & Output

> 시스템은 산출된 점수에 따라 엄격한 알고리즘 기반의 리포트를 발행합니다.
> 

| 조건 | 출력 |
| --- | --- |
| 🏆 **Case A** — Score > 65 | **Top 3 투자 추천 보고서** 발행 (실사 DD 우선순위 제안) |
| 🛑 **Case B** — Score ≤ 65 | **전체 투자 보류 보고서** 발행 (미달 사유 및 재검토 조건 명시) |

---

## 3. Key Features

| **Feature** | **Description** | **Key Benefit** |
| --- | --- | --- |
| **📊 Stage-Aware** | Seed~Series C+ 단계별 평가 비중 자동 최적화 | 공정한 벤치마크 실현 |
| **🤖 Multi-Agent** | Supervisor 패턴 기반 5개 전문 에이전트 병렬 조율 | 분석 속도 및 일관성 확보 |
| **🏆 Smart Ranking** | DD-Worthiness Score 기반 Top 3 자동 필터링 | 투자 우선순위 즉시 파악 |
| **📝 Auto Report** | 점수 결과에 따른 추천/보류 보고서 즉시 발행 | 리포트 작성 공수 90% 절감 |

---

## 4. Tech Stack

본 프로젝트는 AI 반도체 스타트업 분석 및 평가를 위해 다음과 같은 기술 스택을 활용합니다.

### 🧠 Core Framework & AI

- **Language:**
    
    [Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
    
- **Orchestration:** *분석 워크플로우 제어 및 순환 그래프(Cyclic Graph) 구현*
    
    [LangChain](https://img.shields.io/badge/LangChain-1C3C3C?style=for-the-badge&logo=langchain&logoColor=white)
    
    [LangGraph](https://img.shields.io/badge/LangGraph-262626?style=for-the-badge&logo=diagram&logoColor=white)
    
- **LLM:** *고성능 멀티모달 추론 및 보고서 생성*
    
    [GPT-4o](https://img.shields.io/badge/GPT--4o-412991?style=for-the-badge&logo=openai&logoColor=white)
    
- **Embedding:** *Multilingual, Dense/Sparse/ColBERT 동시 지원*
    
    [BAAI/bge-m3](https://img.shields.io/badge/BAAI%2F-bge--m3-B31B1B?style=for-the-badge&logo=google-cloud&logoColor=white)
    

### 🔍 Retrieval & Search

- **Search Engine:** `Hybrid Search` (Semantic + Keyword)
- **Retriever:** `FlagEmbedding` (Vector DB Optimization)

### 💾 Data Management

- **Storage:** *정형 데이터(Metadata)와 비정형 데이터(Vector)의 하이브리드 관리*
    
    [RDB](https://img.shields.io/badge/RDB-%EC%8A%A4%ED%83%80%ED%8A%B8%EC%97%85%20%EB%A9%94%ED%83%80%20DB-blue?style=for-the-badge&logo=postgresql&logoColor=white)
    
    [Vector DB](https://img.shields.io/badge/Vector%20DB-Qdrant%20In--Memory-red?style=for-the-badge&logo=qdrant&logoColor=white)
    

---

## 5. Evaluation Criteria — DD-Worthiness Score

> **"실사(Due Diligence)할 가치가 있는 회사인가?"** 를 0~100점으로 수치화
> 

### 5-1. 평가 항목 및 가중치 (기본값 / Series A 기준)

| # | 평가 항목 | 가중치 | 핵심 질문 |
| --- | --- | --- | --- |
| 1 | Team & Founders | 20% | 반도체·AI 다학제 팀인가? 사업화 경험이 있는가? |
| 2 | Market Attractiveness | 20% | TAM $1B 이상? AI 인프라 수요가 뒷받침하는가? |
| 3 | Technology & Product | 20% | TRL 단계는? 칩 아키텍처 차별성과 IP가 있는가? |
| 4 | Traction & Commercialization | 15% | 고객 PoC, 디자인 파트너, 초기 매출이 있는가? |
| 5 | Competitive Advantage | 15% | 경쟁사 대비 지속 가능한 기술 Moat이 있는가? |
| 6 | Execution & Financing Risk | 10% | 제조 파트너와 자금 조달 가능성이 있는가? |

> 점수 공식 : `Final Score = Σ (score × weight × 10/3)` — 각 항목 1~5점
> 

### 5-2. Stage별 동적 가중치

스타트업은 발전하면서 확인 가능한 정보와 사업 성숙도가 달라지기 때문에 동일한 평가 기준을 동일한 비중으로 적용하기 어렵다. 때문에 단계에 따라 투자자가 중요하게 보는 메트릭의 가중치를 조정할 필요가 있다.

| 평가 항목 | Seed | Series A | Series B | Series C+ |
| --- | --- | --- | --- | --- |
| Team & Founders | 30% | 25% | 20% | 15% |
| Market Attractiveness | 25% | 20% | 20% | 15% |
| Technology & Product | 25% | 25% | 20% | 15% |
| Traction | 5% | 15% | 25% | 25% |
| Competitive Advantage | 10% | 10% | 10% | 15% |
| Risk | 5% | 5% | 5% | 15% |

### 5-3. 투자 판단 기준

| 점수 | 판정 | 의미 |
| --- | --- | --- |
| 80–100 | 🟢 High Priority DD | 우선 정밀 실사 진행 |
| 65–79 | 🟡 Selective DD | 조건부 실사, 추가 확인 필요 |
| 50–64 | 🟠 Watchlist | 추적 관찰 |
| 0–49 | 🔴 No DD | 현시점 투자 검토 대상 아님 |

---

## 6. Flow Chart

![image.png](README%20md/image.png)

---

## 7. Agents 설계

### 7-1. Agent Lists

| 순서 | Agent | 한 줄 역할 요약 |
| --- | --- | --- |
| 0 | 기업 리스트업 | 후보 기업 10개 수집, 1차 풀 구성 |
| 1 | 시장성 조사 | AI 반도체 도메인 전체 시장 환경 파악 |
| 2 | 기업 조사 | 기업별 기초 정보 (기술·팀·고객·특허) 수집 |
| 3 | 투자 판단 (Supervisor) | 5개 평가 종합 → DD-Worthiness Score 산출 |
| 3-a | 기술 평가 | 기술 차별성·TRL·IP 정성 평가 |
| 3-a-i | 추가 조사 (기술) | 기술 정보 부족 시 자동 보완 → 재평가 |
| 3-b | 시장성 평가 | 시장 크기·고객 문제 심각성·확장성 평가 |
| 3-b-i | 추가 조사 (시장) | 시장 정보 부족 시 자동 보완 → 재평가 |
| 3-c | 팀 역량 평가 | 창업자·핵심 팀 실행력 평가 |
| 3-d | 리스크 분석 | 기술·시장·규제·운영·재무 리스크 식별 |
| 3-e | 경쟁사 비교 | 유사 기업 대비 상대적 경쟁력 평가 |
| 4 | 기업 리스트 생성 | 65점 기준 필터링 → Top3 선정 또는 전체 보류 |
| 5-1 | Top3 보고서 생성 | 투자 추천 보고서 자동 작성 |
| 5-2 | 보류 보고서 생성 | 전체 보류 보고서 자동 작성 |

### 7-2. State Diagram

![image.png](README%20md/image%201.png)

---

## 8. 추후 확장

1. **Feedback loop pattern 적용:** 
    - 특정 기업 조사 과정에서 특허, 핵심 기술 등 기업 고유 정보가 기존 도메인 정보와 불일치할 수 있다. 이 경우 추가 탐색 단계를 반복 수행해 정보 누락을 보완하고, 더 정합성 높은 기업 프로파일을 구성할 필요가 있다.
2. **기업 탐색 agent 필요:** 
    - 현재 시스템은 사용자 또는 운영자가 분석 대상 회사 리스트를 직접 입력하는 방식으로 동작한다. 따라서 시스템이 후보 기업을 스스로 발굴하지 못하며, 도메인 내 신규 기업 탐색, 시장 변화 반영, 후보군 확장에 한계가 있다. 이를 보완하기 위한 별도 기업 탐색 agent가 필요하다.
3. **스타트업 Stage별 가중치 조정 필요:** 
    - Series C 기업의 경우 점수별 가중치 조정 이후에도 높은 총점을 받아 다른 기업과의 변별 없이 상위 후보로 고정되는 문제가 관찰되었다. 이를 완화하기 위해 추가 조정 계수(예: 0.6)를 적용해 점수를 보정할 예정이다.
4. **기업 유형별 평가 기준 확장 필요:** 
    - 같은 도메인 기업이라도 제조 중심 기업과 연구 중심 기업은 사업 구조, 기술 성숙도, 수익화 방식, 리스크 요인이 다르다. 따라서 모든 기업에 동일한 평가 기준을 적용하기보다는, 기업 유형에 따라 별도의 평가 항목과 기준을 적용할 수 있도록 확장할 필요가 있다. 이를 통해 각 기업의 특성에 맞는 더 정밀하고 현실적인 투자 판단이 가능해진다.

---

## 9. Output Report Structure

### 9-1. 투자 추천 보고서 목차 (Top3 존재 시)

```
1. Executive Summary
2. Market Overview
3. Candidate Comparison
4. Individual Company Analysis (기업별)
   ├── Executive Overview
   ├── Technology Assessment
   ├── Market Position
   └── Risk Assessment
5. Scorecard Evaluation
6. DD-Worthiness Decision
7. Investment Recommendation
   References
```

### 9-2. 투자 보류 보고서 목차 (전체 보류 시)

```
1. Executive Summary
2. Market Overview
3. Candidate Comparison
4. Individual Company Analysis
5. Scorecard Evaluation
6. DD-Worthiness Decision
7. Future Monitoring Candidates
   References
```

---

## 10. 📁 Directory Structure

```
├── data                                         # 입력 데이터와 실행용 샘플 회사 목록
├── investment_pipeline                          # LangGraph/LangChain 기반 핵심 애플리케이션 코드
└── outputs                                      # 실행 결과물과 검색/벡터 저장 산출물
    ├── qdrant                                   # Qdrant 기반 로컬 벡터 DB 저장소
    │   └── collection                           # 컬렉션 단위 인덱스 저장 디렉토리
    │       └── smoke_hybrid_demo                # 하이브리드 검색 컬렉션 데이터
    └── research_cache                           # Tavily 웹 리서치 캐시 문서 저장소

```

---

## 11. Investment Report Highlights

### 투자 판단 평가 기준 설계

- 도메인과 스타트업 특성을 모두 반영한 평가 기준 설계
    - 도메인 특성을 반영한 6가지 평가 항목
    - 스타트업 단계 특성을 반영한 평가 항목별 **동적 가중치**

---

## 12. Lessons Learned

- 명확한 **설계 문서와 시각화**
    - 프로젝트 초기 단계에서 시스템 구조와 설계 의도를 문서화하고 이를 다이어그램 형태로 시각화하여 공유하는 과정이 팀 내 이해도를 맞추는 데 큰 도움이 되었다. 서로 다르게 이해하고 있던 부분을 조기에 발견하고 수정할 수 있었다. 문서 기반 협업 방식은 커뮤니케이션 오류로 인해 발생할 수 있는 불필요한 시행착오와 리소스 낭비를 줄이는 데 효과적이었다.
    - 특히나 AI 기반 구현에서는 에이전트 구조, 데이터 흐름, 평가 로직 등이 명확하게 정의된 설계 문서가 있어야 정확한 구현을 안정적으로 진행할 수 있다는 점을 알 수 있었다.
- 촘촘한 설계의 중요성
    - 설계 단계에서 구조와 요구사항을 충분히 구체화하지 않으면 구현 단계에서 새로운 요구사항이 지속적으로 추가되어 구현 단계의 복잡도를 증가시키고 전체 개발 시간을 크게 늘리는 원인이 된다.
    - 이번 프로젝트에서는 이전 팀 프로젝트 경험과 비교하여 설계와 구현의 시간비가 역전되었다. 설계 단계에 더 많은 시간을 투자하였는데 더 안정적인 프로젝트 진행이 되었다. 이를 통해 초기 설계의 완성도가 전체 개발 효율과 프로젝트 안정성에 큰 영향을 미친다는 점을 체감하였다.
- 도메인 지식의 중요성
    - 본 프로젝트에서는 스타트업 투자 분석과 AI 반도체 산업이라는 도메인을 다루면서 관련 개념과 산업 구조를 이해하는 데 예상보다 많은 시간이 필요했다.
    - 이러한 경험을 통해 기술 지식뿐 아니라 서비스 도메인 지식도 꾸준히 학습하고, 충분한 이해를 바탕으로 설계 단계에 들어가는 것이 중요하다는 점을 깨닫게 되었다.

---

## 13. Contributors

- 김연수 : LangGraph 설계, Agent Pattern 설계 State-diagram 작성, Flow-chart 작성, 서비스 설계
- 장재훈 : LangGraph 설계, start-up search, Agent Pattern 설계
- 김유빈 : 아키텍처 설계, 기술 조사, Agent Pattern 설계
- 양예원 : 투자 판단 평가 기준 설계, 투자 보고서 template 작성, 조사 agent prompting 작성

## 14. 평가 실행 및 전후 비교

제품 품질 변경 전 원본 실행을 보존하고 동일 사례를 재평가하는 방법은
[baseline·frozen/live·paired 비교 실행 안내](docs/evaluation/issue29.md)를 참조하세요.
RAGAS 주 지표, 독립 사실·필수 정보·규칙 검사, 워크플로우 성공 여부,
생성 비용·시간 및 평가 운영 비용·시간을 별도로 비교합니다.
