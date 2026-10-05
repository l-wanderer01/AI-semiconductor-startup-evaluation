"""작성 기록(Codex): #24 실제 문맥 지지와 인용 지지를 사실 정확도와 구분한다."""
from typing import Literal
from datetime import date

from pydantic import Field, model_validator

from .models import (CitationAssessment, GroundingAssessment, Identifier, JsonValue,
                     RecordModel, Sha256, SnapshotRef)


class SupportDocument(RecordModel):
    document_id: Identifier
    kind: Literal['source_evidence', 'delivered_context', 'received_input']
    text: str
    sha256: Sha256
    source_id: Identifier | None = None
    evidence_ids: list[Identifier] = Field(default_factory=list)
    company_ids: list[Identifier] = Field(default_factory=list)
    context_kind: str | None = None
    as_of_date: date | None = None
    published_date: date | None = None
    original_snapshot: SnapshotRef | None = None
    snapshot: SnapshotRef | None = None


class SupportComparison(RecordModel):
    document_id: Identifier
    relation: Literal['supported', 'unsupported', 'contradicted']
    same_entity: bool
    same_time: bool
    same_conditions: bool
    quote: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    claim_quantity_quote: str | None = None
    document_quantity_quote: str | None = None
    reason: Identifier


class SupportProposal(RecordModel):
    comparisons: list[SupportComparison]
    reason: Identifier


class GroundingResult(GroundingAssessment):
    sample_id: Identifier
    case_id: Identifier
    as_of_date: date
    evaluator_version: Identifier
    policy_version: Identifier
    supporting_document_ids: list[Identifier] = Field(default_factory=list)
    comparisons: list[SupportComparison] = Field(default_factory=list)
    effective_relations: dict[str, str] = Field(default_factory=dict)
    conflict_document_ids: list[Identifier] = Field(default_factory=list)
    raw_output: JsonValue = None
    human_review_status: Literal['pending'] = 'pending'


class CitationResult(CitationAssessment):
    sample_id: Identifier
    case_id: Identifier
    as_of_date: date
    markers: list[str]
    binding: Literal['explicit', 'adjacent', 'ambiguous']
    evaluator_version: Identifier
    policy_version: Identifier
    supporting_evidence_ids: list[Identifier] = Field(default_factory=list)
    comparisons: list[SupportComparison] = Field(default_factory=list)
    effective_relations: dict[str, str] = Field(default_factory=dict)
    conflict_document_ids: list[Identifier] = Field(default_factory=list)
    raw_output: JsonValue = None
    human_review_status: Literal['pending'] = 'pending'

    @model_validator(mode='after')
    def support_proof(self):
        if self.verdict == 'supported' and not self.supporting_evidence_ids:
            raise ValueError('인용 지지에는 실제 원문 evidence ID가 필요합니다.')
        return self
