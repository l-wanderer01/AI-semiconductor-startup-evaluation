"""#25 필수 항목의 의미 검사 제안과 원문 추적 결과."""
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .models import Identifier, RecordModel, RequiredInformationAssessment, Sha256, SourceLocation


class ItemProposal(RecordModel):
    claim_ids: list[Identifier]
    reference_ids: list[Identifier]
    quote: str
    mentioned: bool
    accurate: bool
    explanation_complete: bool
    time_satisfied: bool
    evidence_satisfied: bool
    uncertainty_explicit: bool
    no_invented_value: bool
    reason: Identifier


class CoverageAssessment(RequiredInformationAssessment):
    verdict: Literal['fulfilled', 'missing', 'incorrect', 'not_applicable'] | None = None
    case_id: Identifier
    company_id: Identifier | None
    section_id: Identifier
    stage: Identifier
    as_of_date: Identifier
    item_version: Identifier
    dataset_sha256: Sha256
    policy_version: Identifier
    reference_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    source_locations: list[SourceLocation] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    applicable: bool
    exclusion_reason: str | None = None
    policy_failure_reasons: list[str] = Field(default_factory=list)
    proposal: ItemProposal | None = None
    raw_output: JsonValue = None
    human_review_status: Literal['pending'] = 'pending'

    @model_validator(mode='after')
    def fixed_applicability(self):
        if self.verdict == 'not_applicable' and (self.applicable or not self.exclusion_reason):
            raise ValueError('고정 제외 여부와 제외 사유가 있어야 not_applicable을 저장합니다.')
        if not self.applicable and self.verdict != 'not_applicable':
            raise ValueError('고정 제외 항목은 not_applicable로 저장합니다.')
        if self.verdict == 'fulfilled' and self.policy_failure_reasons:
            raise ValueError('정책 실패가 있는 항목은 fulfilled일 수 없습니다.')
        return self
