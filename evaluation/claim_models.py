"""#22 사용자 정의 추출과 단계 sample 계약. RAGAS 내부 claim과 별개다."""
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .models import Count, EvaluationSample, Identifier, RecordModel, Sha256, SourceLocation


class TextUnit(RecordModel):
    unit_id: Identifier
    text: str
    location: SourceLocation
    company_id: Identifier | None = None
    section_id: Identifier
    context: str = ''
    json_pointer: str | None = None
    json_fragment: bool = False
    format: Literal['markdown', 'json_string']
    exclusion_reason: str | None = None


class AtomicCandidate(RecordModel):
    """quote는 출력 원문에 실제 있는 구간이고 statement는 그 구간의 독립 주장이다."""
    statement: Identifier
    quote: Identifier
    start: Count
    end: Count
    company_id: Identifier | None
    section_id: Identifier
    kind: Literal['fact', 'opinion', 'score', 'decision']
    category: Literal['stage', 'funding', 'revenue', 'customers', 'benchmark', 'commercial_status', 'other']
    relation_key: Identifier
    numbers: list[str]
    units: list[str]
    time_conditions: list[str]
    comparison_conditions: list[str]
    critical: bool
    citation_markers: list[str]

    @model_validator(mode='after')
    def offsets(self):
        if self.end <= self.start:
            raise ValueError('claim quote must be nonempty')
        return self


class ExtractedAtoms(RecordModel):
    atoms: list[AtomicCandidate]
    no_claim_reason: str | None = None

    @model_validator(mode='after')
    def empty_reason(self):
        if not self.atoms and not (self.no_claim_reason or '').strip():
            raise ValueError('빈 추출에는 사유가 필요합니다.')
        return self


class AtomAudit(RecordModel):
    unit_id: Identifier
    sample_id: Identifier | None = None
    candidate: AtomicCandidate
    location: SourceLocation
    claim_id: Identifier | None = None
    citation_source_ids: list[Identifier] = Field(default_factory=list)
    unresolved_citations: list[str] = Field(default_factory=list)


class ExtractionAudit(RecordModel):
    unit: TextUnit
    extractor_version: Identifier
    prompt_sha256: Sha256
    status: Literal['completed', 'error', 'not_applicable']
    raw_output: JsonValue = None
    error: str | None = None
    excluded_reason: str | None = None


class StageSample(EvaluationSample):
    logical_role: Identifier
    role_mapping_version: Identifier
    source_locations: list[SourceLocation] = Field(default_factory=list)
    raw_response: str | None = None
    claim_ids: list[Identifier] = Field(default_factory=list)
    excluded_atom_count: Count = 0
    upstream_sample_ids: list[Identifier] = Field(default_factory=list)
    previous_sample_id: Identifier | None = None
    reference_reason: str | None = None
    transformation_version: Identifier
    source_sha256: Sha256 | None = None
    rubric_targets: list[str] = Field(default_factory=list)


class ClaimLink(RecordModel):
    upstream_claim_id: Identifier
    downstream_claim_id: Identifier
    relation: Literal['repeated', 'changed', 'unknown']
    basis: Literal['actual_delivered_context', 'reevaluation', 'report_artifact']
    candidate: Literal['inherited', 'new', 'corrected', 'unknown']
    reason: Identifier
    certainty: Literal['candidate_only'] = 'candidate_only'
    human_review_status: Literal['pending', 'confirmed', 'rejected'] = 'pending'
    human_review_id: Identifier | None = None


class CoverageEntry(RecordModel):
    logical_role: Identifier
    company_id: Identifier | None = None
    invocation_ids: list[Identifier]
    sample_ids: list[Identifier]
    status: Literal['covered', 'missing', 'error', 'not_applicable', 'unsupported']
    reason: Identifier


class IsolatedContract(RecordModel):
    """단독 실행 run의 고정 입력 동일성. 기존 전체 실행의 mode만 바꾸는 것을 금지한다."""
    input_sha256: Sha256
    evidence_sha256: Sha256
    reference_sha256: Sha256
    evaluator_configuration_sha256: Sha256
    role_mapping_version: Identifier
    dataset_version: Identifier
    target_invocation_ids: list[Identifier] = Field(min_length=1)
