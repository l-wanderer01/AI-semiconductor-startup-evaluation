"""평가 계약의 저장·상태·계보 경계를 검증한다. 외부 API를 호출하지 않는다."""

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from evaluation import models as m


SHA = "a" * 64
START = datetime(2026, 10, 4, tzinfo=timezone.utc)
END = START + timedelta(seconds=1)
SNAPSHOT = dict(path="snapshots/input.json", sha256=SHA)


def run_fields():
    return dict(
        run_id="run_1", comparison_role="baseline", execution_path="agents",
        contract_version="0.1", schema_version="0.1", policy_version="agents_v1",
        workflow_version="0.1", commit_sha="abc123", dirty=False,
        as_of_date="2026-10-04", requested_formats=["md"],
        llm_enabled=True, live_research_enabled=False,
    )


def evaluator_fields():
    return dict(
        ragas_version="chosen-version", api_version="chosen-api", adapter_version="0.1",
        schema_version="0.1", model=dict(provider="test", model="evaluator"),
        configuration_sha256=SHA,
        metrics=[dict(metric_name="faithfulness", implementation="chosen-metric",
                      prompt=dict(prompt_id="faithfulness", version="0.1", sha256=SHA),
                      trace_supported=False, trace_limitations=["not exposed"])],
    )


class EvaluationModelTests(unittest.TestCase):
    def assert_round_trip(self, model):
        self.assertEqual(type(model).model_validate_json(model.model_dump_json()), model)

    def test_original_models_still_work(self):
        records = [
            m.FactAssessment(evaluation_id="eval_1", claim_id="claim_1",
                             evaluation_status="completed", verdict="unverifiable"),
            m.RequiredInformationAssessment(evaluation_id="eval_1", sample_id="sample_1",
                                            required_item_id="risk", evaluation_status="completed", verdict="missing"),
            m.RuleCheckResult(run_id="run_1", check_id="route", execution_path="agents",
                              policy_version="v1", expected="hold", actual="top3",
                              evaluation_status="completed", verdict="fail"),
            m.RagasMetricResult(evaluation_id="eval_1", sample_id="sample_1",
                                metric_name="faithfulness", evaluation_status="completed", value=0.0),
        ]
        for record in records:
            with self.subTest(model=type(record).__name__):
                self.assert_round_trip(record)

    def test_utc_normalization_and_naive_rejection(self):
        local = START.astimezone(timezone(timedelta(hours=9)))
        record = m.TimingRecord(started_at=local, ended_at=END, duration_seconds=1)
        self.assertEqual(record.started_at.utcoffset(), timedelta(0))
        self.assert_round_trip(record)
        for fields in [dict(started_at=datetime(2026, 10, 4)),
                       dict(started_at=END, ended_at=START),
                       dict(duration_seconds=1), dict(started_at=START, duration_seconds=-1)]:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                m.TimingRecord(**fields)

    def test_saved_artifact_requires_real_validation(self):
        fields = dict(artifact_id="report", format="md", path="report.md", status="saved")
        with self.assertRaises(ValidationError):
            m.ArtifactRecord(**fields)
        record = m.ArtifactRecord(**fields, sha256=SHA, size_bytes=10, parseable=True)
        self.assert_round_trip(record)

    def test_run_success_does_not_hide_workflow_failure(self):
        record = m.RunManifest(**run_fields(), started_at=START, ended_at=END,
                               generation_status="succeeded", workflow_status="failed",
                               artifacts=[dict(artifact_id="report", format="md", path="report.md",
                                               status="saved", sha256=SHA, size_bytes=10, parseable=True)])
        self.assertFalse(record.execution_succeeded)
        self.assert_round_trip(record)
        with self.assertRaises(ValidationError):
            m.RunManifest(**run_fields(), started_at=START, ended_at=END, generation_status="succeeded")
        with self.assertRaises(ValidationError):
            m.RunManifest(**run_fields(), fallback_used=True)

    def test_evaluation_manifest_resume_and_coverage(self):
        fields = dict(evaluation_id="eval_2", run_id="run_1", mode="isolated",
                      dataset_version="v1", reference_sha256=SHA, input_sha256=SHA,
                      evidence_sha256=SHA, evaluator=evaluator_fields(),
                      started_at=START, ended_at=END, sample_count=2, completed_sample_count=1)
        record = m.EvaluationManifest(**fields, previous_evaluation_id="eval_1",
                                      resume_reason="retry failed sample", quality_evaluation_status="partial")
        self.assert_round_trip(record)
        with self.assertRaises(ValidationError):
            m.EvaluationManifest(**fields, quality_evaluation_status="completed")
        with self.assertRaises(ValidationError):
            m.EvaluationManifest(**fields, previous_evaluation_id="eval_2", resume_reason="retry")

    def test_invocation_retry_and_snapshot_requirements(self):
        fields = dict(run_id="run_1", operation_id="op_1", invocation_id="call_2",
                      invocation_type="llm", input_schema_version="v1", output_schema_version="v1",
                      status="succeeded", started_at=START, ended_at=END,
                      input_snapshot=SNAPSHOT, output_snapshot=SNAPSHOT)
        record = m.InvocationRecord(**fields, attempt=2, previous_invocation_id="call_1", link_reason="retry")
        self.assert_round_trip(record)
        for extra in [dict(attempt=2), dict(parent_invocation_id="call_2"),
                      dict(previous_invocation_id="call_1"), dict(fallback_used=True)]:
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                m.InvocationRecord(**(fields | extra))
        with self.assertRaises(ValidationError):
            m.InvocationRecord(**(fields | dict(output_snapshot=None)))

    def test_search_order_and_metric_context_separation(self):
        context = dict(context_id="ctx_1", evidence_id="ev_1", source_id="src_1",
                       rank=1, text="original source", content_sha256=SHA)
        record = m.RetrievalRecord(run_id="run_1", invocation_id="search_1", query="company A",
                                   candidates=[context])
        self.assert_round_trip(record)
        with self.assertRaises(ValidationError):
            m.RetrievalRecord(run_id="run_1", invocation_id="search_1", query="company A",
                              candidates=[context, context])
        for metric, kind in [("faithfulness", "delivered"), ("context_precision", "retrieved"),
                             ("context_recall", "retrieved"), ("factual_precision", "none")]:
            self.assert_round_trip(m.RagasSampleInput(sample_id="sample_1", evaluation_id="eval_1",
                                                     metric_name=metric, context_kind=kind))
        with self.assertRaises(ValidationError):
            m.RagasSampleInput(sample_id="sample_1", evaluation_id="eval_1",
                               metric_name="context_precision", context_kind="delivered")

    def test_unknown_trace_counts_are_not_invented(self):
        fields = dict(evaluation_id="eval_1", sample_id="sample_1", metric_name="faithfulness",
                      raw_response={"value": 0.5}, mapping_status="unsupported", unavailable_reason="no trace")
        record = m.RagasTraceRecord(**fields)
        self.assertIsNone(record.denominator)
        self.assert_round_trip(record)
        with self.assertRaises(ValidationError):
            m.RagasTraceRecord(**fields, numerator=1, denominator=2)

    def test_claim_counts_incomplete_zero_and_complete(self):
        incomplete = m.ClaimCounts(total=3, judged=2, pending=1, verified=1, contradicted=1, unverifiable=0)
        self.assertIsNone(incomplete.verified_rate)
        self.assertAlmostEqual(incomplete.completion_rate, 2 / 3)
        zero = m.ClaimCounts(total=0, judged=0, pending=0, verified=0, contradicted=0, unverifiable=0)
        self.assertIsNone(zero.completion_rate)
        complete = m.ClaimCounts(total=2, judged=2, pending=0, verified=1, contradicted=1, unverifiable=0)
        self.assertEqual(complete.verified_rate, 0.5)
        for record in [incomplete, zero, complete]:
            self.assert_round_trip(record)
        with self.assertRaises(ValidationError):
            m.ClaimCounts(total=3, judged=2, pending=0, verified=1, contradicted=1, unverifiable=0)
        with self.assertRaises(ValidationError):
            m.ClaimCounts(total=1, judged=1, pending=0, verified=1, contradicted=0,
                           unverifiable=0, critical_contradicted=1)

    def test_input_missing_and_confirmed_missing_have_different_coverage(self):
        record = m.CheckCounts(applicable=3, passed=1, failed=1, missing=1, not_applicable=2)
        self.assertIsNone(record.pass_rate)
        self.assertAlmostEqual(record.completion_rate, 2 / 3)
        self.assert_round_trip(record)
        self.assertEqual(m.CheckCounts(applicable=2, passed=1, failed=1).pass_rate, 0.5)

    def test_no_new_evidence_is_not_research_success(self):
        fields = dict(run_id="run_1", company_id="company_a", section_id="technology",
                      invocation_id="research_1", trigger="low_score", questions=["patent?"],
                      input_updated=False, outcome="no_new_evidence", termination_reason="no sources",
                      policy_version="v1")
        self.assert_round_trip(m.AdditionalResearchRecord(**fields))
        with self.assertRaises(ValidationError):
            m.AdditionalResearchRecord(**fields, new_evidence_ids=["ev_1"])

    def test_reference_requires_human_and_source(self):
        fields = dict(fact_id="fact_1", reference_id="ref_1", section_id="technology",
                      statement="A has a prototype", as_of_date="2026-10-04",
                      source_ids=["src_1"], source_locations=[], review_status="verified")
        with self.assertRaises(ValidationError):
            m.ReferenceFact(**fields)
        record = m.ReferenceFact(**(fields | dict(reviewer="reviewer_1", reviewed_at=START,
                                    source_locations=[dict(snapshot=SNAPSHOT, start_offset=0, end_offset=10, quote="prototype")])) )
        self.assert_round_trip(record)

    def test_usage_unknown_cost_and_purpose_separation(self):
        fields = dict(run_id="run_1", invocation_id="call_1", purpose="generation", call_type="llm",
                      provider="test", attempt=1, input_tokens=10, cached_input_tokens=5,
                      unknown_reason="provider did not supply usage")
        record = m.UsageRecord(**fields)
        self.assertIsNone(record.cost)
        self.assert_round_trip(record)
        with self.assertRaises(ValidationError):
            m.UsageRecord(**(fields | dict(cached_input_tokens=11)))
        with self.assertRaises(ValidationError):
            m.RuntimeMetrics(purpose="ragas_evaluation", usage=[record], unknown_reason="unknown")
        with self.assertRaises(ValidationError):
            m.MetricsRecord(run_id="run_1", scope="run",
                            generation_runtime=dict(purpose="ragas_evaluation", unknown_reason="unknown"))

    def test_pairing_does_not_accept_changed_isolated_input(self):
        fields = dict(pair_id="pair_1", case_id="case_1", section_id="technology", mode="isolated", attempt=1,
                      baseline_run_id="run_1", refactored_run_id="run_2",
                      baseline_evaluation_id="eval_1", refactored_evaluation_id="eval_2",
                      baseline_sample_id="sample_1", refactored_sample_id="sample_2",
                      input_sha256=SHA, evidence_sha256=SHA, reference_sha256=SHA, evaluator_configuration_sha256=SHA)
        self.assert_round_trip(m.ComparisonPair(**fields))
        with self.assertRaises(ValidationError):
            m.ComparisonPair(**fields, actual_input_changed=True, change_reason="upstream changed")

    def test_new_records_reject_typos_and_invalid_hashes(self):
        with self.assertRaises(ValidationError):
            m.SnapshotRef(path="input.json", sha256="not-a-sha256")
        with self.assertRaises(ValidationError):
            m.SnapshotRef(**SNAPSHOT, unknown_field="typo")
        # 계산 필드는 저장 후 복원 시 신뢰하지 않고 원본 count에서 재계산한다.
        record = m.ClaimCounts(total=1, judged=1, pending=0, verified=1, contradicted=0,
                               unverifiable=0, verified_rate=0.2)
        self.assertEqual(record.verified_rate, 1.0)


if __name__ == "__main__":
    unittest.main()
