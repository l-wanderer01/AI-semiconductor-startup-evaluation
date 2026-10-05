from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

from evaluation.recording import traced

from .config import settings
from .llm import llm_client
from .models import (
    CompanyProfile,
    CompanyResearch,
    CompanyResearchLLMOutput,
    CompanyWebResearch,
    EvaluationLLMOutput,
    EvaluationResult,
    MarketResearch,
    MarketResearchLLMOutput,
    MarketWebResearch,
    ResearchEvidence,
)
from .prompts import COMPANY_RESEARCH_PROMPT, EVALUATION_PROMPT, MARKET_PROMPT
from .retrieval import (
    DesignDocumentKnowledgeBase,
    build_evidence_knowledge_base,
    format_docs,
)
from .scoring import weighted_score
from .tavily import tavily_client


_knowledge_base = None
_company_research_cache: Dict[str, CompanyWebResearch] = {}
_market_research_cache: Dict[str, MarketWebResearch] = {}
_company_evidence_kb_cache = {}
_market_evidence_kb_cache = {}

_NOISE_PATTERNS = [
    r"skip to (?:content|footer|main content)",
    r"share this:?|sign in|subscribe|cookie",
    r"powered by|loading chart|view more|warning!",
    r"org chart|display daily|graphicspeak|babeltechreviews",
    r"santa clara, calif\.,",
]

_CATEGORY_FALLBACKS = {
    "기업 개요": "{company}는 {stage} 단계에서 AI 반도체 제품 상용화와 고객 검증을 추진 중인 기업이다.",
    "사업화 현황": "{company}는 초기 고객 발굴과 사업화 경로 검증을 병행하며 상용화 가능성을 높이고 있다.",
    "기술 및 제품": "{company}는 AI 연산 성능과 전력 효율 개선에 초점을 둔 반도체 아키텍처를 개발 중이다.",
    "트랙션": "{company}는 고객 검증과 파트너십 확대를 통해 초기 트랙션을 축적하는 단계다.",
    "팀 역량": "{company}는 반도체와 AI 시스템 경험을 보유한 창업진 및 핵심 인력을 기반으로 실행력을 확보하고 있다.",
    "경쟁 우위": "{company}는 아키텍처 차별화와 제품 포지셔닝을 통해 경쟁 우위를 구축하려는 단계다.",
    "리스크": "{company}는 양산 실행력, 고객 전환, 추가 자금 조달 측면의 리스크를 함께 관리해야 한다.",
    "사업 모델": "{company}는 제품 상용화와 초기 고객 확보를 중심으로 사업 모델을 구체화하고 있다.",
    "제품 개요": "{company}는 AI 반도체 제품을 통해 추론 및 가속기 수요를 공략하고 있다.",
    "고객 및 시장": "{company}는 데이터센터, 엣지, AI 인프라 수요처를 중심으로 시장 진입을 시도하고 있다.",
    "차별화 요소": "{company}는 아키텍처와 제품 전략 차별화를 통해 경쟁우위를 확보하려 한다.",
    "팀 요약": "{company}는 기술 중심 팀 구성을 바탕으로 제품 개발과 사업화를 병행하고 있다.",
    "시장성 요약": "{company}는 AI 반도체 수요 확대 국면에서 시장 진입 기회를 보유하고 있다.",
    "기술 요약": "{company}는 성능과 효율을 함께 개선하는 AI 반도체 기술 개발에 집중하고 있다.",
    "트랙션 요약": "{company}는 파트너십과 고객 검증을 통해 초기 트랙션을 쌓는 단계다.",
    "경쟁력 요약": "{company}는 제품 포지셔닝과 기술 차별성을 기반으로 경쟁력을 강화하고 있다.",
    "리스크 요약": "{company}는 양산, 고객 확보, 자금 조달 측면의 실행 리스크를 안고 있다.",
}


@traced('get_knowledge_base')
def get_knowledge_base() -> DesignDocumentKnowledgeBase | None:
    global _knowledge_base
    if _knowledge_base is not None:
        return _knowledge_base
    source_path = settings.default_design_doc_path
    if not source_path.exists():
        return None
    _knowledge_base = DesignDocumentKnowledgeBase.from_markdown(source_path)
    return _knowledge_base


