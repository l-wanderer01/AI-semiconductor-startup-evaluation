"""#29 paired integration and adversarial reporting; no paid API calls."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.experiment import SearchReplay, export_search
from evaluation.models import ModelSettings
from evaluation.paired import (aggregates, campaign_report, paired_report, preserve, runtime_groups,
                               verify_receipt, write_report)
from evaluation.paired_cli import evaluate_pair, run_experiment
from evaluation.ragas_adapter import RagasAdapter
from evaluation.stage_samples import prepare_stage_package, write_stage_package
from tests.test_evaluation_claims import ClaimsTests, LABELS
from tests.test_evaluation_coverage import CoverageJudge
from tests.test_evaluation_factual import FixedJudge
from evaluation.factual_models import JudgmentProposal, FactComparison
from evaluation.coverage_models import ItemProposal
from tests.test_evaluation_grounding import FixtureJudge
from tests.test_evaluation_ragas_adapter import RagasAdapterTests


class RequiredJudge(CoverageJudge):
    def judge(self, item, facts, claims, assessments, sample):
        p = ItemProposal(claim_ids=[c.claim_id for c in claims], reference_ids=[f.reference_id for f in facts],
            quote=sample.raw_response, mentioned=True, accurate=True, explanation_complete=True,
            time_satisfied=True, evidence_satisfied=True, uncertainty_explicit=False,
            no_invented_value=True, reason='synthetic deterministic proposal')
        return p, {'proposal': p.model_dump(mode='json')}


class PairedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RagasAdapterTests.setUpClass()

    def setUp(self):
        self.fixture = ClaimsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.dataset, self.case = self.fixture.reviewed_dataset()
        self.runs, self.packages = [], []
        for index in range(2):
            rec = self.fixture.recorder()
            rec.manifest.as_of_date = self.case.as_of_date
            with rec.activate():
                target = self.fixture.call(rec, 'evaluate_business', LABELS['cases'][9]['text'])
            rec.finish(exc=ValueError('synthetic incomplete workflow'))
            package = prepare_stage_package(rec.directory, self.fixture.extractor, dataset_directory=self.dataset,
                case_bindings={target.invocation_id + '|FixtureChip|business': self.case.case_id}, allow_synthetic=True)
            self.runs.append(rec.directory)
            self.packages.append(write_stage_package(package, self.root / f'package-{index}'))
        fact = package['dataset']['facts'][0]
        self.factual = FixedJudge(JudgmentProposal(comparisons=[FactComparison(fact_id=fact['fact_id'], relation='supports',
            same_entity=True, same_attribute=True, same_event=True, same_time=True, same_conditions=True,
            claim_quantity_quote='2곳', reference_quantity_quote='2곳', reason='synthetic equal count')], reason='fixture'))
        self.adapter = RagasAdapter(llm=RagasAdapterTests.llm, model=ModelSettings(provider='fixture', model='deterministic'))
        self.receipt = preserve(self.runs[0])

    def evaluate(self, name='attempt.json', **kwargs):
        return asyncio.run(evaluate_pair(self.runs, self.packages, self.dataset, self.adapter,
            output=self.root/name, receipt=self.receipt, factual_judge=self.factual,
            support_judge=FixtureJudge(), coverage_judge=RequiredJudge(None), allow_synthetic=True, **kwargs))

    def test_real_ragas_custom_pair_preserves_generation_and_workflow_failure(self):
        result = self.evaluate()
        self.assertEqual(result['status'], 'completed')
        report = paired_report(*result['evaluations'], data_mode='frozen', baseline_receipt=self.receipt)
        self.assertEqual(len(report['samples']), 1)
        sample = report['samples'][0]
        self.assertEqual(sample['pair_status'], 'paired')
        self.assertEqual(sample['baseline']['values']['claim_count'], 1)
        self.assertEqual(sample['difference']['required_information_coverage'], 0)
        self.assertFalse(report['quality']['overall_agent_success'])
        self.assertEqual(report['workflow']['baseline']['attempted_runs'], 1)
        self.assertEqual(report['workflow']['baseline']['success_rate'], 0)
        self.assertIsNone(report['aggregates'][0]['ragas_claim_weighted'])
        self.assertTrue(report['stage_coverage']['baseline'])
        output = write_report(report, self.root/'comparison')
        self.assertTrue((output/'comparison.csv').is_file())
        self.assertIn('rule_conformance', (output/'comparison.md').read_text())
        self.assertEqual(verify_receipt(self.receipt), self.runs[0])
        self.assertIsNone(report['generation_auxiliary']['baseline'][0]['p95_seconds'])

    def test_resume_creates_new_ids_both_sides_and_rejects_changed_settings(self):
        old = self.evaluate()
        old_bytes = [(Path(p)/'evaluation.json').read_bytes() for p in old['evaluations']]
        new = self.evaluate('resumed.json', resume=self.root/'attempt.json', reason='retry fixture')
        self.assertNotEqual(old['evaluations'], new['evaluations'])
        for old_dir, old_content, new_dir in zip(old['evaluations'], old_bytes, new['evaluations']):
            self.assertEqual((Path(old_dir)/'evaluation.json').read_bytes(), old_content)
            manifest = json.loads((Path(new_dir)/'evaluation.json').read_text())
            self.assertEqual(manifest['previous_evaluation_id'], Path(old_dir).name)
        self.adapter.model = ModelSettings(provider='fixture', model='changed')
        with self.assertRaisesRegex(ValueError, 'unchanged'):
            self.evaluate('bad.json', resume=self.root/'attempt.json', reason='changed evaluator')

    def test_receipt_detects_modified_or_added_generation_files(self):
        (self.runs[0]/'extra.txt').write_text('mutation')
        with self.assertRaisesRegex(ValueError, 'changed'):
            verify_receipt(self.receipt)

    def test_unpaired_and_zero_scores_are_visible(self):
        result = self.evaluate()
        after = Path(result['evaluations'][1])
        package = json.loads((after/'stage_package.json').read_text())
        package['data']['samples'][0]['logical_role'] = 'different_role'
        (after/'stage_package.json').write_text(json.dumps(package))
        report = paired_report(*result['evaluations'], data_mode='frozen', baseline_receipt=self.receipt)
        self.assertEqual([r['pair_status'] for r in report['samples']], ['unpaired', 'unpaired'])
        self.assertFalse(report['quality']['complete'])
        for row in report['samples']:
            self.assertTrue(all(v is None for v in row['difference'].values()))

    def test_different_references_rejected(self):
        result = self.evaluate()
        after = Path(result['evaluations'][1])
        manifest = json.loads((after/'evaluation.json').read_text())
        manifest['reference_sha256'] = 'a'*64
        (after/'evaluation.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'reference'):
            paired_report(*result['evaluations'], data_mode='live')

    def test_failure_journal_and_resume_after_left_failure(self):
        with patch('evaluation.ragas_adapter.evaluate_inputs', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                self.evaluate()
        journal = json.loads((self.root/'attempt.json').read_text())
        self.assertEqual(journal['status'], 'failed')
        self.assertEqual(journal['evaluations'], [])
        resumed = self.evaluate('resume-failed.json', resume=self.root/'attempt.json', reason='restart failed attempt')
        self.assertEqual(resumed['status'], 'completed')

    def test_campaign_includes_missing_evaluations_and_rejects_fake_repetitions(self):
        receipt_path = self.root/'receipt.json'
        receipt_path.write_text(json.dumps(self.receipt))
        attempt = {'attempt_id':'one', 'data_mode':'frozen', 'baseline_run':str(self.runs[0]),
                   'after_run':str(self.runs[1]), 'baseline_receipt':str(receipt_path), 'reason':'generation workflow failed'}
        report = campaign_report([attempt])
        group = report['groups']['frozen']
        self.assertEqual(group['evaluated_pairs'], 0)
        self.assertEqual(group['workflow']['after']['attempted_runs'], 1)
        self.assertEqual(group['workflow']['after']['success_rate'], 0)
        with self.assertRaisesRegex(ValueError, 'distinct generation'):
            campaign_report([attempt, {**attempt, 'attempt_id':'two'}])

    def test_harness_converts_paths_and_records_role(self):
        from types import SimpleNamespace
        observed = []
        def runner(input: Path):
            observed.append(input)
            rec = self.fixture.recorder()
            rec.finish(exc=ValueError('synthetic run'))
        with patch('evaluation.paired_cli.importlib.import_module', return_value=SimpleNamespace(main=runner)):
            result = run_experiment('fixture:main', {'input':'fixture.json'}, role='refactored',
                                    data_mode='live', receipt=self.receipt)
        self.assertIsInstance(observed[0], Path)
        run = Path(result['run_directories'][0])
        self.assertEqual(json.loads((run/'run.json').read_text())['comparison_role'], 'refactored')
        self.assertEqual(json.loads((run/'experiment.json').read_text())['data']['data_mode'], 'live')

    def test_zero_and_errors_remain_separate(self):
        from evaluation.paired import sample_values
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            rows = [{'sample_id':'s', 'metric_name':'factual_precision', 'evaluation_status':'completed','value':0},
                    {'sample_id':'s', 'metric_name':'factual_recall', 'evaluation_status':'error','value':None}]
            (d/'ragas_results.jsonl').write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
            values = sample_values(d, {'sample_id':'s'})
            self.assertEqual(values['values']['factual_precision'], 0)
            self.assertIsNone(values['values']['factual_recall'])
            self.assertEqual(values['statuses']['factual_recall'], 'error')

    def test_p95_only_at_twenty_observations_and_unknown_cost_not_averaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=[]
            for i in range(20):
                d=Path(tmp)/str(i)
                d.mkdir()
                (d/'run.json').write_text(json.dumps({'commit_sha':'fixture','settings_sha256':'fixture','execution_path':'agents','fallback_used':False}))
                (d/'metrics.json').write_text(json.dumps({'generation_runtime':{'status':'succeeded', 'cache_state':'warm',
                    'currency':'USD','usage':[],'duration_seconds':i+1,'cost':None if i == 0 else 0}}))
                paths.append(d)
            self.assertIsNone(runtime_groups(paths[:19])[0]['p95_seconds'])
            group=runtime_groups(paths)[0]
            self.assertEqual(group['p95_seconds'],19)
            self.assertEqual(group['median_seconds'],10.5)
            self.assertEqual(group['known_cost_count'],19)
            self.assertIsNone(group['median_cost'])

    def test_isolated_actual_fixed_agent_execution_with_run_specific_evidence_indexes(self):
        from evaluation.claim_models import IsolatedContract
        from evaluation.paired_cli import evaluator_fingerprint
        from evaluation.stage_samples import ROLE_MAPPING_VERSION
        from evaluation.dataset import load_dataset
        dataset = load_dataset(self.dataset, allow_synthetic=True)
        fingerprint = evaluator_fingerprint(self.adapter, self.factual, FixtureJudge(), RequiredJudge(None))
        self.runs, self.packages = [], []
        for index in range(2):
            rec = self.fixture.recorder(settings={'evaluation_mode':'isolated', 'evaluator_configuration_sha256':fingerprint})
            rec.manifest.as_of_date = self.case.as_of_date
            with rec.activate():
                rec.add_evidence(LABELS['cases'][9]['text'], source='https://fixture.invalid', company='FixtureChip')
                target = self.fixture.call(rec, 'evaluate_business', LABELS['cases'][9]['text'])
            rec.finish(exc=ValueError('synthetic single agent, not full workflow'))
            contract = IsolatedContract(input_sha256=rec.manifest.input_snapshot.sha256,
                evidence_sha256=rec.manifest.evidence_snapshot.sha256, reference_sha256=dataset.manifest.sha256,
                evaluator_configuration_sha256=fingerprint, role_mapping_version=ROLE_MAPPING_VERSION,
                dataset_version=dataset.manifest.version,
                target_invocation_ids=[target.invocation_id])
            package = prepare_stage_package(rec.directory, self.fixture.extractor, dataset_directory=self.dataset,
                mode='isolated', isolated_contract=contract, allow_synthetic=True,
                case_bindings={target.invocation_id+'|FixtureChip|business':self.case.case_id})
            self.runs.append(rec.directory)
            self.packages.append(write_stage_package(package,self.root/f'isolated-package-{index}'))
        self.receipt=preserve(self.runs[0])
        result=self.evaluate('isolated.json')
        report=paired_report(*result['evaluations'],data_mode='frozen',baseline_receipt=self.receipt)
        self.assertEqual(report['scope']['mode'],'isolated')
        self.assertTrue(report['scope']['input_or_evidence_changed'])
        self.assertFalse(report['scope']['source_evidence_changed'])
        self.assertFalse(report['samples'][0]['input_changed'])


class ReplayTests(unittest.TestCase):
    def test_strict_injection_without_credentials_no_network_fallthrough(self):
        from investment_pipeline.tavily import TavilySearchClient
        replay = SearchReplay({'schema_version':'search-replay-v1', 'entries':[
            {'operation':'TavilySearchClient_search', 'query':'fixture query', 'args':[],
             'options':{'category':'technology'}, 'results':[]} ]})
        with patch('investment_pipeline.tavily.requests.post', side_effect=AssertionError('network')) as post:
            with replay.activate():
                self.assertEqual(TavilySearchClient(None).search('fixture query', category='technology'), [])
                with self.assertRaisesRegex(ValueError, 'missing exact'):
                    TavilySearchClient(None).search('unexpected', category='technology')
            post.assert_not_called()

    def test_duplicate_keys_and_missing_receipt_rejected(self):
        row = {'operation':'search', 'query':'q', 'args':[], 'options':{}, 'results':[]}
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            SearchReplay({'schema_version':'search-replay-v1', 'entries':[row,row]})
        with self.assertRaisesRegex(ValueError, 'receipt'):
            run_experiment('no_module:main', {}, role='refactored', data_mode='live')
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            run_experiment('no_module:main', {}, role='baseline', data_mode='frozen')

    def test_micro_and_company_macro_differ_and_null_denominator(self):
        records=[]
        for company, n, score in [('A',100,1), ('B',1,0)]:
            sample={'values':dict.fromkeys(('factual_precision','factual_recall','faithfulness','context_precision','context_recall'), score),
                    'statuses':dict.fromkeys(('factual_precision','factual_recall','faithfulness','context_precision','context_recall','custom_factual'), 'completed'),
                    'counts':{'total':n,'verified':n if score else 0,'contradicted':0 if score else n,'unverifiable':0,'pending':0}}
            records.append({'key':['case','role',company,'tech','final','end_to_end',1], 'baseline':sample,'after':sample})
        aggregate=aggregates(records)[0]
        self.assertEqual(aggregate['company_section_macro']['factual_precision'], .5)
        self.assertAlmostEqual(aggregate['custom_claim_weighted']['verified'],100/101)
        self.assertTrue(aggregate['claim_concentration_warning'])
        records[1]['baseline']['statuses']['custom_factual']='partial'
        self.assertIsNone(aggregates(records)[0]['custom_claim_weighted']['verified'])


if __name__ == '__main__':
    unittest.main()
