from __future__ import annotations

from evaluation.recording import traced

from .llm import llm_client


FULL_REPORT_TRANSLATION_PROMPT = """
당신은 스타트업 투자심사 보고서를 최종 편집하는 한국어 편집자다.

아래 Markdown 보고서를 다음 규칙에 맞게 전체 재작성하라.

규칙:
- Markdown 구조와 제목은 유지한다.
- 모든 섹션 제목과 목차 헤더는 영어 그대로 유지한다.
- 제목 아래의 본문, bullet, 설명 문장은 모두 한국어로 다시 쓴다.
- 표의 헤더, 회사명, Series, 점수, URL, REFERENCE 항목은 유지한다.
- 본문에 영어 문장을 남기지 않는다. 예외는 회사명, Series, DD-Worthiness, Selective DD, REFERENCE, 표 헤더, URL뿐이다.
- 영어 웹페이지 스니펫, navigation text, 잡음 문구는 제거한다.
- 긴 원문 조각은 자연스러운 한국어 투자 메모 문체로 1~3문장 요약으로 바꾼다.
- 회사별 Executive Overview, Technology Assessment, Market Position, Risk Assessment는 각각 완결된 한국어 분석 문단으로 다시 쓴다.
- 사실을 새로 만들지 말고, 제공된 내용만 정리한다.
- REFERENCE 형식과 링크는 훼손하지 않는다.
- 출력은 최종 Markdown 본문만 반환한다.
"""


@traced('polish_report_to_korean')
def polish_report_to_korean(markdown: str) -> str:
    if not llm_client.available:
        return markdown

    polished = llm_client.invoke_text(
        f"{FULL_REPORT_TRANSLATION_PROMPT}\n\n[Markdown Report]\n{markdown}"
    )
    return (polished or markdown).strip() + "\n"