@traced('get_company_evidence_kb')
def get_company_evidence_kb(company: CompanyProfile):
    cache_key = f"{company.name}:{company.stage}"
    if cache_key in _company_evidence_kb_cache:
        return _company_evidence_kb_cache[cache_key]
    company_live = _search_company(company)
    kb = build_evidence_knowledge_base(key=cache_key, evidence=company_live.evidence)
    _company_evidence_kb_cache[cache_key] = kb
    return kb


@traced('get_market_evidence_kb')
def get_market_evidence_kb(domain: str):
    if domain in _market_evidence_kb_cache:
        return _market_evidence_kb_cache[domain]
    market_live = _search_market(domain)
    kb = build_evidence_knowledge_base(key=domain, evidence=market_live.evidence)
    _market_evidence_kb_cache[domain] = kb
    return kb


def load_companies(path: Path) -> List[CompanyProfile]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [CompanyProfile.model_validate(item) for item in payload["companies"]]


def _compact_text(text: str, limit: int = 320) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")


def _clean_source_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    for pattern in _NOISE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"[#|]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -:;,")
    return cleaned


def _first_meaningful_chunk(text: str) -> str:
    cleaned = _clean_source_text(text)
    if not cleaned:
        return ""
    chunks = re.split(r"(?<=[.!?])\s+| \.\.\. |; ", cleaned)
    filtered = []
    for chunk in chunks:
        chunk = chunk.strip(" -:;,")
        if len(chunk) < 40:
            continue
        if any(re.search(pattern, chunk, flags=re.IGNORECASE) for pattern in _NOISE_PATTERNS):
            continue
        filtered.append(chunk)
    if filtered:
        return _compact_text(" ".join(filtered[:2]), limit=280)
    return _compact_text(cleaned, limit=280)


def _needs_korean_rewrite(text: str) -> bool:
    english_chars = len(re.findall(r"[A-Za-z]", text))
    korean_chars = len(re.findall(r"[가-힣]", text))
    return english_chars > korean_chars


def _rewrite_as_korean_note(
    *,
    text: str,
    fallback: str,
    context_label: str,
    company_name: str = "",
) -> str:
    cleaned = _first_meaningful_chunk(text)
    if not cleaned:
        cleaned = fallback

    safe_fallback = fallback.strip() or _CATEGORY_FALLBACKS.get(
        context_label,
        "{company}는 핵심 사업과 실행력을 검증 중인 단계다.",
    ).format(company=company_name or "해당 기업", stage="")

    if llm_client.available and cleaned and _needs_korean_rewrite(cleaned):
        subject = f"{company_name} {context_label}".strip()
        prompt = (
            "당신은 VC 투자심사 애널리스트다.\n"
            "아래 근거를 1~2문장의 자연스러운 한국어 투자 메모 문장으로 정리하라.\n"
            "규칙:\n"
            "- 웹페이지 잡음, 헤더, 네비게이션 문구는 제거한다.\n"
            "- 회사명, Series, 수치, 핵심 사실은 유지한다.\n"
            "- 과장 없이 사실 중심으로 쓴다.\n"
            "- 문장만 반환한다.\n\n"
            f"주제: {subject or context_label}\n"
            f"기본 문장: {fallback}\n"
            f"근거: {cleaned}"
        )
        rewritten = llm_client.invoke_text(prompt)
        if rewritten:
            return _compact_text(rewritten, limit=320)

    if _needs_korean_rewrite(cleaned):
        return _compact_text(safe_fallback, limit=320)

    return _compact_text(cleaned or safe_fallback, limit=320)


def _summarize_evidence(
    evidence: List[ResearchEvidence],
    fallback: str,
    *,
    context_label: str,
    company_name: str = "",
) -> str:
    for item in evidence:
        if item.content:
            return _rewrite_as_korean_note(
                text=item.content,
                fallback=fallback,
                context_label=context_label,
                company_name=company_name,
            )
    return _rewrite_as_korean_note(
        text=fallback,
        fallback=fallback,
        context_label=context_label,
        company_name=company_name,
    )


