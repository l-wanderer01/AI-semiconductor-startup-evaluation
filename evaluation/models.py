"""평가 계약에 따른 사실·필수 정보·규칙 검사 결과 모델.

작성 기록: Codex가 제공한 뼈대를 바탕으로 l-wanderer01이 상태·판정 목록,
평가 필드와 검증 조건을 직접 작성하고 리뷰를 거쳐 수정했다.
규칙 검사 모델은 Codex가 반복되는 필드·검증 패턴을 작성하고,
l-wanderer01이 JsonValue 필드와 저장용 status 계산을 직접 완성했다.
RAGAS 지표 모델은 Codex가 반복 패턴을 작성하고,
l-wanderer01이 점수 범위와 완료 후 값 없음 처리 조건을 직접 완성했다.
이하 실행·호출·자료·데이터셋·집계·비교 모델은 Codex가 계약에 따라 작성했다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, JsonValue, StringConstraints,
    computed_field, model_validator,
)


class EvaluationStatus(str, Enum):
    """평가 진행 상태. str을 상속해 JSON에 문자열로 저장할 수 있습니다."""

    # 파이썬에서 사용할 이름 = 로그에 저장할 문자열
    PENDING = "pending"
    # 직접 작성(l-wanderer01): 계약에 정의된 나머지 평가 진행 상태.
    COMPLETED = "completed"
    MISSING = "missing"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"


class FactVerdict(str, Enum):
    """평가가 완료된 주장에 대한 내용 판정."""

    VERIFIED = "verified"
    # 직접 작성(l-wanderer01): 모순·검증 불가 판정. 평가 오류와 구분한다.
    CONTRADICTED = "contradicted"
    UNVERIFIABLE = "unverifiable"


class FactAssessment(BaseModel):
    """주장 하나의 평가 기록. BaseModel이 필드 타입을 검증합니다."""

    # 필드명: 타입 = 제약 또는 기본값
    # 기본값이 없으므로 evaluation_id는 필수이며 빈 문자열을 허용하지 않습니다.
    evaluation_id: str = Field(min_length=1)
    evaluation_status: EvaluationStatus

    # 직접 작성(l-wanderer01): 평가 대상 주장 ID는 필수이며 빈 문자열을 거부한다.
    claim_id: str = Field(min_length=1)

    # 직접 작성(l-wanderer01): 미완료 평가를 표현하도록 판정에 None을 허용한다.
    verdict: FactVerdict | None = None

    # 직접 작성(l-wanderer01): 문자열 ID 목록이며 모델마다 새로운 빈 목록을 만든다.
    reference_ids: list[str] = Field(default_factory=list)

    # 직접 작성(l-wanderer01): 사유의 필수 여부는 아래에서 상태에 따라 검증한다.
    reason: str | None = None

    @model_validator(mode="after")
    def validate_assessment(self) -> FactAssessment:
        """필드 타입 검증 후, 여러 필드 사이의 관계를 검증합니다."""

        # 직접 작성(l-wanderer01): 완료된 평가에는 반드시 판정이 있어야 한다.
        if self.evaluation_status == EvaluationStatus.COMPLETED and self.verdict is None:
            raise ValueError("평가 완료 시 verdict가 필요합니다.")

        # 직접 작성(l-wanderer01): 미완료 평가에 내용 판정이 섞이지 않도록 한다.
        if self.evaluation_status != EvaluationStatus.COMPLETED and self.verdict is not None:
            raise ValueError("미완료 평가에는 verdict를 기록할 수 없습니다.")

        # 직접 작성(l-wanderer01): 입력 누락·오류·적용 제외에는 사유가 필요하다.
        # None, 빈 문자열, 공백뿐인 문자열을 모두 빈 사유로 처리한다.
        reason_required_statuses = {
            EvaluationStatus.MISSING,
            EvaluationStatus.ERROR,
            EvaluationStatus.NOT_APPLICABLE
        }
        if self.evaluation_status in reason_required_statuses and not (self.reason or "").strip():
            raise ValueError("입력 누락·오류·적용 제외에는 사유가 필요합니다.")

        # 직접 작성(l-wanderer01): 검증을 통과한 모델 객체를 반환한다.
        return self


class RequiredInformationVerdict(str, Enum):
    """검사를 완료한 필수 정보 항목의 내용 판정."""

    # 예시: 정확성·시점·근거 조건까지 충족한 경우.
    FULFILLED = "fulfilled"
    # 직접 작성(l-wanderer01): 필수 내용 누락과 잘못된 정보를 구분한다.
    MISSING = "missing"      # 검사를 완료했지만 필수 정보가 없음.
    INCORRECT = "incorrect"    # 정보는 있지만 값이나 조건이 잘못됨.
    # not_applicable은 EvaluationStatus로 표현하므로 여기에 추가하지 않습니다.


class RequiredInformationAssessment(BaseModel):
    """필수 항목 하나의 평가 기록. 실제 정보 검색·판정 기능은 아닙니다."""

    # 예시: 품질 평가 ID는 필수이며 빈 문자열을 허용하지 않습니다.
    evaluation_id: str = Field(min_length=1)

    # 직접 작성(l-wanderer01): 기업·영역별 평가 sample에 연결한다.
    sample_id: str = Field(min_length=1)

    # 직접 작성(l-wanderer01): 평가 기준의 필수 항목 정의에 연결한다.
    required_item_id: str = Field(min_length=1)

    # 직접 작성(l-wanderer01): 공통 평가 진행 상태를 재사용한다.
    evaluation_status: EvaluationStatus

    # 직접 작성(l-wanderer01): 필수 정보 판정과 미완료 상태를 표현한다.
    verdict: RequiredInformationVerdict | None = None

    # 직접 작성(l-wanderer01): 판정과 연결된 주장 ID를 목록으로 보존한다.
    # 누락 판정에는 연결할 주장이 없을 수 있으므로 빈 목록을 허용합니다.
    claim_ids: list[str] = Field(default_factory=list)

    # 직접 작성(l-wanderer01): 사유의 필수 여부는 평가 상태에 따라 검증한다.
    reason: str | None = None

    @model_validator(mode="after")
    def validate_assessment(self) -> RequiredInformationAssessment:
        """평가 진행 상태와 필수 정보 판정·사유의 관계를 검증한다."""

        # 직접 작성(l-wanderer01): 완료된 평가에는 판정이 필요하다.
        if self.evaluation_status == EvaluationStatus.COMPLETED and self.verdict is None:
            raise ValueError("필수 정보 평가 완료 시 verdict가 필요합니다.")

        # 직접 작성(l-wanderer01): 미완료 상태에는 내용 판정을 기록하지 않는다.
        if self.evaluation_status != EvaluationStatus.COMPLETED and self.verdict is not None:
            raise ValueError("미완료 평가에는 필수 정보 판정을 기록할 수 없습니다.")

        # 직접 작성(l-wanderer01): 입력 누락·오류·적용 제외에는 사유가 필요하다.
        reason_required_statuses = {
            EvaluationStatus.MISSING,
            EvaluationStatus.ERROR,
            EvaluationStatus.NOT_APPLICABLE,
        }

        # 직접 작성(l-wanderer01): None·빈 문자열·공백뿐인 사유를 거부한다.
        if self.evaluation_status in reason_required_statuses and not (self.reason or "").strip():
            raise ValueError("입력 누락·오류·적용 제외에는 사유가 필요합니다.")

        # 직접 작성(l-wanderer01): 검증을 통과한 모델을 반환한다.
        return self


class RuleVerdict(str, Enum):
    """규칙 검사를 정상 완료했을 때의 판정. 적용 제외·오류는 진행 상태다."""

    PASS = "pass"
    # 작성 기록(l-wanderer01): 기존 판정 Enum 패턴으로 규칙 위반 값을 정의했다.
    FAIL = "fail"


class RuleCheckResult(BaseModel):
    """규칙 검사 한 건의 기록. 점수 계산·정렬·추천 분기를 수행하지 않습니다."""

    # 생성 종료 시의 검사와 후처리 평가 모두 원본 실행에 연결됩니다.
    run_id: str = Field(min_length=1)

    # 작성 기록(l-wanderer01): 기존 선택 문자열 패턴을 평가 ID에 적용했다.
    # 생성 실행 자체의 워크플로우 검사에는 evaluation_id가 없을 수 있습니다.
    evaluation_id: str | None = None

    # 작성 기록(l-wanderer01): 필수 ID 패턴을 적용했다. 예: score_sum, recommendation_route.
    check_id: str = Field(min_length=1)

    # 작성 기록(l-wanderer01): 실행 경로와 정책 버전에 필수 문자열 패턴을 적용했다.
    execution_path: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)

    # 작성 기록(l-wanderer01): 기존 모델과 동일하게 진행 상태와 선택 판정을 분리했다.
    evaluation_status: EvaluationStatus
    verdict: RuleVerdict | None = None

    # 작성 기록(l-wanderer01): 기대값·실제값을 JsonValue 타입의 필수 필드로 정의했다.
    # JsonValue는 숫자·문자열·불리언·목록·객체·None 등 JSON 값을 허용합니다.
    # actual=null도 유효한 실제 결과일 수 있으므로 무조건 거부하지 않습니다.
    expected: JsonValue
    actual: JsonValue

    # 작성 기록(l-wanderer01): 기업 및 원인 호출 ID에 선택 문자열 패턴을 적용했다.
    # 전체 Top3 검사 등은 company_id=None, 원인 호출 불명은 invocation_id=None입니다.
    company_id: str | None = None
    invocation_id: str | None = None

    # 작성 기록(l-wanderer01): 기존 모델의 선택 사유 필드를 재사용했다.
    reason: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> RuleCheckResult:
        """검사 기록의 일관성을 확인합니다. expected와 actual을 비교하지 않습니다."""

        # 작성 기록(l-wanderer01): l-wanderer01이 작성한 완료 상태·판정 검증 패턴을 적용했다.
        if self.evaluation_status == EvaluationStatus.COMPLETED and self.verdict is None:
            raise ValueError("규칙 검사 완료 시 verdict가 필요합니다.")

        # 작성 기록(l-wanderer01): l-wanderer01이 작성한 미완료 상태·판정 검증 패턴을 적용했다.
        if self.evaluation_status != EvaluationStatus.COMPLETED and self.verdict is not None:
            raise ValueError("미완료 검사에는 규칙 판정을 기록할 수 없습니다.")

        # 작성 기록(l-wanderer01): l-wanderer01이 작성한 사유 필수 상태·빈 사유 검증을 적용했다.
        reason_required_statuses = {
            EvaluationStatus.MISSING,
            EvaluationStatus.ERROR,
            EvaluationStatus.NOT_APPLICABLE,
        }
        if self.evaluation_status in reason_required_statuses and not (self.reason or "").strip():
            raise ValueError("입력 누락·오류·적용 제외에는 사유가 필요합니다.")

        # 작성 기록(l-wanderer01): 기존 validator 패턴에 따라 검증된 모델을 반환한다.
        return self

    @computed_field
    @property
    def status(self) -> str:
        """checks.json용 status를 두 필드에서 계산해 저장합니다."""

        # 작성 기록(l-wanderer01): 완료 시 판정, 그 외에는 진행 상태를 저장용 문자열로 반환한다.
        # Enum의 .value는 JSON에 사용할 문자열입니다.
        # status를 입력 필드로 따로 관리하면 evaluation_status/verdict와 불일치할 수 있습니다.
        if self.evaluation_status == EvaluationStatus.COMPLETED:
            return self.verdict.value
        return self.evaluation_status.value


class RagasMetricName(str, Enum):
    """프로젝트 평가 계약에서 사용하는 지표 이름."""

    # 작성 기록(l-wanderer01): 앞서 사용한 Enum 패턴으로 허용 지표를 제한한다.
    FACTUAL_PRECISION = "factual_precision"
    FACTUAL_RECALL = "factual_recall"
    FAITHFULNESS = "faithfulness"
    CONTEXT_RECALL = "context_recall"
    CONTEXT_PRECISION = "context_precision"


class RagasMetricResult(BaseModel):
    """sample 하나의 지표 결과. RAGAS 호출이나 점수 계산은 수행하지 않습니다."""

    # 작성 기록(l-wanderer01): 기존 모델의 필수 ID와 Enum 필드 패턴을 적용한다.
    evaluation_id: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    metric_name: RagasMetricName
    evaluation_status: EvaluationStatus

    # 작성 기록(l-wanderer01): ge·le로 점수 범위를 0~1로 제한했다.
    # ge는 이상, le는 이하입니다. 0.0과 1.0도 정상 점수입니다.
    # allow_inf_nan=False는 JSON 점수로 사용할 수 없는 NaN·무한대를 거부합니다.
    value: float | None = Field(default=None, allow_inf_nan=False, ge=0.0, le=1.0)

    # 작성 기록(l-wanderer01): 기존 선택 문자열 패턴을 적용한다.
    reason: str | None = None
    invocation_id: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> RagasMetricResult:
        """평가 진행 상태와 반환된 점수의 관계를 검증합니다."""

        # 작성 기록(l-wanderer01): 기존 미완료 상태·판정 검증을 점수 필드에 적용한다.
        # is not None으로 검사하므로 0.0도 점수가 존재하는 것으로 처리합니다.
        if self.evaluation_status != EvaluationStatus.COMPLETED and self.value is not None:
            raise ValueError("미완료 평가에는 점수를 기록할 수 없습니다.")

        # 작성 기록(l-wanderer01): 기존 사유 필수 상태·빈 사유 검증을 적용한다.
        reason_required_statuses = {
            EvaluationStatus.MISSING,
            EvaluationStatus.ERROR,
            EvaluationStatus.NOT_APPLICABLE,
        }
        if self.evaluation_status in reason_required_statuses and not (self.reason or "").strip():
            raise ValueError("입력 누락·오류·적용 제외에는 사유가 필요합니다.")

        # 작성 기록(l-wanderer01): 완료됐지만 점수가 없을 때 빈 사유를 거부한다.
        # is None을 사용해 정상 점수 0.0과 값 없음 상태를 구분했다.
        if self.evaluation_status == EvaluationStatus.COMPLETED and self.value is None and not (self.reason or "").strip():
            raise ValueError("완료된 평가의 점수가 없으면 사유가 필요합니다.")

        # 사유 예: "평가 완료, 적용 가능한 주장이 없어 값이 정의되지 않음".
        # API 실패·입력 누락은 completed로 바꾸지 않고 error/missing으로 기록합니다.

        # 작성 기록(l-wanderer01): 기존 validator와 동일하게 검증된 모델을 반환한다.
        return self


# 작성 기록(Codex): 이하 모델은 평가 계약과 #20~#29에서 필요한 저장 구조를 작성했다.
# ID 생성·해시 계산·파일 저장·검색·LLM 호출·정책 판정은 별도 서비스의 책임이다.
# 모델은 한 기록 안의 타입·범위·관계만 검증한다. 여러 파일의 ID 참조 검사는 writer/harness가 수행한다.

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(strict=True, ge=0)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Rate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
ExecutionPath = Literal["agents", "investment_pipeline"]
CacheState = Literal["cold", "warm", "mixed", "unknown"]
EvaluationMode = Literal["isolated", "end_to_end"]


def _utc_datetime(value: datetime) -> datetime:
    """timezone 없는 시각을 거부하고, offset이 있는 시각을 UTC로 정규화한다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("시각에는 UTC 또는 명시적 timezone 정보가 필요합니다.")
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_utc_datetime)]


