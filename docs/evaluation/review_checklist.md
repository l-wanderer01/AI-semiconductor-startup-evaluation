# #21 최초 실제 자료 검토 목록

이 파일은 검토 안내다. 작성 시점에는 아래 항목을 사람이 승인하지 않았다.
검토 결과의 저장 위치는 `data/evaluation/semiconductor-baseline/0.1.0-draft/dataset.json`을
복사한 **별도 작업본**의 facts와 case_reviews다. 발행 방법은 [issue21.md](issue21.md)를 따른다.

| fact_id | 확인할 내용 | 주의할 오류 | 현재 상태 |
| --- | --- | --- | --- |
| fact_groq_2024_round_1 | 2024년 8월 라운드가 Series D인지 | 현재 투자 단계라는 주장으로 바꾸지 않기 | pending |
| fact_groq_2024_round_2 | 해당 라운드 USD 640,000,000 | 6,400만 달러·원화·누적 투자액·매출로 바꾸지 않기 | pending |
| fact_tenstorrent_2024_round_1 | 2024-12-02 발표된 Series D인지 | 다른 발표 날짜와 혼동하지 않기 | pending |
| fact_tenstorrent_2024_round_2 | 해당 라운드 USD 693,000,000 초과 | 정확히 693M으로 단정하거나 pre-money valuation과 합치지 않기 | pending |

원문:

- [Groq 거래에 참여한 Fenwick 발표](https://www.fenwick.com/insights/experience/fenwick-represents-cisco-investments-in-groqs-640m-series-d-funding)
- [Tenstorrent의 기업 발표](https://www.prnewswire.com/news-releases/tenstorrent-closes-693m-of-series-d-funding-led-by-samsung-securities-and-afw-partners-302319584.html)

case 검토에서 다음 내용을 직접 판단한다:

1. 질문이 해당 과거 라운드의 단계/금액만 요구하는가?
2. 저장된 사실과 발췌로 질문의 모든 필수 사실을 충족하는가?
3. `conditions`가 금액 범위, 통화, 발표 주체, 사건 시점을 보존하는가?
4. 매출·현재 고객·현재 투자 단계·전체 보고서처럼 범위 밖 정보가 정답에 섞이지 않았는가?
5. 실제 검색이 있는 경우 relevant_source_ids의 원문이 해당 질문에 적합한가?

검토 완료 예시의 필드 형태:

```json
{
  "review_status": "verified",
  "review_kind": "human",
  "reviewer": "실제로 검토한 사람의 ID",
  "reviewed_at": "실제 검토 시각의 ISO 8601 UTC 값"
}
```

case_reviews에는 review_status 대신 `completeness: "complete"`를 기록한다.
형태를 복사하는 것만으로 검토가 완료되는 것은 아니다. 먼저 원문과 범위를 대조한다.