def _normalize_evidence_items(evidence: List[str], *, category: str) -> List[str]:
    normalized: List[str] = []
    for item in evidence:
        cleaned = _clean_source_text(item)
        if not cleaned:
            continue
        if llm_client.available and _needs_korean_rewrite(cleaned):
            prompt = (
                "다음 근거 문장을 한국어 투자심사 메모용 짧은 bullet로 바꿔라.\n"
                "웹페이지 잡음은 제거하고 핵심 사실만 남긴다.\n"
                "한 줄만 반환한다.\n\n"
                f"카테고리: {category}\n"
                f"근거: {cleaned}"
            )
            rewritten = llm_client.invoke_text(prompt)
            cleaned = rewritten or cleaned
        elif _needs_korean_rewrite(cleaned):
            cleaned = f"{category} 관련 정성 근거가 확인되며, 세부 검증은 추가 실사가 필요하다."
        cleaned = _compact_text(cleaned, limit=180)
        if cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _evaluation_fallback_summary(category: str, signal: int) -> str:
    level = {
        5: "매우 우수한 편이다",
        4: "상대적으로 우수한 편이다",
        3: "보통 수준으로 판단된다",
        2: "보수적으로 접근할 필요가 있다",
        1: "핵심 리스크로 관리해야 한다",
    }[signal]
    mapping = {
        "Technology & Product": f"기술 완성도와 제품 차별성은 {level}. 추가 벤치마크와 제품 검증 자료 확인이 중요하다.",
        "Market & Traction": f"시장 진입 가능성과 초기 트랙션은 {level}. 고객 전환과 상용 매출 검증이 필요하다.",
        "Team & Founders": f"창업팀과 핵심 인력의 실행 역량은 {level}. 핵심 채용과 조직 확장 계획을 추가 확인할 필요가 있다.",
        "Execution & Financing Risk": f"실행 및 자금 조달 리스크는 {level}. 양산 일정과 자금 소진 계획 점검이 필요하다.",
        "Competitive Advantage": f"경쟁 우위와 차별화 요소는 {level}. 기술 모방 가능성과 포지셔닝 지속성을 확인해야 한다.",
    }
    return mapping.get(category, f"{category} 평가는 {level}.")


def _merge_references(evidence: List[ResearchEvidence]) -> List[str]:
    refs: List[str] = []
    for item in evidence:
        ref = f"{item.title} - {item.url}"
        if ref not in refs:
            refs.append(ref)
    return refs


def _pick_summary(
    evidence: List[ResearchEvidence],
    fallback: str,
    *,
    context_label: str = "",
    company_name: str = "",
) -> str:
    return _summarize_evidence(
        evidence,
        fallback,
        context_label=context_label or "요약",
        company_name=company_name,
    )


def _score_from_evidence(evidence: List[ResearchEvidence], fallback: int) -> int:
    if not evidence:
        return fallback
    strong = 0
    medium = 0
    for item in evidence:
        text = item.content.lower()
        if any(
            keyword in text
            for keyword in [
                "customer",
                "partner",
                "founder",
                "patent",
                "benchmark",
                "funding",
                "pilot",
                "design partner",
                "foundry",
                "revenue",
            ]
        ):
            strong += 1
        elif len(text) > 80:
            medium += 1
    if strong >= 4:
        return 5
    if strong >= 2 or medium >= 4:
        return 4
    if strong >= 1 or medium >= 2:
        return 3
    return max(2, fallback - 1)


@traced('_search_company')
def _search_company(company: CompanyProfile) -> CompanyWebResearch:
    cache_key = f"{company.name}:{company.stage}"
    if cache_key in _company_research_cache:
        return _company_research_cache[cache_key]

    query_map = {
        "Team & Founders": [
            f"{company.name} founders executives semiconductor startup",
        ],
        "Market Attractiveness": [
            f"{company.name} ai semiconductor customers target market",
        ],
        "Technology & Product": [
            f"{company.name} AI accelerator architecture patent benchmark",
        ],
        "Traction & Commercialization": [
            f"{company.name} design partner pilot customers funding partnership",
        ],
        "Competitive Advantage": [
            f"{company.name} proprietary architecture patent moat",
        ],
        "Execution & Financing Risk": [
            f"{company.name} foundry manufacturing funding risk",
        ],
    }

    queries: List[str] = []
    evidence: List[ResearchEvidence] = []
    for category, category_queries in query_map.items():
        for query in category_queries:
            queries.append(query)
            evidence.extend(tavily_client.search(query, category=category, days=1825))

    researched = CompanyWebResearch(
        company_name=company.name,
        stage=company.stage,
        industry=company.industry,
        search_queries=queries,
        evidence=evidence,
    )
    _company_research_cache[cache_key] = researched
    return researched


