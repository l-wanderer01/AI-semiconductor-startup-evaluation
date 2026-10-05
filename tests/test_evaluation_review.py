"""#27 검토 계약 회귀. reviewer 문자열은 테스트용이며 실제 사람 검토가 아니다."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from evaluation.models import ModelSettings, RagasSampleInput
from evaluation.recording import RunRecorder, SnapshotPayload, now
from evaluation.ragas_adapter import evaluate_inputs
from evaluation.review import (ReviewDecision, ReviewStore, inventory, reliability_report,
                               review_plan, strata)
from evaluation.reevaluation import comparison, reevaluate_pair
from evaluation.storage import EvaluationStorage
import test_evaluation_ragas_adapter as adapter_tests


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.rec = RunRecorder(root=self.root, execution_path='agents', inputs={}, settings={},
            requested_formats=['md'], repository=Path(__file__).resolve().parents[1])
        self.rec.finish(exc=ValueError('fixture'))
        self.storage = EvaluationStorage(self.root)
        self.directory = self.storage.create_evaluation(self.rec.run_id, 'evaluation_review')
        # Actual manifest fixture from deterministic evaluation helper.
        from evaluation.models import EvaluationManifest
        if not hasattr(adapter_tests.RagasAdapterTests, 'llm'):
            adapter_tests.RagasAdapterTests.setUpClass()
        self.adapter = adapter_tests.RagasAdapterTests().adapter()
        config = self.adapter.configuration(self.storage, self.directory)
        manifest = EvaluationManifest(evaluation_id=self.directory.name, run_id=self.rec.run_id,
            mode='end_to_end', dataset_version='synthetic', reference_sha256='a'*64,
            input_sha256=self.rec.manifest.input_snapshot.sha256,
            evidence_sha256=self.rec.manifest.evidence_snapshot.sha256, evaluator=config,
            started_at=now(), ended_at=now(), quality_evaluation_status='completed',
            sample_count=1, completed_sample_count=1)
        self.storage.write_json(self.directory, 'evaluation.json', manifest)
        # Source shapes are the native assessment/claim records (not a data wrapper).
        fixture = json.loads((Path(__file__).parent / 'fixtures/evaluator_ko_v1.json').read_text())
        self.assertFalse(fixture['human_reviewed'])
        self.fixture = fixture
        claims, assessments = [], []
        for case in fixture['cases']:
            claims.append({'claim_id':case['id'], 'atomic_statement':case['response'], 'critical':True})
            assessments.append({'evaluation_id':self.directory.name, 'claim_id':case['id'],
                'evaluation_status':'completed', 'verdict':case['expected']})
        self.storage.write_bytes(self.directory, 'claims.jsonl',
            ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in claims).encode(), media_type='application/jsonl')
        self.storage.write_bytes(self.directory, 'fact_assessments.jsonl',
            ''.join(json.dumps(r)+'\n' for r in assessments).encode(), media_type='application/jsonl')
        metric = {'evaluation_id':self.directory.name, 'sample_id':'sample', 'metric_name':'factual_precision',
                  'evaluation_status':'completed', 'value':0.73}
        self.storage.write_bytes(self.directory, 'ragas_results.jsonl', (json.dumps(metric)+'\n').encode(), media_type='application/jsonl')
        self.storage.write_json(self.directory, 'stage_package.json', SnapshotPayload(data={
            'extractions':[{'unit':{'unit_id':'unit', 'text':'고객은 2곳이고 PoC 진행 중이다.'}, 'status':'completed'}],
            'atoms':[{'unit_id':'unit', 'candidate':{'statement':'고객은 2곳이다.', 'kind':'fact'}}]}))
        self.storage.write_bytes(self.directory, 'grounding_assessments.jsonl', (json.dumps({
            'evaluation_id':self.directory.name, 'claim_id':'number', 'basis':'source_evidence',
            'evaluation_status':'completed', 'grounded':True})+'\n').encode(), media_type='application/jsonl')
        self.storage.freeze(self.directory)
        self.store = ReviewStore(self.directory)
        self.targets = inventory(self.directory)

    def decision(self, target, *, result=None, previous=None, reviewer='reviewer-a', status='confirmed', provenance='human', resolves=()):
        return ReviewDecision(review_id='review-' + str(len(self.store.read())+1),
            evaluation_id=target.evaluation_id, target_id=target.target_id,
            target_kind=target.target_kind, reviewer=reviewer, reviewed_at=now(),
            automatic_result=target.automatic_result,
            human_result=target.automatic_label if result is None else result, reason='fixture 검토 이유',
            source_sha256=target.source_sha256, previous_review_id=previous,
            provenance=provenance, status=status, resolves_review_ids=list(resolves))

    def test_plan_census_strata_determinism(self):
        plan = review_plan(self.targets, per_stratum=1)
        self.assertFalse(plan['missing_strata'])
        self.assertEqual(plan, review_plan(list(reversed(self.targets)), per_stratum=1))
        self.assertEqual(len(plan['targets']), len(self.targets))
        for case in self.fixture['cases']:
            self.assertTrue(set(case['tags']).issubset(strata(case['response'])))

    def test_history_preserves_frozen_files_and_overrides(self):
        before = {p.name:p.read_bytes() for p in self.directory.iterdir() if p.is_file()}
        target = self.targets[0]
        first = self.decision(target, result='verified')
        self.store.append(first)
        correction = self.decision(target, result='contradicted', previous=first.review_id)
        self.store.append(correction)
        report = reliability_report(self.targets, self.store.read())
        self.assertEqual(report['groups']['custom_factual']['human_pair_count'], 1)
        self.assertEqual(report['groups']['custom_factual']['sample_agreement'], 1)
        self.assertEqual(len(self.store.read()), 2)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.directory.iterdir() if p.is_file()})
        self.assertIsNone(report['groups']['custom_factual']['validated_accuracy'])

    def test_disagreement_requires_independent_adjudicator(self):
        target = self.targets[0]
        first = self.decision(target, result='verified')
        self.store.append(first)
        second = self.decision(target, result='contradicted', reviewer='reviewer-b', previous=first.review_id)
        self.store.append(second)
        self.assertIn(target.target_id, reliability_report(self.targets, self.store.read())['conflicting_target_ids'])
        bad = self.decision(target, result='contradicted', status='adjudicated', previous=second.review_id,
            resolves=[first.review_id, second.review_id])
        with self.assertRaisesRegex(ValueError, 'third reviewer'):
            self.store.append(bad)
        good = bad.model_copy(update={'reviewer':'adjudicator'})
        self.store.append(good)
        self.assertFalse(reliability_report(self.targets, self.store.read())['conflicting_target_ids'])

    def test_reject_wrong_scope_hash_label_and_stale_revision(self):
        target = self.targets[0]
        decision = self.decision(target)
        for update in ({'evaluation_id':'other'}, {'source_sha256':'b'*64}, {'human_result':0.7},
                       {'automatic_result':{}}, {'previous_review_id':'missing'}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.store.append(decision.model_copy(update=update))
        self.store.append(decision)
        with self.assertRaises(ValueError):
            self.store.append(decision)

    def test_no_labels_synthetic_and_continuous_scores_are_not_accuracy(self):
        report = reliability_report(self.targets, [])
        self.assertIsNone(report['groups']['custom_factual']['sample_agreement'])
        self.assertFalse(report['confidence_is_validated_accuracy'])
        target = self.targets[0]
        self.store.append(self.decision(target, provenance='synthetic_fixture'))
        metrics = [t for t in self.targets if t.target_kind=='metric']
        self.store.append(self.decision(metrics[0], result={'score':0.5,'error':True,'error_type':'unit_mismatch'}))
        report = reliability_report(self.targets, self.store.read())
        self.assertEqual(report['groups']['custom_factual']['human_pair_count'], 0)
        self.assertEqual(report['groups']['custom_factual']['synthetic_pair_count'], 1)
        continuous = report['groups']['factual_precision']
        self.assertIsNone(continuous['confusion_matrix'])
        self.assertEqual(continuous['raw_metric_reviews'][0]['automatic_score'], 0.73)
        self.assertEqual(continuous['metric_error_count'], 1)

    def test_confusion_matrix_uses_gold_rows_and_null_undefined_ratios(self):
        self.store.append(self.decision(self.targets[0], result='verified'))
        group = reliability_report(self.targets, self.store.read())['groups']['custom_factual']
        self.assertEqual(group['confusion_matrix']['verified']['contradicted'], 1)
        self.assertEqual(group['per_label']['verified']['recall'], 0)
        self.assertIsNone(group['per_label']['unverifiable']['recall'])
        self.assertEqual(group['sample_agreement'], 0)

    def test_binary_labels_are_separate_from_factual_and_continuous(self):
        target = next(t for t in self.targets if t.group == 'source_evidence')
        self.store.append(self.decision(target, result=False))
        group = reliability_report(self.targets, self.store.read())['groups']['source_evidence']
        self.assertEqual(group['confusion_matrix']['False']['True'], 1)
        self.assertEqual(group['per_label']['False']['recall'], 0)
        with self.assertRaises(ValueError):
            self.store.append(self.decision(target, result='verified', previous=self.store.read()[-1].review_id))

    def test_extraction_missing_and_unexpected_preserved(self):
        target = next(t for t in self.targets if t.target_kind == 'extraction')
        self.store.append(self.decision(target, result={'facts':['고객은 2곳이다.', 'PoC 진행 중이다.'], 'kinds':['fact','fact']}))
        group = reliability_report(self.targets, self.store.read())['groups']['custom_extraction']
        diagnostic = group['extraction_diagnostics'][0]
        self.assertEqual(diagnostic['missing'], [['PoC 진행 중이다', 'fact']])
        self.assertEqual(diagnostic['recall'], 0.5)
        self.assertEqual(group['automatic_completion_rate'], 1)

    def test_stale_plan_and_source_changes_rejected(self):
        plan = review_plan(self.targets)
        plan['targets'][0]['source_sha256'] = 'c'*64
        with self.assertRaisesRegex(ValueError, 'plan source'):
            reliability_report(self.targets, [], plan=plan)
        decision = self.decision(self.targets[0])
        self.store.append(decision)
        altered = [t.model_copy(update={'source_sha256':'c'*64}) if t.target_id == decision.target_id else t for t in self.targets]
        with self.assertRaisesRegex(ValueError, 'source changed'):
            reliability_report(altered, self.store.read())

    def test_proposed_and_api_error_do_not_enter_confusion_matrix(self):
        target = self.targets[0]
        self.store.append(self.decision(target, status='proposed'))
        self.assertEqual(reliability_report(self.targets, self.store.read())['groups']['custom_factual']['paired'], 0)
        error_target = target.model_copy(update={'automatic_result': {'evaluation_status':'error','verdict':None}, 'automatic_label':None})
        review = self.decision(error_target, result='unverifiable')
        report = reliability_report([error_target], [review])['groups']['custom_factual']
        self.assertEqual(report['paired'], 0)
        self.assertEqual(report['statuses']['error'], 1)
        self.assertEqual(report['automatic_completion_rate'], 0)

    def test_symlink_ledger_is_rejected(self):
        self.store.ledger.parent.mkdir()
        self.store.ledger.symlink_to(self.directory / 'evaluation.json')
        with self.assertRaises(ValueError):
            self.store.append(self.decision(self.targets[0]))


class PairedReevaluationTests(unittest.TestCase):
    def test_same_new_version_both_sides_and_previous_results_preserved(self):
        if not hasattr(adapter_tests.RagasAdapterTests, 'llm'):
            adapter_tests.RagasAdapterTests.setUpClass()
        adapter = adapter_tests.RagasAdapterTests().adapter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            storage = EvaluationStorage(root)
            old = []
            for i in range(2):
                rec = RunRecorder(root=root, execution_path='agents', inputs={}, settings={},
                    requested_formats=['md'], repository=Path(__file__).resolve().parents[1])
                with rec.activate():
                    with rec.span('fixture_generation', {}) as span:
                        span['output'] = 'fixture'
                rec.finish(exc=ValueError('fixture'))
                sample = RagasSampleInput(sample_id='sample', evaluation_id='placeholder',
                    metric_name='factual_precision', response='PoC 진행 중', reference='PoC 진행 중', context_kind='none',
                    origin_invocation_id=rec.records[0].invocation_id)
                old.append(asyncio.run(evaluate_inputs(storage, rec.run_id, [sample], adapter,
                    dataset_version='synthetic', reference_sha256='a'*64,
                    input_sha256=rec.manifest.input_snapshot.sha256, evidence_sha256=rec.manifest.evidence_snapshot.sha256)))
            snapshots = [(d/'evaluation.json').read_bytes() for d in old]
            adapter.model = ModelSettings(provider='fixture', model='new-evaluator')
            report = asyncio.run(reevaluate_pair(*old, adapter, reason='new evaluator'))
            self.assertTrue(report['complete'])
            for d, content in zip(old, snapshots):
                self.assertEqual((d/'evaluation.json').read_bytes(), content)
            new = [Path(report['baseline_directory']), Path(report['after_directory'])]
            self.assertNotEqual(new[0], old[0])
            with self.assertRaisesRegex(ValueError, 'BOTH'):
                comparison(old[0], new[1])
            self.assertEqual(comparison(*new)['evaluator_configuration_sha256'], report['evaluator_configuration_sha256'])
            metrics = json.loads((new[0]/'metrics.json').read_text())
            self.assertIsNone(metrics['ragas_runtime']['cost'])
            self.assertEqual(metrics['ragas_runtime']['purpose'], 'ragas_evaluation')
            self.assertIsNone(metrics['generation_runtime'])


if __name__ == '__main__':
    unittest.main()
