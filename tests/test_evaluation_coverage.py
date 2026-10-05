"""#25 고정 제안 회귀. 유료 모델의 의미 판정 정확도 시험은 아니다."""
import asyncio
import json
import unittest

from evaluation.coverage import assess_item, evaluate_coverage, paired_coverage_results, summarize
from evaluation.coverage_models import ItemProposal
from evaluation.factual_models import FactualAssessment, FactComparison, JudgmentProposal
from evaluation.seed_dataset import build_seed
from tests.test_evaluation_factual import claim, sample, FixedJudge


class CoverageJudge:
    def __init__(self, proposal):
        self.proposal = proposal

    def configuration(self):
        return {'provider': 'fixture', 'policy_version': 'fixed-required-information-v1'}

    def judge(self, *args):
        return self.proposal, {'proposal': self.proposal.model_dump(mode='json')}


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.dataset, _ = build_seed(synthetic=True)
        self.s = sample()
        self.c = claim(self.s.response)
        self.s.claim_ids = [self.c.claim_id]
        self.fact = self.dataset.facts[0]
        self.item = self.dataset.required_items[0]
        self.a = FactualAssessment(evaluation_id='evaluation_fixture', claim_id=self.c.claim_id,
            sample_id=self.s.sample_id, case_id=self.s.case_id, company_id=self.c.company_id,
            section_id=self.c.section_id, critical=True, policy_version='fixture', evaluator_version='fixture',
            dataset_sha256=self.dataset.manifest.sha256, as_of_date=self.s.as_of_date.isoformat(),
            evaluation_status='completed', verdict='verified', reference_ids=[self.fact.reference_id],
            source_locations=self.fact.source_locations)
        self.p = ItemProposal(claim_ids=[self.c.claim_id], reference_ids=[self.fact.reference_id],
            quote=self.s.response, mentioned=True, accurate=True, explanation_complete=True,
            time_satisfied=True, evidence_satisfied=True, uncertainty_explicit=False,
            no_invented_value=True, reason='고정 정확성·시점·출처 규칙 충족 제안')

    def assess(self, **kwargs):
        values = dict(item=self.item, facts=[self.fact], claims=[self.c], assessments=[self.a],
                      sample=self.s, judge=CoverageJudge(self.p), evaluation_id='evaluation_fixture', dataset=self.dataset)
        values.update(kwargs)
        return assess_item(**values)

    def test_complete_and_linked_versions_counts(self):
        row = self.assess()
        self.assertEqual(row.verdict, 'fulfilled')
        self.assertEqual(row.fact_ids, [self.fact.fact_id])
        self.assertEqual(row.claim_ids, [self.c.claim_id])
        self.assertEqual(row.item_version, self.dataset.manifest.version)
        self.assertTrue(row.source_locations)
        result = summarize([row], [self.c])
        self.assertEqual((result['numerator'], result['denominator'], result['required_information_coverage']), (1, 1, 1))

    def test_keyword_only_missing_and_wrong_value_incorrect(self):
        self.p.explanation_complete = False
        self.assertEqual(self.assess().verdict, 'missing')
        self.a.verdict = 'contradicted'
        self.assertEqual(self.assess().verdict, 'incorrect')
        # 평가 제안이 모순 주장을 선택하지 않아도 독립 반증을 숨기지 못한다.
        self.p.claim_ids = []
        self.p.reference_ids = []
        row = self.assess()
        self.assertEqual(row.verdict, 'incorrect')
        self.assertEqual(row.claim_ids, [self.c.claim_id])

    def test_time_evidence_and_all_required_facts_mandatory(self):
        for field in ['time_satisfied', 'evidence_satisfied', 'explanation_complete']:
            p = self.p.model_copy(update={field: False})
            self.assertEqual(self.assess(judge=CoverageJudge(p)).verdict, 'missing')
        second = self.fact.model_copy(update={'fact_id': 'fact_second', 'reference_id': 'reference_second'})
        self.assertEqual(self.assess(facts=[self.fact, second]).verdict, 'missing')

    def test_unknown_unverifiable_claim_not_fulfilled_known_fact(self):
        self.a.verdict = 'unverifiable'
        self.assertEqual(self.assess().verdict, 'missing')

    def test_unknown_policy_uses_raw_original_even_no_factual_claims(self):
        self.s.response = ''
        self.s.raw_response = self.item.expected_unknown_response
        self.s.evaluation_status = 'not_applicable'
        p = self.p.model_copy(update={'claim_ids': [], 'reference_ids': [], 'quote': self.s.raw_response,
                                     'uncertainty_explicit': True})
        row = self.assess(facts=[], claims=[], assessments=[], judge=CoverageJudge(p))
        self.assertEqual(row.verdict, 'fulfilled')
        p.no_invented_value = False
        self.assertEqual(self.assess(facts=[], claims=[], assessments=[], judge=CoverageJudge(p)).verdict, 'incorrect')

    def test_not_applicable_only_fixed_item_and_unknown_stays_denominator(self):
        item = self.item.model_copy(update={'applicability': 'not_applicable', 'not_applicable_reason': '고정 범위 적용 제외'})
        row = self.assess(item=item, judge=None)
        self.assertEqual(row.verdict, 'not_applicable')
        self.assertEqual(summarize([row], [])['denominator'], 0)
        self.p.mentioned = False
        self.p.quote = ''
        self.p.claim_ids = self.p.reference_ids = []
        result = summarize([self.assess()], [])
        self.assertEqual((result['denominator'], result['numerator']), (1, 0))

    def test_invalid_quote_ids_and_timeout_are_error_not_zero(self):
        for fields in [{'quote': '원문에 없는 문장'}, {'claim_ids': ['foreign_claim']}, {'reference_ids': ['foreign_ref']}]:
            row = self.assess(judge=CoverageJudge(self.p.model_copy(update=fields)))
            self.assertEqual(row.evaluation_status, 'error')
            self.assertIsNotNone(row.raw_output)
            self.assertIsNone(summarize([row], [self.c])['required_information_coverage'])
        class Broken(CoverageJudge):
            def judge(self, *args):
                raise TimeoutError('fixture timeout')
        self.assertEqual(self.assess(judge=Broken(None)).evaluation_status, 'error')

    def test_deduplication_does_not_hide_conflicting_output(self):
        good = self.assess()
        self.a.verdict = 'contradicted'
        bad = self.assess()
        result = summarize([good, good, bad], [self.c, self.c])
        self.assertEqual(result['denominator'], 1)
        self.assertEqual(result['numerator'], 0)
        self.assertEqual(result['total_claim_count'], 1)

    def test_scopes_unbound_and_no_cross_company_fulfillment(self):
        package = {'dataset': self.dataset.model_dump(mode='json'), 'samples': [self.s.model_dump(mode='json')],
                   'claims': [self.c.model_dump(mode='json')]}
        rows, summaries = evaluate_coverage(package, self.dataset, CoverageJudge(self.p), [self.a], 'evaluation_fixture')
        self.assertEqual({s['scope'] for s in summaries}, {'sample', 'case', 'company', 'section', 'company_section', 'report'})
        self.assertEqual(rows[0].verdict, 'fulfilled')
        report = next(s for s in summaries if s['scope'] == 'report')
        self.assertEqual(report['denominator'], sum(i.applicability == 'applicable' for i in self.dataset.required_items))
        self.assertTrue(report['unrepresented_required_item_ids'])
        self.assertIsNone(report['required_information_coverage'])
        package['samples'][0]['case_id'] = 'unbound'
        _, summaries = evaluate_coverage(package, self.dataset, None, [], 'evaluation_fixture')
        self.assertTrue(all(s['status'] == 'partial' for s in summaries))
        package['samples'][0]['case_id'] = self.s.case_id
        package['claims'][0]['company_id'] = 'OtherChip'
        with self.assertRaisesRegex(ValueError, 'scope'):
            evaluate_coverage(package, self.dataset, None, [], 'evaluation_fixture')

    def test_high_recall_extra_facts_do_not_cover_missing_risk(self):
        good = self.assess()
        risk = good.model_copy(update={'required_item_id': 'required_risk', 'verdict': 'missing', 'claim_ids': []})
        result = summarize([good, risk], [self.c] + [self.c.model_copy(update={'claim_id': f'extra_{i}'}) for i in range(10)])
        self.assertEqual(result['required_information_coverage'], .5)
        self.assertEqual(result['missing_item_ids'], ['required_risk'])
        self.assertEqual(result['total_claim_count'], 11)
        package = {'samples': [self.s.model_dump(mode='json')], 'dataset': self.dataset.model_dump(mode='json')}
        from evaluation.models import RagasMetricResult
        recall = RagasMetricResult(evaluation_id='evaluation_fixture', sample_id=self.s.sample_id,
            metric_name='factual_recall', evaluation_status='completed', value=1)
        paired = list(paired_coverage_results(package, [recall], [{'scope': 'sample', 'key': self.s.sample_id, **result}]))[0]
        self.assertEqual(paired['factual_recall']['value'], 1)
        self.assertEqual(paired['required_information']['required_information_coverage'], .5)
        self.assertIsNone(paired['ragas_recall_denominator'])

    def test_original_table_quote_instead_of_normalized_statement(self):
        self.s.raw_response = '| 투자금 | USD 640,000,000 | [SOURCE:fixture] |'
        self.c.occurrences[0].quote = 'USD 640,000,000'
        self.p.quote = self.s.raw_response
        self.assertEqual(self.assess().verdict, 'fulfilled')

    def test_missing_generation_does_not_become_content_missing(self):
        self.s.evaluation_status = 'missing'
        self.s.reason = '생성 실패'
        row = self.assess(judge=None)
        self.assertEqual(row.evaluation_status, 'missing')
        self.assertIsNone(row.verdict)
        self.assertIsNone(summarize([row], [])['required_information_coverage'])

    def test_actual_ragas_archive_and_combined_configuration(self):
        from tests.test_evaluation_claims import ClaimsTests, LABELS
        from tests.test_evaluation_ragas_adapter import RagasAdapterTests
        from evaluation.ragas_adapter import RagasAdapter, evaluate_inputs
        from evaluation.stage_samples import prepare_stage_package, ragas_inputs
        from evaluation.models import ModelSettings
        from evaluation.storage import EvaluationStorage
        fixture = ClaimsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        directory, case = fixture.reviewed_dataset()
        rec = fixture.recorder()
        rec.manifest.as_of_date = case.as_of_date
        with rec.activate():
            target = fixture.call(rec, 'evaluate_business', LABELS['cases'][9]['text'])
        rec.finish(exc=ValueError('가상 baseline'))
        frozen_before = (rec.directory / 'run.json').read_bytes()
        package = prepare_stage_package(rec.directory, fixture.extractor, dataset_directory=directory,
            case_bindings={target.invocation_id + '|FixtureChip|business': case.case_id}, allow_synthetic=True)
        fact = package['dataset']['facts'][0]
        judge = FixedJudge(JudgmentProposal(comparisons=[FactComparison(fact_id=fact['fact_id'], relation='supports',
            same_entity=True, same_attribute=True, same_event=True, same_time=True, same_conditions=True,
            claim_quantity_quote='2곳', reference_quantity_quote='2곳', reason='고정 고객 수 일치')], reason='가상 판정'))
        class ReplayJudge(CoverageJudge):
            def judge(self, item, facts, claims, assessments, sample):
                p = ItemProposal(claim_ids=[c.claim_id for c in claims], reference_ids=[f.reference_id for f in facts],
                    quote=sample.raw_response, mentioned=True, accurate=True, explanation_complete=True,
                    time_satisfied=True, evidence_satisfied=True, uncertainty_explicit=False,
                    no_invented_value=True, reason='독립 판정과 연결된 고정 제안')
                return p, {'proposal': p.model_dump(mode='json')}
        RagasAdapterTests.setUpClass()
        adapter = RagasAdapter(llm=RagasAdapterTests.llm, model=ModelSettings(provider='fixture', model='deterministic'))
        params = dict(dataset_version=package['dataset']['manifest']['version'],
            reference_sha256=package['dataset']['manifest']['sha256'], input_sha256=rec.manifest.input_snapshot.sha256,
            evidence_sha256=rec.manifest.evidence_snapshot.sha256, stage_package=package,
            factual_judge=judge, reference_dataset_directory=directory, allow_synthetic=True)
        output = asyncio.run(evaluate_inputs(EvaluationStorage(rec.directory.parent), rec.run_id, ragas_inputs(package),
                                             adapter, coverage_judge=ReplayJudge(None), **params))
        rows = [json.loads(l) for l in (output / 'required_information_assessments.jsonl').read_text().splitlines()]
        self.assertEqual(rows[0]['verdict'], 'fulfilled')
        paired = json.loads((output / 'recall_coverage_results.jsonl').read_text().splitlines()[0])['data']
        self.assertEqual(paired['required_information']['required_information_coverage'], 1)
        self.assertIsNotNone(paired['factual_recall']['value'])
        manifest = json.loads((output / 'evaluation.json').read_text())
        self.assertIsNotNone(manifest['evaluator']['custom_coverage_configuration'])
        self.assertEqual(manifest['quality_evaluation_status'], 'completed')
        self.assertEqual((rec.directory / 'run.json').read_bytes(), frozen_before)
        self.assertTrue((output / '.frozen').is_file())
        class Broken(ReplayJudge):
            def judge(self, *args):
                raise TimeoutError('fixture timeout')
        failed = asyncio.run(evaluate_inputs(EvaluationStorage(rec.directory.parent), rec.run_id, ragas_inputs(package),
                                             adapter, coverage_judge=Broken(None), **params))
        self.assertEqual(json.loads((failed / 'evaluation.json').read_text())['quality_evaluation_status'], 'failed')
        self.assertTrue((failed / '.frozen').is_file())

    def test_all_fixed_cases_and_items_produce_complete_report(self):
        samples, claims, assessments = [], [], []
        for case in self.dataset.manifest.cases:
            facts = [f for f in self.dataset.facts if f.reference_id in case.reference_ids]
            s = self.s.model_copy(update={'sample_id': 'sample_' + case.case_id, 'case_id': case.case_id,
                'company_id': case.company_id, 'section_id': case.section_id, 'as_of_date': case.as_of_date,
                'response': '\n'.join(f.statement for f in facts), 'raw_response': '\n'.join(f.statement for f in facts),
                'reference_ids': case.reference_ids, 'claim_ids': ['claim_' + f.fact_id for f in facts]})
            samples.append(s.model_dump(mode='json'))
            for f in facts:
                c = self.c.model_copy(update={'claim_id': 'claim_' + f.fact_id, 'sample_id': s.sample_id,
                    'company_id': case.company_id, 'section_id': case.section_id, 'atomic_statement': f.statement,
                    'occurrences': [f.source_locations[0].model_copy(update={'quote': f.statement})]})
                claims.append(c.model_dump(mode='json'))
                assessments.append(self.a.model_copy(update={'claim_id': c.claim_id, 'sample_id': s.sample_id,
                    'case_id': case.case_id, 'company_id': case.company_id, 'section_id': case.section_id,
                    'reference_ids': [f.reference_id], 'source_locations': f.source_locations}))
        class AllFacts(CoverageJudge):
            def judge(self, item, facts, claims, assessments, sample):
                p = ItemProposal(claim_ids=['claim_' + f.fact_id for f in facts],
                    reference_ids=[f.reference_id for f in facts], quote='\n'.join(f.statement for f in facts),
                    mentioned=True, accurate=True, explanation_complete=True, time_satisfied=True,
                    evidence_satisfied=True, uncertainty_explicit=True, no_invented_value=True, reason='고정 전체 충족 제안')
                return p, {'fixture': True}
        package = {'samples': samples, 'claims': claims, 'dataset': self.dataset.model_dump(mode='json')}
        rows, summaries = evaluate_coverage(package, self.dataset, AllFacts(None), assessments, 'evaluation_fixture')
        self.assertEqual(len(rows), len(self.dataset.required_items))
        report = next(s for s in summaries if s['scope'] == 'report')
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['required_information_coverage'], 1)
        self.assertEqual(report['unrepresented_required_item_ids'], [])
        self.assertTrue(report['excluded_item_ids'])

    def test_structured_schema_and_cli_dependency(self):
        from unittest.mock import MagicMock, patch
        from evaluation.coverage import LangChainCoverageJudge
        model = MagicMock()
        model.with_structured_output.return_value.invoke.return_value = {'parsed': self.p, 'raw': {'fixture': True}}
        judge = LangChainCoverageJudge(model, model_settings={'provider': 'fixture'})
        proposal, raw = judge.judge(self.item, [self.fact], [self.c], [self.a], self.s)
        self.assertEqual(proposal.quote, self.p.quote)
        self.assertEqual(raw, {'fixture': True})
        model.with_structured_output.assert_called_once_with(ItemProposal, include_raw=True)
        self.assertIn('prompt_sha256', judge.configuration())
        from evaluation.__main__ import main
        import contextlib
        import io
        with patch('sys.argv', ['evaluation', 'evaluate', '/tmp/not-a-run', '--stage-package', '/tmp/not-a-package',
                                '--required-information']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                main()
            self.assertEqual(failure.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