@traced('_search_market')
def _search_market(domain: str) -> MarketWebResearch:
    if domain in _market_research_cache:
        return _market_research_cache[domain]

    queries = [
        f"{domain} market size growth report",
        f"{domain} data center accelerator demand report",
        f"{domain} venture capital evaluation framework deep tech",
    ]
    evidence: List[ResearchEvidence] = []
    for query in queries:
        category = "Industry" if "framework" not in query else "Framework"
        evidence.extend(tavily_client.search(query, category=category, days=3650))

    researched = MarketWebResearch(domain=domain, search_queries=queries, evidence=evidence)
    _market_research_cache[domain] = researched
    return researched


@traced('build_market_research')
def build_market_research(domain: str) -> MarketResearch:
    kb = get_knowledge_base()
    market_live = _search_market(domain)
    design_docs = kb.search(f"{domain} market overview evaluation methodology") if kb else []
    market_kb = get_market_evidence_kb(domain)
    live_docs = (
        market_kb.search(f"{domain} market overview growth outlook investor framework", limit=6)
        if market_kb
        else []
    )
    context = format_docs(design_docs) if design_docs else ""
    live_context = format_docs(live_docs) if live_docs else ""
    combined_context = "\n".join(part for part in [context, live_context] if part)
    if combined_context and llm_client.available:
        prompt = (
            f"{MARKET_PROMPT}\n\n"
            f"Domain: {domain}\n"
            f"Reference context:\n{combined_context}\n"
            "Return structured Korean output."
        )
        enriched = llm_client.invoke_structured(prompt, MarketResearchLLMOutput)
        if enriched:
            return MarketResearch(
                domain=domain,
                market_size_summary=enriched.market_size_summary,
                growth_drivers=enriched.growth_drivers,
                regulatory_context=enriched.regulatory_context,
                references=enriched.references,
            )

    return MarketResearch(
        domain=domain,
        market_size_summary=_pick_summary(
            [item for item in market_live.evidence if item.category == "Industry"],
            "AI semiconductor 시장은 생성형 AI 인프라 확장과 데이터센터 수요 증가에 힘입어 구조적 성장세를 보인다.",
            context_label="시장 개요",
        ),
        growth_drivers=[
            _pick_summary(market_live.evidence[0:1], "생성형 AI 추론 수요가 확대되고 있다.", context_label="성장 동인"),
            _pick_summary(market_live.evidence[1:2], "데이터센터 전력 효율 개선 압력이 커지고 있다.", context_label="성장 동인"),
            _pick_summary(market_live.evidence[2:3], "GPU 대체형 가속기 수요가 확대되고 있다.", context_label="성장 동인"),
        ],
        regulatory_context=[
            "첨단 반도체 공급망과 수출 규제 이슈가 존재한다.",
            "국가 단위 반도체 육성 정책과 보조금이 시장 진입 기회로 작동할 수 있다.",
        ],
        references=_merge_references(market_live.evidence)
        or [
            "McKinsey - Global Semiconductor Industry Reports",
            "NASA - Technology Readiness Level Framework",
            "Bessemer Venture Partners - Startup Investment Checklist",
        ],
    )