class RecordModel(BaseModel):
    """새 로그 구조의 공통 기반. 오타 필드와 비유한 숫자를 거부한다."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def discard_serialized_computed_fields(cls, data: object) -> object:
        # JSON에 저장된 계산 필드는 입력값으로 신뢰하지 않고 원본 필드에서 다시 계산한다.
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if key not in cls.model_computed_fields}
        return data


class GenerationStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkflowStatus(str, Enum):
    NOT_CHECKED = "not_checked"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class QualityEvaluationStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ErrorRecord(RecordModel):
    """오류는 사실 판정과 분리한다. 메시지는 저장 전에 인증정보를 제거해야 한다."""

    error_type: Identifier
    message: Identifier
    retryable: bool = False
    invocation_id: Identifier | None = None


class SnapshotRef(RecordModel):
    """실제 파일/원문을 다시 읽을 수 있는 위치와 내용 해시."""

    path: Identifier
    sha256: Sha256
    schema_version: Identifier | None = None
    media_type: str = "application/json"


class SourceLocation(RecordModel):
    """문자 위치는 0부터 시작하며 end_offset은 포함하지 않는다."""

    snapshot: SnapshotRef
    start_offset: Count
    end_offset: Count
    quote: str

    @model_validator(mode="after")
    def validate_offsets(self) -> SourceLocation:
        if self.end_offset < self.start_offset:
            raise ValueError("본문 끝 위치는 시작 위치보다 빠를 수 없습니다.")
        return self


class TimingRecord(RecordModel):
    """UTC 시각과 monotonic 경과시간을 함께 보존한다. 미시작 시각은 None이다."""

    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    duration_seconds: NonNegative | None = None

    @model_validator(mode="after")
    def validate_timing(self) -> TimingRecord:
        if self.ended_at is not None:
            if self.started_at is None or self.ended_at < self.started_at:
                raise ValueError("종료 시각에는 그보다 늦지 않은 시작 시각이 필요합니다.")
        if self.duration_seconds is not None and self.started_at is None:
            raise ValueError("경과시간을 기록하려면 시작 시각이 필요합니다.")
        return self


class ModelSettings(RecordModel):
    """인증정보를 받지 않는 모델 설정 허용 목록."""

    provider: Identifier
    model: Identifier
    temperature: NonNegative | None = None
    max_tokens: Annotated[int, Field(strict=True, gt=0)] | None = None
    timeout_seconds: NonNegative | None = None
    max_retries: Count = 0


class PromptVersion(RecordModel):
    prompt_id: Identifier
    version: Identifier
    sha256: Sha256
    snapshot: SnapshotRef | None = None


class RetrievalSettings(RecordModel):
    backend: Identifier
    dense_model: Identifier | None = None
    sparse_model: Identifier | None = None
    top_k: Annotated[int, Field(strict=True, gt=0)]
    chunk_size: Annotated[int, Field(strict=True, gt=0)] | None = None
    chunk_overlap: Count | None = None

    @model_validator(mode="after")
    def validate_overlap(self) -> RetrievalSettings:
        if self.chunk_overlap is not None:
            if self.chunk_size is None or self.chunk_overlap >= self.chunk_size:
                raise ValueError("chunk_overlap은 지정된 chunk_size보다 작아야 합니다.")
        return self


class ArtifactRecord(RecordModel):
    """저장 성공 판정은 파일을 실제로 검사한 writer가 제공한다."""

    artifact_id: Identifier
    format: Identifier
    path: Identifier
    required: bool = True
    status: Literal["pending", "saved", "error"] = "pending"
    sha256: Sha256 | None = None
    size_bytes: Count | None = None
    parseable: bool | None = None
    error: ErrorRecord | None = None

    @model_validator(mode="after")
    def validate_saved_artifact(self) -> ArtifactRecord:
        if self.status == "saved" and (
            self.sha256 is None or self.size_bytes is None
            or self.size_bytes == 0 or self.parseable is not True
        ):
            raise ValueError("저장 완료 산출물에는 해시·양수 파일 크기·형식 검증이 필요합니다.")
        if self.status == "error" and self.error is None:
            raise ValueError("산출물 저장 실패에는 오류 기록이 필요합니다.")
        return self


class RunManifest(TimingRecord):
    """run.json. 생성 종료 후 동결하고 후처리 평가 상태로 소급 수정하지 않는다."""

    run_id: Identifier
    comparison_role: Literal["baseline", "refactored", "standalone"]
    execution_path: ExecutionPath
    contract_version: Identifier
    schema_version: Identifier
    policy_version: Identifier
    workflow_version: Identifier
    commit_sha: Identifier
    dirty: bool
    dirty_diff_sha256: Sha256 | None = None
    included_paths: list[str] = Field(default_factory=list)
    excluded_paths: list[str] = Field(default_factory=list)
    as_of_date: date
    input_snapshot: SnapshotRef | None = None
    evidence_snapshot: SnapshotRef | None = None
    settings_sha256: Sha256 | None = None
    generation_model: ModelSettings | None = None
    prompts: list[PromptVersion] = Field(default_factory=list)
    retrieval: RetrievalSettings | None = None
    candidate_company_ids: list[Identifier] = Field(default_factory=list)
    requested_formats: list[Identifier] = Field(min_length=1)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    cache_state: CacheState = "unknown"
    llm_enabled: bool
    live_research_enabled: bool
    fallback_used: bool = False
    fallback_reasons: list[Identifier] = Field(default_factory=list)
    generation_status: GenerationStatus = GenerationStatus.NOT_STARTED
    workflow_status: WorkflowStatus = WorkflowStatus.NOT_CHECKED
    quality_evaluation_status: QualityEvaluationStatus = QualityEvaluationStatus.NOT_STARTED
    error: ErrorRecord | None = None

    @model_validator(mode="after")
    def validate_run(self) -> RunManifest:
        if self.fallback_used != bool(self.fallback_reasons):
            raise ValueError("fallback 사용 여부와 사유 목록이 일치해야 합니다.")
        if self.generation_status in {GenerationStatus.SUCCEEDED, GenerationStatus.FAILED}:
            if self.ended_at is None:
                raise ValueError("종료된 생성 실행에는 종료 시각이 필요합니다.")
        if self.generation_status == GenerationStatus.SUCCEEDED:
            required = [item for item in self.artifacts if item.required]
            if not required or any(item.status != "saved" for item in required):
                raise ValueError("생성 성공에는 필수 산출물의 저장 완료가 필요합니다.")
            saved_formats = {item.format for item in required}
            if not set(self.requested_formats).issubset(saved_formats):
                raise ValueError("요청한 형식의 필수 산출물이 누락되었습니다.")
        return self

    @computed_field
    @property
    def execution_succeeded(self) -> bool:
        return self.generation_status == GenerationStatus.SUCCEEDED and self.workflow_status == WorkflowStatus.PASSED


class MetricConfiguration(RecordModel):
    """채택한 라이브러리의 실제 metric API·설정·trace 지원을 기록한다."""

    metric_name: RagasMetricName
    implementation: Identifier
    mode: Literal["precision", "recall", "f1"] | None = None
    atomicity: Literal["low", "high"] | None = None
    coverage: Literal["low", "high"] | None = None
    prompt: PromptVersion
    language: Identifier = "ko"
    trace_supported: bool
    trace_limitations: list[str] = Field(default_factory=list)


class EvaluatorConfiguration(RecordModel):
    ragas_version: Identifier
    api_version: Identifier
    adapter_version: Identifier
    schema_version: Identifier
    model: ModelSettings
    embedding_model: ModelSettings | None = None
    metrics: list[MetricConfiguration] = Field(min_length=1)
    configuration_sha256: Sha256
    cache_enabled: bool = False
    max_retries: Count = 0
    llm_adapter: str | None = None
    dependencies: dict[str, str] = Field(default_factory=dict)
    runtime_settings: dict[str, JsonValue] = Field(default_factory=dict)
    custom_factual_configuration: dict[str, JsonValue] | None = None
    custom_grounding_configuration: dict[str, JsonValue] | None = None
    custom_coverage_configuration: dict[str, JsonValue] | None = None


class EvaluationManifest(TimingRecord):
    """evaluation.json. 최초 평가·재개·재평가마다 독립 ID를 부여한다."""

    evaluation_id: Identifier
    run_id: Identifier
    previous_evaluation_id: Identifier | None = None
    resume_reason: Identifier | None = None
    mode: EvaluationMode
    dataset_version: Identifier
    reference_sha256: Sha256
    input_sha256: Sha256
    evidence_sha256: Sha256
    evaluator: EvaluatorConfiguration
    quality_evaluation_status: QualityEvaluationStatus = QualityEvaluationStatus.NOT_STARTED
    sample_count: Count = 0
    completed_sample_count: Count = 0
    error: ErrorRecord | None = None

    @model_validator(mode="after")
    def validate_evaluation(self) -> EvaluationManifest:
        if self.completed_sample_count > self.sample_count:
            raise ValueError("완료 sample 수는 전체 sample 수보다 클 수 없습니다.")
        if self.previous_evaluation_id is not None:
            if self.previous_evaluation_id == self.evaluation_id or not self.resume_reason:
                raise ValueError("이전 평가 연결에는 서로 다른 ID와 재개·재평가 사유가 필요합니다.")
        terminal = {QualityEvaluationStatus.COMPLETED, QualityEvaluationStatus.PARTIAL, QualityEvaluationStatus.FAILED}
        if self.quality_evaluation_status in terminal and self.ended_at is None:
            raise ValueError("종료된 평가에는 종료 시각이 필요합니다.")
        if self.quality_evaluation_status == QualityEvaluationStatus.COMPLETED:
            if self.completed_sample_count != self.sample_count:
                raise ValueError("평가 완료 시 모든 적용 대상 sample이 완료되어야 합니다.")
        return self


class EvidenceRecord(RecordModel):
    """원문 evidence. reference와 같은 원문을 쓸 때 source_id로 출처 관계를 보존한다."""

    evidence_id: Identifier
    source_id: Identifier
    company_ids: list[Identifier] = Field(default_factory=list)
    title: str
    url: str | None = None
    published_date: date | None = None
    collected_at: UtcDatetime
    as_of_date: date
    snapshot: SnapshotRef
    content: str | None = None


class RetrievedContext(RecordModel):
    """검색 query 하나에서 반환된 원문 후보와 실제 순위."""

    context_id: Identifier
    evidence_id: Identifier
    source_id: Identifier
    rank: Annotated[int, Field(strict=True, ge=1)]
    text: str
    content_sha256: Sha256
    score: Annotated[float, Field(allow_inf_nan=False)] | None = None


class RetrievalRecord(TimingRecord):
    run_id: Identifier
    invocation_id: Identifier
    company_id: Identifier | None = None
    query: Identifier
    candidates: list[RetrievedContext] = Field(default_factory=list)
    cache_hit: bool | None = None
    error: ErrorRecord | None = None

    @model_validator(mode="after")
    def validate_ranks(self) -> RetrievalRecord:
        ranks = [item.rank for item in self.candidates]
        if ranks != sorted(set(ranks)):
            raise ValueError("검색 결과는 중복되지 않는 rank의 오름차순이어야 합니다.")
        return self


class DeliveredContext(RecordModel):
    """LLM에 실제 전달한 문맥. 원문·검색 후보와 구분해 보존한다."""

    context_id: Identifier
    invocation_id: Identifier
    context_kind: Literal["source_evidence", "intermediate_analysis", "mixed"]
    text: str
    sha256: Sha256
    evidence_ids: list[Identifier] = Field(default_factory=list)
    retrieval_invocation_ids: list[Identifier] = Field(default_factory=list)
    upstream_invocation_ids: list[Identifier] = Field(default_factory=list)


class InvocationRecord(TimingRecord):
    """함수·노드·LLM·검색 호출을 구분하고 재시도·재평가 계보를 연결한다."""

    run_id: Identifier
    evaluation_id: Identifier | None = None
    operation_id: Identifier
    invocation_id: Identifier
    parent_invocation_id: Identifier | None = None
    previous_invocation_id: Identifier | None = None
    link_reason: Literal["retry", "reevaluation"] | None = None
    attempt: Annotated[int, Field(strict=True, ge=1)] = 1
    invocation_type: Literal["function", "node", "llm", "retrieval", "tool"]
    agent_id: Identifier | None = None
    node_id: Identifier | None = None
    company_id: Identifier | None = None
    section_id: Identifier | None = None
    applies_to_company_ids: list[Identifier] = Field(default_factory=list)
    status: Literal["running", "succeeded", "failed", "skipped"]
    input_snapshot: SnapshotRef | None = None
    output_snapshot: SnapshotRef | None = None
    input_schema_version: Identifier
    output_schema_version: Identifier
    evidence_ids: list[Identifier] = Field(default_factory=list)
    context_ids: list[Identifier] = Field(default_factory=list)
    model: ModelSettings | None = None
    prompt: PromptVersion | None = None
    route: str | None = None
    state_update: SnapshotRef | None = None
    fallback_used: bool = False
    fallback_reason: Identifier | None = None
    error: ErrorRecord | None = None

    @model_validator(mode="after")
    def validate_invocation(self) -> InvocationRecord:
        if self.invocation_id in {self.parent_invocation_id, self.previous_invocation_id}:
            raise ValueError("호출은 자신을 부모 또는 이전 호출로 참조할 수 없습니다.")
        if (self.previous_invocation_id is None) != (self.link_reason is None):
            raise ValueError("이전 호출 ID와 연결 사유는 함께 기록해야 합니다.")
        if self.attempt > 1 and self.link_reason != "retry":
            raise ValueError("2회차 이상 시도에는 이전 호출과 retry 연결이 필요합니다.")
        if self.link_reason == "retry" and self.attempt < 2:
            raise ValueError("재시도 attempt는 2 이상이어야 합니다.")
        if self.link_reason == "reevaluation" and self.attempt != 1:
            raise ValueError("새 재평가 작업은 attempt=1로 시작합니다.")
        if self.fallback_used != (self.fallback_reason is not None):
            raise ValueError("fallback 사용 여부와 사유가 일치해야 합니다.")
        if self.status != "running" and self.ended_at is None:
            raise ValueError("종료·실패·skip 호출에는 종료 시각이 필요합니다.")
        if self.status == "failed" and self.error is None:
            raise ValueError("실패 호출에는 오류 기록이 필요합니다.")
        if self.status == "succeeded" and (self.input_snapshot is None or self.output_snapshot is None):
            raise ValueError("성공 호출에는 실제 입력·출력 스냅샷이 필요합니다.")
        return self


class EventRecord(RecordModel):
    """events.jsonl의 한 행. details에도 저장 전 인증정보 제거가 필요하다."""

    event_id: Identifier
    run_id: Identifier
    evaluation_id: Identifier | None = None
    invocation_id: Identifier | None = None
    event_type: Literal["start", "end", "failure", "retry", "skip", "fallback", "route_selected", "state_updated"]
    timestamp: UtcDatetime
    duration_seconds: NonNegative | None = None
    route: str | None = None
    state_update: SnapshotRef | None = None
    error: ErrorRecord | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class AgentOutputRecord(RecordModel):
    """중간 결과 원본. 자연어 외 구조화 결과도 JSON으로 보존한다."""

    run_id: Identifier
    invocation_id: Identifier
    agent_id: Identifier
    company_id: Identifier | None = None
    section_id: Identifier | None = None
    attempt: Annotated[int, Field(strict=True, ge=1)]
    schema_version: Identifier
    snapshot: SnapshotRef
    response: JsonValue
    evidence_ids: list[Identifier] = Field(default_factory=list)
    context_ids: list[Identifier] = Field(default_factory=list)


class EvaluationSample(RecordModel):
    """단계별 sample 원본. None은 입력 누락, 빈 문자열은 실제 빈 응답으로 구분한다."""

    sample_id: Identifier
    run_id: Identifier
    evaluation_id: Identifier
    case_id: Identifier
    company_id: Identifier | None = None
    section_id: Identifier
    agent_id: Identifier | None = None
    node_id: Identifier | None = None
    operation_id: Identifier
    invocation_id: Identifier
    attempt: Annotated[int, Field(strict=True, ge=1)]
    stage: Literal["intermediate", "final"]
    mode: EvaluationMode
    as_of_date: date
    user_input: str | None = None
    response: str | None = None
    reference: str | None = None
    input_snapshot: SnapshotRef | None = None
    response_location: SourceLocation | None = None
    input_sha256: Sha256 | None = None
    response_sha256: Sha256 | None = None
    reference_sha256: Sha256 | None = None
    evidence_ids: list[Identifier] = Field(default_factory=list)
    reference_ids: list[Identifier] = Field(default_factory=list)
    delivered_context_ids: list[Identifier] = Field(default_factory=list)
    retrieval_invocation_ids: list[Identifier] = Field(default_factory=list)
    supported_metrics: list[RagasMetricName] = Field(default_factory=list)
    unsupported_metric_reasons: dict[RagasMetricName, str] = Field(default_factory=dict)
    evaluation_status: EvaluationStatus = EvaluationStatus.PENDING
    reason: str | None = None

    @model_validator(mode="after")
    def validate_sample_status(self) -> EvaluationSample:
        if self.evaluation_status in {EvaluationStatus.MISSING, EvaluationStatus.ERROR, EvaluationStatus.NOT_APPLICABLE}:
            if not (self.reason or "").strip():
                raise ValueError("sample 입력 누락·오류·적용 제외에는 사유가 필요합니다.")
        return self


class RagasSampleInput(RecordModel):
    """metric별 adapter 입력. 모든 metric에 같은 문맥을 강제로 주입하지 않는다."""

    sample_id: Identifier
    evaluation_id: Identifier
    metric_name: RagasMetricName
    user_input: str | None = None
    response: str | None = None
    reference: str | None = None
    retrieved_contexts: list[str] | None = None
    context_kind: Literal["delivered", "received_input", "retrieved", "none"]
    context_ids: list[Identifier] = Field(default_factory=list)
    evidence_ids: list[Identifier] = Field(default_factory=list)
    reference_ids: list[Identifier] = Field(default_factory=list)
    origin_invocation_id: Identifier | None = None
    context_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_context_kind(self) -> RagasSampleInput:
        expected = {
            RagasMetricName.FAITHFULNESS: "delivered",
            RagasMetricName.CONTEXT_RECALL: "retrieved",
            RagasMetricName.CONTEXT_PRECISION: "retrieved",
            RagasMetricName.FACTUAL_PRECISION: "none",
            RagasMetricName.FACTUAL_RECALL: "none",
        }
        if self.metric_name == RagasMetricName.FAITHFULNESS and self.context_kind == 'received_input':
            return self
        if self.context_kind != expected[self.metric_name]:
            raise ValueError("metric에 맞는 문맥 종류를 사용해야 합니다.")
        return self


class RagasTraceRecord(RecordModel):
    """라이브러리가 실제 제공한 trace만 저장하고 custom claim 대응 여부를 표시한다."""

    evaluation_id: Identifier
    sample_id: Identifier
    metric_name: RagasMetricName
    invocation_id: Identifier | None = None
    raw_response: JsonValue
    trace_snapshot: SnapshotRef | None = None
    raw_claim_ids: list[str] = Field(default_factory=list)
    custom_claim_mapping: dict[str, list[Identifier]] = Field(default_factory=dict)
    mapping_status: Literal["available", "partial", "unsupported"]
    unavailable_reason: str | None = None
    numerator: Count | None = None
    denominator: Count | None = None
    count_source: Identifier | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> RagasTraceRecord:
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("제공된 trace 분자·분모는 함께 기록해야 합니다.")
        if self.denominator is not None:
            if self.numerator > self.denominator or self.count_source is None:
                raise ValueError("trace count에는 유효한 범위와 확인 가능한 출처가 필요합니다.")
        if self.mapping_status != "available" and not (self.unavailable_reason or "").strip():
            raise ValueError("부분 대응 또는 미지원 trace에는 사유가 필요합니다.")
        return self


class WorkflowStep(RecordModel):
    step_id: Identifier
    agent_id: Identifier
    node_ids: list[Identifier] = Field(default_factory=list)
    function_names: list[Identifier] = Field(default_factory=list)
    section_ids: list[Identifier] = Field(default_factory=list)
    per_company: bool
    required: bool
    enabled: bool
    supported: bool
    unsupported_reason: str | None = None


class WorkflowManifest(RecordModel):
    """경로별 기대 동작. 미지원과 필수 단계 실패를 구분해 검사기가 판정한다."""

    version: Identifier
    execution_path: ExecutionPath
    policy_version: Identifier
    check_basis: Literal["baseline_behavior", "target_policy"]
    candidate_company_ids: list[Identifier]
    steps: list[WorkflowStep] = Field(min_length=1)
    allowed_routes: dict[str, list[str]]
    requested_formats: list[Identifier] = Field(min_length=1)
    max_iterations: Annotated[int, Field(strict=True, gt=0)]
    termination_conditions: list[Identifier] = Field(min_length=1)
    no_new_evidence_policy: Identifier
    policy_snapshot: SnapshotRef

    @model_validator(mode="after")
    def validate_unique_ids(self) -> WorkflowManifest:
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("workflow step_id는 중복될 수 없습니다.")
        if len(set(self.candidate_company_ids)) != len(self.candidate_company_ids):
            raise ValueError("후보 기업 ID는 중복될 수 없습니다.")
        return self


class AdditionalResearchRecord(RecordModel):
    run_id: Identifier
    company_id: Identifier
    section_id: Identifier
    invocation_id: Identifier
    trigger: Identifier
    questions: list[str]
    search_invocation_ids: list[Identifier] = Field(default_factory=list)
    new_evidence_ids: list[Identifier] = Field(default_factory=list)
    duplicate_evidence_ids: list[Identifier] = Field(default_factory=list)
    input_updated: bool
    state_update: SnapshotRef | None = None
    reevaluation_invocation_id: Identifier | None = None
    outcome: Literal["new_evidence", "no_new_evidence", "error"]
    termination_reason: Identifier
    policy_version: Identifier
    error: ErrorRecord | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> AdditionalResearchRecord:
        if set(self.new_evidence_ids) & set(self.duplicate_evidence_ids):
            raise ValueError("같은 evidence를 신규·중복으로 동시에 기록할 수 없습니다.")
        if self.outcome == "new_evidence" and not self.new_evidence_ids:
            raise ValueError("신규 증거 확보 결과에는 신규 evidence ID가 필요합니다.")
        if self.outcome == "no_new_evidence" and self.new_evidence_ids:
            raise ValueError("no_new_evidence에 신규 증거를 기록할 수 없습니다.")
        if self.outcome == "error" and self.error is None:
            raise ValueError("조사 오류에는 오류 기록이 필요합니다.")
        return self


class ReferenceFact(RecordModel):
    """#21 기준 사실. 기존 생성 보고서를 자동 정답으로 채택하지 않는다."""

    fact_id: Identifier
    reference_id: Identifier
    company_id: Identifier | None = None
    section_id: Identifier
    statement: Identifier
    value: JsonValue = None
    unit: str | None = None
    conditions: list[str] = Field(default_factory=list)
    as_of_date: date
    source_ids: list[Identifier]
    source_locations: list[SourceLocation]
    critical: bool = False
    required_item_ids: list[Identifier] = Field(default_factory=list)
    review_status: Literal["pending", "verified", "rejected"]
    reviewer: Identifier | None = None
    reviewed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_review(self) -> ReferenceFact:
        if self.review_status != "pending" and (self.reviewer is None or self.reviewed_at is None):
            raise ValueError("완료된 reference 검토에는 검토자와 시각이 필요합니다.")
        if self.review_status == "verified" and (not self.source_ids or not self.source_locations):
            raise ValueError("검증된 기준 사실에는 출처와 원문 위치가 필요합니다.")
        return self


