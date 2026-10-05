from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from langgraph.graph import END, START, StateGraph

from evaluation.recording import traced, company_scope

from .config import settings
from .models import CompanyProfile, InvestmentDecision, PipelineResult, RankingSelection
from .reporting import render_hold_report, render_top_report
from .scoring import STAGE_WEIGHTS, to_recommendation
from .services import (
    build_company_research,
    build_follow_up,
    build_market_research,
    collect_references,
    enrich_company_profile,
    make_evaluation,
)
from .states import CompanyAnalysisState, PipelineState


def _company_graph():
    graph = StateGraph(CompanyAnalysisState)

    @traced('investment_supervisor', kind='node')
    def investment_supervisor_node(state: CompanyAnalysisState) -> Dict[str, object]:
        route = "decision"
        if "company_research_state" not in state:
            route = "company_research"
        elif "technical_evaluation_state" not in state:
            route = "technical_eval"
        elif (
            state["technical_evaluation_state"].score < 4
            and "technical_additional_research_state" not in state
            and not state.get("technical_recheck_completed_state", False)
        ):
            route = "technical_additional_research"
        elif (
            "technical_additional_research_state" in state
            and not state.get("technical_recheck_completed_state", False)
        ):
            route = "technical_eval"
        elif "market_evaluation_state" not in state:
            route = "market_eval"
        elif (
            state["market_evaluation_state"].score < 4
            and "market_additional_research_state" not in state
            and not state.get("market_recheck_completed_state", False)
        ):
            route = "market_additional_research"
        elif (
            "market_additional_research_state" in state
            and not state.get("market_recheck_completed_state", False)
        ):
            route = "market_eval"
        elif "team_evaluation_state" not in state:
            route = "team_eval"
        elif "risk_analysis_state" not in state:
            route = "risk_eval"
        elif "competition_evaluation_state" not in state:
            route = "competition_eval"

        return {"supervisor_route_state": route}

    @traced('company_research', kind='node')
    def company_research_node(state: CompanyAnalysisState) -> Dict[str, object]:
        company = state["selected_company_context_state"]
        return {"company_research_state": build_company_research(company)}

    @traced('technical_eval', kind='node')
    def technical_eval_node(state: CompanyAnalysisState) -> Dict[str, object]:
        company = state["selected_company_context_state"]
        research = state["company_research_state"]
        weight = STAGE_WEIGHTS[company.stage]["technology"]
        additional_notes = state.get("technical_additional_research_state", [])
        summary = research.product_and_technology or company.technology_summary
        evidence = [research.product_and_technology or company.product_summary, company.moat]
        if additional_notes:
            summary = f"{summary} 추가 확인 사항: {' '.join(additional_notes)}"
            evidence = evidence + additional_notes
        return {
            "technical_evaluation_state": make_evaluation(
                category="Technology & Product",
                signal=company.technology_signal,
                weight=weight,
                summary=summary,
                evidence=evidence,
                follow_up_questions=build_follow_up(company.technology_signal, "기술력"),
            ),
            "technical_recheck_completed_state": bool(additional_notes),
        }

    @traced('technical_additional_research', kind='node')
    def technical_additional_research_node(state: CompanyAnalysisState) -> Dict[str, object]:
        tech = state["technical_evaluation_state"]
        notes = [] if tech.score >= 4 else ["특허, 벤치마크, 프로토타입 검증 자료를 추가 확보한다."]
        return {"technical_additional_research_state": notes}

    @traced('market_eval', kind='node')
    def market_eval_node(state: CompanyAnalysisState) -> Dict[str, object]:
        company = state["selected_company_context_state"]
        research = state["company_research_state"]
        weight = STAGE_WEIGHTS[company.stage]["market"] + STAGE_WEIGHTS[company.stage]["traction"]
        additional_notes = state.get("market_additional_research_state", [])
        summary = f"{research.business_model_status} {research.traction_summary}".strip()
        if additional_notes:
            summary = f"{summary} 추가 확인 사항: {' '.join(additional_notes)}"
        return {
            "market_evaluation_state": make_evaluation(
                category="Market & Traction",
                signal=round((company.market_signal + company.traction_signal) / 2),
                weight=weight,
                summary=summary,
                evidence=[company.customer_focus, research.traction_summary] + additional_notes,
                follow_up_questions=build_follow_up(company.market_signal, "시장성"),
            ),
            "market_recheck_completed_state": bool(additional_notes),
        }

    @traced('market_additional_research', kind='node')
    def market_additional_research_node(state: CompanyAnalysisState) -> Dict[str, object]:
        market = state["market_evaluation_state"]
        notes = [] if market.score >= 4 else ["고객 세그먼트, PoC 전환율, 파운드리 파트너 현황을 추가 조사한다."]
        return {"market_additional_research_state": notes}

    @traced('team_eval', kind='node')
    def team_eval_node(state: CompanyAnalysisState) -> Dict[str, object]:
        company = state["selected_company_context_state"]
        research = state["company_research_state"]
        weight = STAGE_WEIGHTS[company.stage]["team"]
        return {
            "team_evaluation_state": make_evaluation(
                category="Team & Founders",
                signal=company.team_signal,
                weight=weight,
                summary=research.team_summary or company.team_summary,
                evidence=company.tags[:2] or [research.team_summary or company.team_summary],
                follow_up_questions=build_follow_up(company.team_signal, "팀 역량"),
            )
        }

    @traced('risk_eval', kind='node')
    def risk_eval_node(state: CompanyAnalysisState) -> Dict[str, object]:
        company = state["selected_company_context_state"]
        research = state["company_research_state"]
        weight = STAGE_WEIGHTS[company.stage]["risk"]
        risk_score = max(1, min(5, 6 - company.risk_signal))
        return {
            "risk_analysis_state": make_evaluation(
                category="Execution & Financing Risk",
                signal=risk_score,
                weight=weight,
                summary=research.risk_summary or company.risk_summary,
                evidence=company.risks,
                follow_up_questions=build_follow_up(risk_score, "리스크"),
            )
        }

    @traced('competition_eval', kind='node')
    def competition_eval_node(state: CompanyAnalysisState) -> Dict[str, object]:
        company = state["selected_company_context_state"]
        research = state["company_research_state"]
        weight = STAGE_WEIGHTS[company.stage]["competition"]
        return {
            "competition_evaluation_state": make_evaluation(
                category="Competitive Advantage",
                signal=company.competition_signal,
                weight=weight,
                summary=research.competition_summary or company.competition_summary,
                evidence=[research.competition_summary or company.moat],
                follow_up_questions=build_follow_up(company.competition_signal, "경쟁 우위"),
            )
        }

    @traced('decision', kind='node')
    def decision_node(state: CompanyAnalysisState) -> Dict[str, object]:
        company = state["selected_company_context_state"]
        research = state["company_research_state"]
        technical = state["technical_evaluation_state"]
        market = state["market_evaluation_state"]
        team = state["team_evaluation_state"]
        risk = state["risk_analysis_state"]
        competition = state["competition_evaluation_state"]
        final_score = round(
            technical.weighted_score
            + market.weighted_score
            + team.weighted_score
            + risk.weighted_score
            + competition.weighted_score
        )
        recommendation = to_recommendation(final_score)
        strengths = [
            item
            for item in [technical.summary, market.summary, team.summary]
            if item
        ][:3]
        weaknesses = [risk.summary] + company.risks[:2]
        summary = (
            research.company_overview
            or (
                f"{company.name}는 {company.stage} 단계에서 DD-Worthiness Score {final_score}점을 기록했으며, "
                f"{recommendation} 의견을 받았다."
            )
        )
        if any(token in summary for token in [" provides ", "receiving a ", " and is currently "]):
            summary = (
                f"{company.name}는 {company.stage} 단계에서 DD-Worthiness Score {final_score}점을 기록했으며, "
                f"{recommendation} 의견을 받은 투자 검토 대상이다."
            )
        return {
            "investment_decision_state": InvestmentDecision(
                company_name=company.name,
                stage=company.stage,
                final_score=final_score,
                recommendation=recommendation,
                strengths=strengths,
                weaknesses=weaknesses,
                summary=summary,
                technical_evaluation=technical,
                market_evaluation=market,
                team_evaluation=team,
                competition_evaluation=competition,
                risk_analysis=risk,
                references=collect_references(company, state["domain_market_research_state"]),
            )
        }

    @traced('company_route')
    def company_route(state: CompanyAnalysisState) -> str:
        return state["supervisor_route_state"]

    graph.add_node("investment_supervisor", investment_supervisor_node)
    graph.add_node("company_research", company_research_node)
    graph.add_node("technical_eval", technical_eval_node)
    graph.add_node("technical_additional_research", technical_additional_research_node)
    graph.add_node("market_eval", market_eval_node)
    graph.add_node("market_additional_research", market_additional_research_node)
    graph.add_node("team_eval", team_eval_node)
    graph.add_node("risk_eval", risk_eval_node)
    graph.add_node("competition_eval", competition_eval_node)
    graph.add_node("decision", decision_node)

    graph.add_edge(START, "investment_supervisor")
    graph.add_conditional_edges(
        "investment_supervisor",
        company_route,
        {
            "company_research": "company_research",
            "technical_eval": "technical_eval",
            "technical_additional_research": "technical_additional_research",
            "market_eval": "market_eval",
            "market_additional_research": "market_additional_research",
            "team_eval": "team_eval",
            "risk_eval": "risk_eval",
            "competition_eval": "competition_eval",
            "decision": "decision",
        },
    )
    graph.add_edge("company_research", "investment_supervisor")
    graph.add_edge("technical_eval", "investment_supervisor")
    graph.add_edge("technical_additional_research", "investment_supervisor")
    graph.add_edge("market_eval", "investment_supervisor")
    graph.add_edge("market_additional_research", "investment_supervisor")
    graph.add_edge("team_eval", "investment_supervisor")
    graph.add_edge("risk_eval", "investment_supervisor")
    graph.add_edge("competition_eval", "investment_supervisor")
    graph.add_edge("decision", END)

    return graph.compile()