@traced('build_company_research')
def build_company_research(company: CompanyProfile) -> CompanyResearch:
    kb = get_knowledge_base()
    company_live = _search_company(company)
    design_docs = kb.search(f"{company.stage} startup evaluation technology market risk") if kb else []
    company_kb = get_company_evidence_kb(company)
    live_docs = (
        company_kb.search(
            f"{company.name} architecture customers founders funding patents moat",
            limit=8,
        )
        if company_kb
        else []
    )
    context = format_docs(design_docs) if design_docs else ""
    live_context = format_docs(live_docs) if live_docs else ""
    combined_context = "\n".join(part for part in [context, live_context] if part)
    if combined_context and llm_client.available:
        prompt = (
            f"{COMPANY_RESEARCH_PROMPT}\n\n"
            f"Company name: {company.name}\n"
            f"Stage: {company.stage}\n"
            f"Industry: {company.industry}\n"
            f"Business model: {company.business_model}\n"
            f"Product summary: {company.product_summary}\n"
            f"Customer focus: {company.customer_focus}\n"
            f"Moat: {company.moat}\n"
            f"Known risks: {', '.join(company.risks)}\n"
            f"Reference context:\n{combined_context}\n"
            "Return structured Korean output."
        )
        enriched = llm_client.invoke_structured(prompt, CompanyResearchLLMOutput)
        if enriched:
            return CompanyResearch(
                company_name=company.name,
                stage=company.stage,
                company_overview=enriched.company_overview,
                business_model_status=enriched.business_model_status,
                product_and_technology=enriched.product_and_technology,
                traction_summary=enriched.traction_summary,
                team_summary=enriched.team_summary,
                competition_summary=enriched.competition_summary,
                risk_summary=enriched.risk_summary,
                references=company.references,
            )

    by_category: Dict[str, List[ResearchEvidence]] = {}
    for item in company_live.evidence:
        by_category.setdefault(item.category, []).append(item)

    return CompanyResearch(
        company_name=company.name,
        stage=company.stage,
        company_overview=(
            _pick_summary(
                by_category.get("Technology & Product", []) + by_category.get("Market Attractiveness", []),
                f"{company.name}는 {company.product_summary or 'AI semiconductor 제품'}을 중심으로 {company.customer_focus or '목표 고객'}을 겨냥하는 {company.industry} 스타트업이다.",
                context_label="기업 개요",
                company_name=company.name,
            )
        ),
        business_model_status=_pick_summary(
            by_category.get("Traction & Commercialization", []),
            company.business_model,
            context_label="사업화 현황",
            company_name=company.name,
        ),
        product_and_technology=_pick_summary(
            by_category.get("Technology & Product", []),
            company.technology_summary,
            context_label="기술 및 제품",
            company_name=company.name,
        ),
        traction_summary=_pick_summary(
            by_category.get("Traction & Commercialization", []),
            company.traction_summary,
            context_label="트랙션",
            company_name=company.name,
        ),
        team_summary=_pick_summary(
            by_category.get("Team & Founders", []),
            company.team_summary,
            context_label="팀 역량",
            company_name=company.name,
        ),
        competition_summary=_pick_summary(
            by_category.get("Competitive Advantage", []),
            company.competition_summary,
            context_label="경쟁 우위",
            company_name=company.name,
        ),
        risk_summary=_pick_summary(
            by_category.get("Execution & Financing Risk", []),
            company.risk_summary,
            context_label="리스크",
            company_name=company.name,
        ),
        references=_merge_references(company_live.evidence) or company.references,
    )


@traced('make_evaluation')
def make_evaluation(
    *,
    category: str,
    signal: int,
    weight: float,
    summary: str,
    evidence: List[str],
    follow_up_questions: List[str],
) -> EvaluationResult:
    normalized_summary = _rewrite_as_korean_note(
        text=summary,
        fallback=summary,
        context_label=category,
    )
    normalized_evidence = _normalize_evidence_items(evidence, category=category)
    kb = get_knowledge_base()
    context_docs = kb.search(f"{category} evaluation score methodology") if kb else []
    context = format_docs(context_docs) if context_docs else ""
    if context and llm_client.available:
        prompt = (
            f"{EVALUATION_PROMPT}\n\n"
            f"Category: {category}\n"
            f"Signal score: {signal}/5\n"
            f"Weight: {weight}\n"
            f"Initial summary: {normalized_summary}\n"
            f"Evidence candidates: {', '.join(normalized_evidence)}\n"
            f"Reference context:\n{context}\n"
            "Return structured Korean output."
        )
        enriched = llm_client.invoke_structured(prompt, EvaluationLLMOutput)
        if enriched:
            normalized_summary = enriched.summary
            normalized_evidence = enriched.evidence or normalized_evidence
            follow_up_questions = enriched.follow_up_questions or follow_up_questions

    if _needs_korean_rewrite(normalized_summary):
        normalized_summary = _evaluation_fallback_summary(category, signal)

    return EvaluationResult(
        category=category,
        score=signal,
        weighted_score=round(weighted_score(signal, weight), 1),
        summary=normalized_summary,
        evidence=normalized_evidence,
        follow_up_questions=follow_up_questions,
    )