class RequiredInformationItem(RecordModel):
    """필수 항목의 적용·정확성·시점·근거 기준을 버전 관리한다."""

    required_item_id: Identifier
    version: Identifier
    company_id: Identifier | None = None
    section_id: Identifier
    description: Identifier
    accuracy_rule: Identifier
    freshness_rule: Identifier
    evidence_rule: Identifier
    applicability_rule: Identifier
    unknown_information_policy: Identifier
    reference_ids: list[Identifier] = Field(default_factory=list)


class DatasetCase(RecordModel):
    case_id: Identifier
    company_id: Identifier | None = None
    section_id: Identifier
    as_of_date: date
    user_input: Identifier
    evidence_snapshot: SnapshotRef
    reference_snapshot: SnapshotRef
    reference_ids: list[Identifier]
    required_item_ids: list[Identifier] = Field(default_factory=list)
    supported_metrics: list[RagasMetricName] = Field(default_factory=list)


class DatasetManifest(RecordModel):
    dataset_id: Identifier
    version: Identifier
    sha256: Sha256
    created_at: UtcDatetime
    reviewers: list[Identifier]
    cases: list[DatasetCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cases(self) -> DatasetManifest:
        ids = [case.case_id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("데이터셋 case_id는 중복될 수 없습니다.")
        return self


class ClaimRecord(RecordModel):
    """#22 원자 주장. 반복 출현 위치와 고유 주장을 구분한다."""

    claim_id: Identifier
    run_id: Identifier
    evaluation_id: Identifier
    sample_id: Identifier
    invocation_id: Identifier
    company_id: Identifier | None = None
    section_id: Identifier
    atomic_statement: Identifier
    claim_kind: Literal["fact", "opinion", "mixed"]
    critical: bool
    occurrences: list[SourceLocation] = Field(min_length=1)
    evidence_ids: list[Identifier] = Field(default_factory=list)
    reference_ids: list[Identifier] = Field(default_factory=list)
    upstream_claim_ids: list[Identifier] = Field(default_factory=list)
    error_origin_candidate: Literal["inherited", "new", "corrected", "unknown"] = "unknown"
    origin_reason: str | None = None
    extractor_version: Identifier


class GroundingAssessment(RecordModel):
    """원문 근거 지지와 전달 문맥 지지를 구분하는 custom 결과."""

    evaluation_id: Identifier
    claim_id: Identifier
    basis: Literal["source_evidence", "delivered_context"]
    evaluation_status: EvaluationStatus
    grounded: bool | None = None
    supporting_evidence_ids: list[Identifier] = Field(default_factory=list)
    supporting_context_ids: list[Identifier] = Field(default_factory=list)
    source_locations: list[SourceLocation] = Field(default_factory=list)
    reason: Identifier

    @model_validator(mode="after")
    def validate_grounding(self) -> GroundingAssessment:
        if (self.evaluation_status == EvaluationStatus.COMPLETED) != (self.grounded is not None):
            raise ValueError("완료된 문맥 지지 검사에만 grounded 판정을 기록합니다.")
        if self.grounded is True:
            ids = self.supporting_evidence_ids if self.basis == "source_evidence" else self.supporting_context_ids
            if not ids:
                raise ValueError("지지 판정에는 평가 기준에 맞는 근거 ID가 필요합니다.")
        return self


class CitationAssessment(RecordModel):
    evaluation_id: Identifier
    claim_id: Identifier
    cited_source_id: Identifier
    evaluation_status: EvaluationStatus
    verdict: Literal["supported", "unsupported", "contradicted", "inaccessible"] | None = None
    source_locations: list[SourceLocation] = Field(default_factory=list)
    reason: Identifier

    @model_validator(mode="after")
    def validate_citation(self) -> CitationAssessment:
        if (self.evaluation_status == EvaluationStatus.COMPLETED) != (self.verdict is not None):
            raise ValueError("완료된 인용 검사에만 내용 판정을 기록합니다.")
        if self.verdict == "supported" and not self.source_locations:
            raise ValueError("인용 지지 판정에는 원문 구간이 필요합니다.")
        return self


class HumanReviewRecord(RecordModel):
    """#27 원판정과 사람 확정 결과를 덮어쓰지 않고 별도 보존한다."""

    review_id: Identifier
    evaluation_id: Identifier
    target_id: Identifier
    target_kind: Literal["claim", "metric", "required_item", "rule", "extraction"]
    reviewer: Identifier
    reviewed_at: UtcDatetime
    automatic_result: JsonValue
    human_result: JsonValue
    reason: Identifier
    previous_review_id: Identifier | None = None


class ClaimCounts(RecordModel):
    """custom 주장 판정 집계. RAGAS 내부 분모로 재사용하지 않는다."""

    total: Count
    judged: Count
    pending: Count
    verified: Count
    contradicted: Count
    unverifiable: Count
    critical_contradicted: Count = 0
    critical_unverifiable: Count = 0

    @model_validator(mode="after")
    def validate_counts(self) -> ClaimCounts:
        if self.verified + self.contradicted + self.unverifiable != self.judged:
            raise ValueError("세 판정의 합은 judged와 같아야 합니다.")
        if self.judged + self.pending != self.total:
            raise ValueError("judged + pending은 total과 같아야 합니다.")
        if self.critical_contradicted > self.contradicted or self.critical_unverifiable > self.unverifiable:
            raise ValueError("핵심 오류 수는 해당 전체 판정 수보다 클 수 없습니다.")
        return self

    @computed_field
    @property
    def completion_rate(self) -> float | None:
        return self.judged / self.total if self.total else None

    @computed_field
    @property
    def verified_rate(self) -> float | None:
        return self.verified / self.total if self.total and not self.pending else None

    @computed_field
    @property
    def contradicted_rate(self) -> float | None:
        return self.contradicted / self.total if self.total and not self.pending else None

    @computed_field
    @property
    def unverifiable_rate(self) -> float | None:
        return self.unverifiable / self.total if self.total and not self.pending else None


class CheckCounts(RecordModel):
    """필수 정보 또는 규칙 검사의 분모. missing은 미완료 입력 누락 수다."""

    applicable: Count
    passed: Count
    failed: Count
    pending: Count = 0
    missing: Count = 0
    error: Count = 0
    not_applicable: Count = 0

    @model_validator(mode="after")
    def validate_counts(self) -> CheckCounts:
        if self.passed + self.failed + self.pending + self.missing + self.error != self.applicable:
            raise ValueError("적용 대상 수는 완료·미완료 항목의 합과 같아야 합니다.")
        return self

    @computed_field
    @property
    def completion_rate(self) -> float | None:
        return (self.passed + self.failed) / self.applicable if self.applicable else None

    @computed_field
    @property
    def pass_rate(self) -> float | None:
        if not self.applicable or self.pending + self.missing + self.error:
            return None
        return self.passed / self.applicable


class UsageRecord(RecordModel):
    """#28 미제공 usage는 None이다. cached tokens는 input tokens의 부분집합이다."""

    run_id: Identifier
    evaluation_id: Identifier | None = None
    invocation_id: Identifier
    purpose: Literal["generation", "ragas_evaluation", "custom_evaluation"]
    call_type: Literal["llm", "embedding", "search"]
    provider: Identifier
    model: str | None = None
    attempt: Annotated[int, Field(strict=True, ge=1)]
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    cached_input_tokens: Count | None = None
    cache_hit: bool | None = None
    duration_seconds: NonNegative | None = None
    cost: NonNegative | None = None
    currency: str | None = None
    price_version: str | None = None
    price_as_of_date: date | None = None
    calculation_method: str | None = None
    unknown_reason: str | None = None

    @model_validator(mode="after")
    def validate_usage(self) -> UsageRecord:
        if self.cached_input_tokens is not None and self.input_tokens is not None:
            if self.cached_input_tokens > self.input_tokens:
                raise ValueError("cached input tokens는 전체 input tokens보다 클 수 없습니다.")
        if self.cost is None and not (self.unknown_reason or "").strip():
            raise ValueError("미확인 비용에는 사유가 필요합니다.")
        if self.cost is not None and not all((self.currency, self.price_version, self.price_as_of_date, self.calculation_method)):
            raise ValueError("확인된 비용에는 통화·가격표 버전·기준일·계산 방법이 필요합니다.")
        return self


class RuntimeMetrics(RecordModel):
    purpose: Literal["generation", "ragas_evaluation", "custom_evaluation"]
    duration_seconds: NonNegative | None = None
    cost: NonNegative | None = None
    currency: str | None = None
    usage: list[UsageRecord] = Field(default_factory=list)
    cache_state: CacheState = "unknown"
    unknown_reason: str | None = None

    @model_validator(mode="after")
    def validate_purpose(self) -> RuntimeMetrics:
        if any(item.purpose != self.purpose for item in self.usage):
            raise ValueError("서로 다른 목적의 비용·시간을 한 집계에 섞을 수 없습니다.")
        if (self.cost is None or self.duration_seconds is None) and not (self.unknown_reason or "").strip():
            raise ValueError("미확인 비용 또는 시간에는 사유가 필요합니다.")
        if self.cost is not None and not self.currency:
            raise ValueError("비용 집계에는 통화가 필요합니다.")
        return self


class DecisionRecord(RecordModel):
    """경로별 원래 점수 척도와 정책을 보존한다. 점수 재계산은 하지 않는다."""

    run_id: Identifier
    company_id: Identifier
    invocation_id: Identifier
    execution_path: ExecutionPath
    policy_version: Identifier
    stage: str | None = None
    score: Annotated[float, Field(allow_inf_nan=False)]
    score_scale: Identifier
    recommendation: Identifier
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    original_snapshot: SnapshotRef


class RankingRecord(RecordModel):
    run_id: Identifier
    invocation_id: Identifier
    policy_version: Identifier
    branch: Literal["top3", "hold"]
    ranked_company_ids: list[Identifier]
    selected_company_ids: list[Identifier]
    held_company_ids: list[Identifier]
    threshold: Annotated[float, Field(allow_inf_nan=False)]
    top_k: Annotated[int, Field(strict=True, gt=0)]
    original_snapshot: SnapshotRef


class MetricsRecord(RecordModel):
    """품질·검색 진단·실행 자원을 구분해 보존하는 metrics.json 구조."""

    run_id: Identifier
    evaluation_id: Identifier | None = None
    company_id: Identifier | None = None
    section_id: Identifier | None = None
    scope: Literal["run", "company", "section", "sample"]
    aggregation: Literal["none", "company_macro", "section_macro", "claim_weighted"] = "none"
    sample_count: Count = 0
    claim_counts: ClaimCounts | None = None
    required_information_counts: CheckCounts | None = None
    rule_counts: CheckCounts | None = None
    ragas_results: list[RagasMetricResult] = Field(default_factory=list)
    generation_runtime: RuntimeMetrics | None = None
    ragas_runtime: RuntimeMetrics | None = None
    custom_runtime: RuntimeMetrics | None = None
    error_claim_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_metric_context(self) -> MetricsRecord:
        for record, purpose in (
            (self.generation_runtime, "generation"),
            (self.ragas_runtime, "ragas_evaluation"),
            (self.custom_runtime, "custom_evaluation"),
        ):
            if record is not None and record.purpose != purpose:
                raise ValueError("비용·시간은 생성·RAGAS·custom 평가별 필드에 기록해야 합니다.")
        if self.ragas_results and self.evaluation_id is None:
            raise ValueError("RAGAS 결과 집계에는 evaluation_id가 필요합니다.")
        if any(item.evaluation_id != self.evaluation_id for item in self.ragas_results):
            raise ValueError("서로 다른 evaluation_id의 결과를 섞을 수 없습니다.")
        return self


class ComparisonPair(RecordModel):
    """#29 전후 pairing 기준. 변경된 생성 설정은 두 manifest에서 별도 확인한다."""

    pair_id: Identifier
    case_id: Identifier
    company_id: Identifier | None = None
    section_id: Identifier
    agent_id: Identifier | None = None
    mode: EvaluationMode
    attempt: Annotated[int, Field(strict=True, ge=1)]
    baseline_run_id: Identifier
    refactored_run_id: Identifier
    baseline_evaluation_id: Identifier
    refactored_evaluation_id: Identifier
    baseline_sample_id: Identifier
    refactored_sample_id: Identifier
    input_sha256: Sha256
    evidence_sha256: Sha256
    reference_sha256: Sha256
    evaluator_configuration_sha256: Sha256
    actual_input_changed: bool = False
    change_reason: str | None = None

    @model_validator(mode="after")
    def validate_pair(self) -> ComparisonPair:
        if self.baseline_run_id == self.refactored_run_id:
            raise ValueError("전후 비교는 서로 다른 생성 실행을 사용해야 합니다.")
        if self.actual_input_changed and (self.mode == "isolated" or not (self.change_reason or "").strip()):
            raise ValueError("입력 변경은 end_to_end에서만 사유와 함께 표시합니다.")
        return self
