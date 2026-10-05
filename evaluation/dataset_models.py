"""#21 데이터셋의 출처·적용 범위·사람 검토 계약. 기존 #20 모델을 확장한다."""
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .models import (
    DatasetManifest, Identifier, RecordModel, ReferenceFact, RequiredInformationItem,
    Sha256, SnapshotRef, SourceLocation, UtcDatetime,
)


class DatasetSource(RecordModel):
    source_id: Identifier
    company_ids: list[Identifier] = Field(min_length=1)
    title: Identifier
    url: Identifier | None = None
    published_date: date
    collected_at: UtcDatetime
    snapshot: SnapshotRef
    snapshot_kind: Literal['original_excerpt', 'synthetic_fixture']
    provenance: Literal['primary_source', 'synthetic_fixture']
    original_locator: Identifier


class DatasetFact(ReferenceFact):
    category: Literal['stage', 'funding', 'revenue', 'customers', 'benchmark', 'commercial_status', 'market']
    source_as_of_date: date
    review_kind: Literal['human', 'synthetic_fixture'] | None = None

    @model_validator(mode='after')
    def verified_review_kind(self):
        if self.review_status == 'verified' and self.review_kind is None:
            raise ValueError('검증된 사실에는 human 또는 synthetic_fixture 검토 종류가 필요합니다.')
        if self.category != 'market' and not self.critical:
            raise ValueError('투자 단계·조달·매출·고객·성능·상용 상태는 핵심 사실로 지정합니다.')
        return self


class DatasetRequiredItem(RequiredInformationItem):
    critical: bool = True
    applicability: Literal['applicable', 'not_applicable'] = 'applicable'
    not_applicable_reason: Identifier | None = None
    expected_unknown_response: Identifier

    @model_validator(mode='after')
    def excluded_reason(self):
        if self.applicability == 'not_applicable' and self.not_applicable_reason is None:
            raise ValueError('적용 제외 항목에는 사유가 필요합니다.')
        return self


class ExpectedAssertion(RecordModel):
    """판정기 회귀 검증용 예제. 실제 Agent 출력 또는 투자 정답이 아니다."""
    statement: Identifier
    expected_verdict: Literal['verified', 'contradicted', 'unverifiable']
    reason: Identifier
    fact_ids: list[Identifier] = Field(default_factory=list)


class CaseReview(RecordModel):
    case_id: Identifier
    scope_description: Identifier
    completeness: Literal['pending', 'complete', 'incomplete']
    reviewer: Identifier | None = None
    reviewed_at: UtcDatetime | None = None
    review_kind: Literal['human', 'synthetic_fixture'] | None = None
    relevant_source_ids: list[Identifier] = Field(default_factory=list)
    retrieval_applicability_reason: Identifier
    tags: list[Identifier] = Field(default_factory=list)
    expected_assertions: list[ExpectedAssertion] = Field(default_factory=list)
    expected_route: Literal['recommend', 'hold'] | None = None
    route_reason: Identifier | None = None

    @model_validator(mode='after')
    def review_metadata(self):
        if self.completeness != 'pending' and any(v is None for v in (self.reviewer, self.reviewed_at, self.review_kind)):
            raise ValueError('범위 검토 완료에는 검토자·시각·종류가 필요합니다.')
        if self.expected_route is not None and self.route_reason is None:
            raise ValueError('추천/보류 예제에는 정책과 근거 설명이 필요합니다.')
        return self


class FixedDataset(RecordModel):
    schema_version: Literal['0.2.0'] = '0.2.0'
    purpose: Literal['baseline', 'synthetic_fixture']
    status: Literal['draft', 'released']
    manifest: DatasetManifest
    sources: list[DatasetSource] = Field(min_length=1)
    facts: list[DatasetFact]
    required_items: list[DatasetRequiredItem]
    case_reviews: list[CaseReview]
    inventory: dict[str, Sha256]


class RunCaseBinding(RecordModel):
    """기록된 호출을 case에 수동 매핑한다. 응답은 원본 파일 구간에서만 읽는다."""
    case_id: Identifier
    company_id: Identifier
    section_id: Identifier
    as_of_date: date
    generation_invocation_id: Identifier
    response_location: SourceLocation
    retrieval_invocation_id: Identifier | None = None
