# Synthetic paired smoke example

유료 API 호출 0, 실제 RAGAS 0.4.3 + 고정 응답. 사람 검토/실모델 정확도 검증 결과가 아닙니다.

| 역할 | 단계 | factual precision 전→후 | factual recall 전→후 | faithfulness 전→후 | 필수 정보 전→후 | workflow 성공 |
|---|---|---|---|---|---|---|
| business | intermediate | 1.0 → 1.0 | 1.0 → 1.0 | 1.0 → 1.0 | 1.0 → 1.0 | false |
| delivered_report | final | 1.0 → 1.0 | 1.0 → 1.0 | 1.0 → 1.0 | 1.0 → 1.0 | false |
| report_writer | intermediate | 1.0 → 1.0 | 1.0 → 1.0 | 1.0 → 1.0 | 1.0 → 1.0 | false |

생성 산출물은 저장되었지만 필수 Agent/결정 coverage가 부족해 양쪽 workflow 성공률은 0/1입니다.
규칙 평가 미완료는 null이며 전체 Agent 성공을 선언하지 않습니다. Context 지표는 연결된 검색이 없어 null입니다.

- [전체 sample 값과 독립 품질 판정](summary.json)
- [지표별 차이 및 상태 CSV](comparison.csv)
- [제품 품질 코드 수정 전 baseline receipt](prechange-baseline-receipt.json)

전체 원본·stage package·reference·오류 상세·JSON/Markdown/CSV는 `python -m tests.issue29_demo`로 새 immutable run에 재생성합니다.
생성/평가 API 비용은 provider usage와 가격 미확인 시 null입니다. synthetic 값을 실제 API 가격이나 품질 성능으로 해석하지 않습니다.