def build_follow_up(signal: int, category: str) -> List[str]:
    if signal >= 4:
        return [f"{category} 관련 강점을 검증할 수 있는 실제 고객 또는 기술 증빙을 추가 확인한다."]
    if signal == 3:
        return [f"{category} 관련 불확실성을 해소할 추가 데이터와 인터뷰가 필요하다."]
    return [f"{category}는 핵심 리스크 영역이므로 정밀 실사 이전에 보강 증거 확보가 필요하다."]


def collect_references(company: CompanyProfile, market: MarketResearch) -> List[str]:
    live_company_refs = _merge_references(_search_company(company).evidence)
    merged = live_company_refs + company.references + market.references
    deduped: List[str] = []
    for item in merged:
        if item not in deduped:
            deduped.append(item)
    return deduped


@traced('enrich_company_profile')
def enrich_company_profile(company: CompanyProfile) -> CompanyProfile:
    web_research = _search_company(company)
    by_category: Dict[str, List[ResearchEvidence]] = {}
    for item in web_research.evidence:
        by_category.setdefault(item.category, []).append(item)

    return company.model_copy(
        update={
            "references": _merge_references(web_research.evidence) or company.references,
            "business_model": _pick_summary(
                by_category.get("Traction & Commercialization", []),
                company.business_model,
                context_label="사업 모델",
                company_name=company.name,
            ),
            "product_summary": _pick_summary(
                by_category.get("Technology & Product", []),
                company.product_summary or f"{company.name} AI semiconductor product",
                context_label="제품 개요",
                company_name=company.name,
            ),
            "customer_focus": _pick_summary(
                by_category.get("Market Attractiveness", []),
                company.customer_focus,
                context_label="고객 및 시장",
                company_name=company.name,
            ),
            "moat": _pick_summary(
                by_category.get("Competitive Advantage", []),
                company.moat,
                context_label="차별화 요소",
                company_name=company.name,
            ),
            "team_signal": _score_from_evidence(
                by_category.get("Team & Founders", []),
                company.team_signal,
            ),
            "market_signal": _score_from_evidence(
                by_category.get("Market Attractiveness", []),
                company.market_signal,
            ),
            "technology_signal": _score_from_evidence(
                by_category.get("Technology & Product", []),
                company.technology_signal,
            ),
            "traction_signal": _score_from_evidence(
                by_category.get("Traction & Commercialization", []),
                company.traction_signal,
            ),
            "competition_signal": _score_from_evidence(
                by_category.get("Competitive Advantage", []),
                company.competition_signal,
            ),
            "risk_signal": max(
                1,
                min(
                    5,
                    _score_from_evidence(
                        by_category.get("Execution & Financing Risk", []),
                        company.risk_signal,
                    ),
                ),
            ),
            "team_summary": _pick_summary(
                by_category.get("Team & Founders", []),
                company.team_summary,
                context_label="팀 요약",
                company_name=company.name,
            ),
            "market_summary": _pick_summary(
                by_category.get("Market Attractiveness", []),
                company.market_summary,
                context_label="시장성 요약",
                company_name=company.name,
            ),
            "technology_summary": _pick_summary(
                by_category.get("Technology & Product", []),
                company.technology_summary,
                context_label="기술 요약",
                company_name=company.name,
            ),
            "traction_summary": _pick_summary(
                by_category.get("Traction & Commercialization", []),
                company.traction_summary,
                context_label="트랙션 요약",
                company_name=company.name,
            ),
            "competition_summary": _pick_summary(
                by_category.get("Competitive Advantage", []),
                company.competition_summary,
                context_label="경쟁력 요약",
                company_name=company.name,
            ),
            "risk_summary": _pick_summary(
                by_category.get("Execution & Financing Risk", []),
                company.risk_summary,
                context_label="리스크 요약",
                company_name=company.name,
            ),
        }
    )


def comparison_row(decision_map: Dict[str, str], key: str) -> str:
    return decision_map.get(key, "-")
