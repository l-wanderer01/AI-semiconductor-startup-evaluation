"""작성 기록(Codex): #23 독립 기준 판정 계약. RAGAS 내부 판정과 구별한다."""
from typing import Literal

from pydantic import Field, model_validator

from .models import FactAssessment, Identifier, RecordModel, Sha256, SourceLocation


class FactComparison(RecordModel):
    fact_id: Identifier
    relation: Literal['supports', 'contradicts', 'unrelated', 'insufficient']
    same_entity: bool
    same_attribute: bool
    same_event: bool
    same_time: bool
    time_mismatch_kind: Literal['none', 'different_observation_period', 'same_event_date_error'] = 'none'
    same_conditions: bool
    claim_quantity_quote: str | None = None
    reference_quantity_quote: str | None = None
    reason: Identifier


class JudgmentProposal(RecordModel):
    comparisons: list[FactComparison]
    reason: Identifier


class FactualAssessment(FactAssessment):
    sample_id: Identifier
    case_id: Identifier
    company_id: Identifier | None
    section_id: Identifier
    critical: bool
    policy_version: Identifier
    evaluator_version: Identifier
    dataset_sha256: Sha256
    as_of_date: Identifier
    source_locations: list[SourceLocation] = Field(default_factory=list)
    comparisons: list[FactComparison] = Field(default_factory=list)
    conflict_fact_ids: list[Identifier] = Field(default_factory=list)
    excluded_fact_ids: list[Identifier] = Field(default_factory=list)
    human_review_status: Literal['pending'] = 'pending'
    raw_output: dict | list | str | int | float | bool | None = None

    @model_validator(mode='after')
    def proof_required(self):
        if self.verdict in {'verified', 'contradicted'} and (not self.reference_ids or not self.source_locations):
            raise ValueError('verified/contradicted 판정에는 독립 기준 ID와 원문 구간이 필요합니다.')
        return self