COMPANY_GRAPH = _company_graph()


def build_pipeline():
    graph = StateGraph(PipelineState)

    @traced('list_candidates', kind='node')
    def list_candidates_node(_: PipelineState) -> Dict[str, object]:
        return {
            "pipeline_meta_state": {
                "run_id": datetime.utcnow().isoformat(),
                "status": "running",
                "version": "1.0.0",
            }
        }

    @traced('market_research', kind='node')
    def market_research_node(state: PipelineState) -> Dict[str, object]:
        return {
            "domain_market_research_state": build_market_research(state["domain_definition_state"])
        }

    @traced('analyze_companies', kind='node')
    def analyze_companies_node(state: PipelineState) -> Dict[str, object]:
        decisions: List[InvestmentDecision] = []
        market = state["domain_market_research_state"]
        for company in state["candidate_company_pool_state"]:
            with company_scope(company.name):
                enriched_company = enrich_company_profile(company)
                result = COMPANY_GRAPH.invoke(
                    {
                        "selected_company_context_state": enriched_company,
                        "domain_market_research_state": market,
                    }
                )
                decisions.append(result["investment_decision_state"])
        return {"investment_decision_state": decisions}

    @traced('ranking', kind='node')
    def ranking_node(state: PipelineState) -> Dict[str, object]:
        decisions = sorted(
            state["investment_decision_state"], key=lambda item: item.final_score, reverse=True
        )
        passed = [d.company_name for d in decisions if d.final_score >= settings.selective_dd_threshold]
        top_companies = passed[:3]
        branch = "top3" if top_companies else "hold"
        watchlist = [d.company_name for d in decisions if d.recommendation == "Watchlist"]
        ranking = RankingSelection(
            branch=branch,
            passed_companies=passed,
            top_companies=top_companies,
            score_threshold=settings.selective_dd_threshold,
            high_priority_threshold=settings.high_priority_threshold,
            watchlist_companies=watchlist,
        )
        return {"ranking_selection_state": ranking}

    @traced('branch_selector')
    def branch_selector(state: PipelineState) -> str:
        return state["ranking_selection_state"].branch

    @traced('top_report', kind='node')
    def top_report_node(state: PipelineState) -> Dict[str, object]:
        report = render_top_report(
            state["ranking_selection_state"],
            state["investment_decision_state"],
            state["domain_market_research_state"],
        )
        return {"final_report_markdown": report}

    @traced('hold_report', kind='node')
    def hold_report_node(state: PipelineState) -> Dict[str, object]:
        report = render_hold_report(
            state["ranking_selection_state"],
            state["investment_decision_state"],
            state["domain_market_research_state"],
        )
        return {"final_report_markdown": report}

    graph.add_node("list_candidates", list_candidates_node)
    graph.add_node("market_research", market_research_node)
    graph.add_node("analyze_companies", analyze_companies_node)
    graph.add_node("ranking", ranking_node)
    graph.add_node("top_report", top_report_node)
    graph.add_node("hold_report", hold_report_node)

    graph.add_edge(START, "list_candidates")
    graph.add_edge("list_candidates", "market_research")
    graph.add_edge("market_research", "analyze_companies")
    graph.add_edge("analyze_companies", "ranking")
    graph.add_conditional_edges(
        "ranking",
        branch_selector,
        {"top3": "top_report", "hold": "hold_report"},
    )
    graph.add_edge("top_report", END)
    graph.add_edge("hold_report", END)
    return graph.compile()


PIPELINE_GRAPH = build_pipeline()


@traced('run_pipeline')
def run_pipeline(domain: str, companies: List[CompanyProfile]) -> PipelineResult:
    result = PIPELINE_GRAPH.invoke(
        {
            "domain_definition_state": domain,
            "candidate_company_pool_state": companies,
        }
    )
    return PipelineResult(
        report_markdown=result["final_report_markdown"],
        branch=result["ranking_selection_state"].branch,
        ranking=result["ranking_selection_state"],
        decisions=result["investment_decision_state"],
        market_research=result["domain_market_research_state"],
    )
