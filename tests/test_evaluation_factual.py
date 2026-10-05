"""#23 가상 한국어 정책 회귀 및 실제 RAGAS API 연동. 실모델 정확도 시험이 아니다."""
import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from evaluation.claim_models import StageSample
from evaluation.dataset import canonical_bytes, publish_dataset, sha256
from evaluation.factual import (LangChainFactualJudge, apply_policy, assess_claim, counts_for,
    evaluate_custom, factual_pairs, numeric_relation, paired_results, quantity)
from evaluation.factual_models import FactComparison, JudgmentProposal
from evaluation.models import ClaimRecord, ModelSettings, RagasSampleInput, SnapshotRef, SourceLocation
from evaluation.seed_dataset import build_seed


def comparison(fact, relation='supports', **fields):
    return FactComparison(fact_id=fact.fact_id, relation=relation, same_entity=True,
        same_attribute=True, same_event=True, same_time=True, same_conditions=True,
        reason='한국어 가상 원문에 대한 고정 기대 비교', **fields)


def claim(statement, *, critical=True, section='business', claim_id='claim_1'):
    return ClaimRecord(claim_id=claim_id, run_id='run_fixture', evaluation_id='evaluation_fixture', sample_id='sample_fixture',
        invocation_id='inv_fixture', company_id='FixtureChip', section_id=section, atomic_statement=statement,
        claim_kind='fact', critical=critical, occurrences=[SourceLocation(snapshot=SnapshotRef(path='fixture', sha256='0'*64),
        start_offset=0, end_offset=len(statement), quote=statement)], extractor_version='fixture')


def sample():
    return StageSample(sample_id='sample_fixture', run_id='run_fixture', evaluation_id='evaluation_fixture', case_id='fixture_currency',
        company_id='FixtureChip', section_id='business', operation_id='evaluate_business', invocation_id='inv_fixture',
        attempt=1, stage='final', mode='end_to_end', as_of_date=date(2026,10,5), user_input='시험 입력',
        response='FixtureChip은 USD 640,000,000을 조달했다.', reference='독립 reference',
        logical_role='business', role_mapping_version='fixture', transformation_version='fixture')


class FixedJudge:
    """실제 모델을 흉내내는 정확도 주장이 아닌 명시적 판정 오라클 재생."""
    def __init__(self, proposal):
        self.proposal = proposal
        self.model_settings = {'provider':'fixture', 'model':'handwritten-proposal'}
    def configuration(self):
        return {'policy_version':'independent-three-state-v1', 'evaluator_version':'ko-reference-judge-v1',
                'model_settings':self.model_settings, 'sha256':sha256(canonical_bytes(self.model_settings))}
    def judge(self, claim, facts, sample):
        return self.proposal, {'fixture_proposal': self.proposal.model_dump(mode='json')}


class FactualTests(unittest.TestCase):
    def setUp(self):
        self.dataset, self.files = build_seed(synthetic=True)
        self.fact = self.dataset.facts[0]

    def proposal(self, selected):
        return JudgmentProposal(comparisons=selected, reason='한국어 가상 판정 이유')

    def test_currency_scale_and_interval_policy(self):
        self.assertEqual(quantity('6억 4천만 미국 달러'), (640000000, 'USD', 'eq'))
        examples = [('6억 4천만 달러','USD 640,000,000','supports'),
            ('6,400만 달러','USD 640,000,000','contradicts'),
            ('6억 4천만 원','USD 640,000,000','contradicts'),
            ('2 TOPS','2배','insufficient'), ('2명','2곳','insufficient'), ('약 2배','2배','insufficient'),
            ('USD 693,000,000','USD 693,000,000 초과','contradicts'),
            ('USD 700,000,000','USD 693,000,000 초과','insufficient'),
            ('USD 600,000,000 이상','USD 693,000,000 초과','supports'),
            ('USD 700,000,000 초과','USD 693,000,000 초과','insufficient')]
        for c,r,want in examples:
            with self.subTest(c=c,r=r):
                self.assertEqual(numeric_relation(c,r)[0],want)

    def test_wrong_numeric_is_contradiction_even_if_judge_suggests_support(self):
        c=claim('FixtureChip은 USD 64,000,000을 조달했다.')
        p=self.proposal([comparison(self.fact, claim_quantity_quote='USD 64,000,000', reference_quantity_quote='USD 640,000,000')])
        verdict, facts, conflict, reason=apply_policy(c,p,[self.fact])
        self.assertEqual(verdict,'contradicted')
        self.assertEqual(facts,[self.fact])
        self.assertFalse(conflict)
        self.assertIn('수치 불일치',reason)

    def test_company_period_benchmark_mismatches_are_not_false_contradictions(self):
        for field in ['same_entity','same_attribute','same_event','same_time','same_conditions']:
            comp=comparison(self.fact,'contradicts').model_copy(update={field:False})
            verdict,_,_,_=apply_policy(claim('FixtureChip의 성능은 모든 GPU 대비 2배다.'),self.proposal([comp]),[self.fact])
            self.assertEqual(verdict,'unverifiable',field)
        comp=comparison(self.fact,'supports').model_copy(update={'same_time':False,'time_mismatch_kind':'same_event_date_error'})
        self.assertEqual(apply_policy(claim('FixtureChip의 해당 라운드 날짜는 2025년이다.'),self.proposal([comp]),[self.fact])[0],'contradicted')

    def test_poc_to_paid_contract_and_unknown_not_zero(self):
        fact=next(f for f in self.dataset.facts if f.reference_id=='reference_fixture_poc_1')
        c=claim('FixtureChip은 유료 계약을 체결했다.',section='traction')
        self.assertEqual(apply_policy(c,self.proposal([comparison(fact,'contradicts')]),[fact])[0],'contradicted')
        unknown=next(f for f in self.dataset.facts if f.reference_id=='reference_fixture_unknown_2')
        self.assertEqual(apply_policy(claim('FixtureChip 유료 고객은 0명이다.'),
            self.proposal([comparison(unknown,'insufficient')]),[unknown])[0],'unverifiable')

    def test_conflicting_sources_do_not_choose_majority_or_latest(self):
        other=self.fact.model_copy(update={'fact_id':'fact_conflict','reference_id':'reference_conflict'})
        p=self.proposal([comparison(self.fact,claim_quantity_quote='USD 640,000,000',reference_quantity_quote='USD 640,000,000'),
                         comparison(other,'contradicts')])
        # 상태 충돌 예제를 수치와 독립적으로 구성한다.
        self.fact.value=other.value='Series A'
        p.comparisons[0].claim_quantity_quote=p.comparisons[0].reference_quantity_quote=None
        verdict,facts,conflict,reason=apply_policy(claim('FixtureChip 라운드는 Series A다.'),p,[self.fact,other])
        self.assertEqual(verdict,'unverifiable')
        self.assertEqual(set(conflict),{self.fact.fact_id,other.fact_id})
        self.assertEqual(len(facts),2)
        self.assertIn('출처 간',reason)

    def test_missing_in_reference_can_use_independent_additional_fact(self):
        extra=self.fact.model_copy(update={'fact_id':'additional_fact','reference_id':'additional_reference'})
        self.dataset.facts=[extra]
        p=self.proposal([comparison(extra,claim_quantity_quote='USD 640,000,000',reference_quantity_quote='USD 640,000,000')])
        s=sample()
        s.reference='FixtureChip은 Series A다.'
        result=assess_claim(claim('FixtureChip은 USD 640,000,000을 조달했다.'),s,self.dataset,FixedJudge(p),'evaluation_fixture')
        self.assertEqual(result.verdict,'verified')
        self.assertEqual(result.reference_ids,['additional_reference'])
        self.assertTrue(result.source_locations)

    def test_no_verified_fact_is_unverifiable_and_judge_failure_is_error(self):
        self.dataset.facts=[]
        result=assess_claim(claim('FixtureChip 매출은 100억 원이다.'),sample(),self.dataset,None,'evaluation_fixture')
        self.assertEqual(result.verdict,'unverifiable')
        self.assertEqual(result.evaluation_status,'completed')
        self.dataset.facts=[self.fact]
        class Failed(FixedJudge):
            def judge(self,*args):
                error=ValueError('가상 parse failure')
                error.raw_output={'raw':'invalid json'}
                raise error
        result=assess_claim(claim('FixtureChip 매출은 100억 원이다.'),sample(),self.dataset,Failed(None),'evaluation_fixture')
        self.assertEqual(result.evaluation_status,'error')
        self.assertIsNone(result.verdict)
        self.assertEqual(result.raw_output,{'raw':'invalid json'})
        summary=counts_for([claim('FixtureChip 매출은 100억 원이다.')],[result])
        self.assertEqual(summary['counts']['pending'],1)
        self.assertIsNone(summary['custom_verified_over_n'])

    def test_all_fact_comparisons_and_original_numeric_quotes_required(self):
        c=claim('FixtureChip은 USD 640,000,000을 조달했다.')
        cases=[[],[comparison(self.fact),comparison(self.fact)],
            [comparison(self.fact,claim_quantity_quote='USD 700,000,000',reference_quantity_quote='USD 640,000,000')],
            [comparison(self.fact)]]
        for rows in cases:
            with self.assertRaises(ValueError):
                apply_policy(c,self.proposal(rows),[self.fact])

    def test_pending_wrong_cutoff_and_future_sources_are_excluded(self):
        for updates in [{'review_status':'pending'},{'as_of_date':date(2024,1,1)}, {'source_as_of_date':date(2099,1,1)}]:
            self.dataset.facts=[self.fact.model_copy(update=updates)]
            a=assess_claim(claim('FixtureChip 설명'),sample(),self.dataset,None,'evaluation_fixture')
            self.assertEqual(a.verdict,'unverifiable')
            self.assertIn(self.fact.fact_id,a.excluded_fact_ids)

    def test_critical_errors_are_separate_and_incomplete_rates_are_null(self):
        from evaluation.factual_models import FactualAssessment
        a=FactualAssessment(evaluation_id='evaluation_fixture',evaluation_status='completed',claim_id='claim_1',
            verdict='unverifiable',reason='가상 근거 부족',sample_id='sample_fixture',case_id='fixture_currency',company_id='FixtureChip',
            section_id='business',critical=True,policy_version='fixture',evaluator_version='fixture',dataset_sha256='0'*64,as_of_date='2026-10-05')
        s=counts_for([claim('시험 주장')],[a])
        self.assertEqual(s['custom_unverifiable_over_n'],1)
        self.assertEqual(s['critical_unverifiable_claim_ids'],['claim_1'])
        s=counts_for([claim('시험 주장'),claim('미판정 주장',claim_id='claim_2')],[a])
        self.assertIsNone(s['custom_verified_over_n'])
        self.assertEqual(s['unjudged_claim_ids'],['claim_2'])

    def test_structured_langchain_judge_preserves_raw_and_prompt(self):
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda
        p=self.proposal([comparison(self.fact,'unrelated')])
        class Model:
            def with_structured_output(self,schema,include_raw):
                self.schema,self.include_raw=schema,include_raw
                return RunnableLambda(lambda messages:{'parsed':p,'raw':AIMessage(content='fixture raw'),'parsing_error':None})
        m=Model()
        judge=LangChainFactualJudge(m,model_settings={'provider':'fixture'})
        parsed,raw=judge.judge(claim('시험 주장'),[self.fact],sample())
        self.assertEqual(parsed,p)
        self.assertEqual(raw['content'],'fixture raw')
        self.assertTrue(m.include_raw)
        self.assertEqual(m.schema,JudgmentProposal)
        self.assertEqual(len(judge.configuration()['prompt_sha256']),64)

    def test_factual_modes_share_response_reference_and_keep_custom_key_separate(self):
        inputs=[RagasSampleInput(sample_id='sample_fixture',evaluation_id='evaluation_fixture',metric_name='factual_precision',
            response='답변',reference='기준',context_kind='none')]
        pairs=factual_pairs({},inputs)
        self.assertEqual(len(pairs),2)
        self.assertEqual(pairs[0].response,pairs[1].response)
        changed=pairs[1].model_copy(update={'reference':'다른 기준'})
        with self.assertRaisesRegex(ValueError,'same response/reference'):
            factual_pairs({},[pairs[0],changed])

    def test_custom_package_evaluation_preserves_stage_separation_and_failed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=publish_dataset(self.dataset,self.files,Path(tmp))
            c=claim('FixtureChip은 USD 640,000,000을 조달했다.')
            s=sample()
            s.claim_ids=[c.claim_id]
            before=StageSample.model_validate({**s.model_dump(mode='json'),'sample_id':'before','stage':'intermediate','claim_ids':[],
                                             'evaluation_status':'error','reason':'추출 오류'})
            package={'dataset':json.loads((directory/'dataset.json').read_text()),'samples':[s.model_dump(mode='json'),before.model_dump(mode='json')],
                     'claims':[c.model_dump(mode='json')]}
            class Judge(FixedJudge):
                def judge(self,claim,facts,sample):
                    rows=[comparison(f,'unrelated') for f in facts]
                    match=next(r for r in rows if r.fact_id=='fact_fixture_currency_1')
                    match.relation='supports'
                    match.claim_quantity_quote=match.reference_quantity_quote='USD 640,000,000'
                    return JudgmentProposal(comparisons=rows,reason='추가 독립 자료까지 검토'),{'fixture':True}
            assessments,summaries=evaluate_custom(package,directory,Judge(None),'evaluation_fixture',allow_synthetic=True)
            self.assertEqual(assessments[0].verdict,'verified')
            final=next(r for r in summaries if r['scope']=='stage' and r['stage']=='final')
            intermediate=next(r for r in summaries if r['scope']=='stage' and r['stage']=='intermediate')
            self.assertEqual(final['custom_verified_over_n'],1)
            self.assertEqual(intermediate['status'],'partial')
            self.assertFalse(intermediate['denominator_complete'])
            self.assertEqual(intermediate['incomplete_input_sample_ids'],['before'])
            self.assertIsNone(intermediate['custom_verified_over_n'])
            rows=paired_results(package,[],[],summaries)
            self.assertIsNone(rows[0]['ragas']['factual_precision']['result'])
            self.assertEqual(rows[0]['custom']['custom_verified_over_n'],1)
            package['dataset']['manifest']['sha256']='0'*64
            with self.assertRaisesRegex(ValueError,'differs'):
                evaluate_custom(package,directory,Judge(None),'evaluation_fixture',allow_synthetic=True)

    def test_equal_customer_count_does_not_erase_paid_contract_contradiction(self):
        fact=next(f for f in self.dataset.facts if f.reference_id=='reference_fixture_poc_2')
        c=claim('FixtureChip의 유료 계약 고객은 2곳이다.',section='traction')
        p=self.proposal([comparison(fact,'contradicts',claim_quantity_quote='2곳',reference_quantity_quote='2곳')])
        self.assertEqual(apply_policy(c,p,[fact])[0],'contradicted')

    def test_reference_lower_bound_cannot_be_omitted_from_quantity_quote(self):
        f=self.fact.model_copy(update={'statement':'FixtureChip은 USD 640,000,000 초과를 조달했다.',
                                     'conditions':['strictly_greater_than']})
        c=claim('FixtureChip은 USD 640,000,000을 조달했다.')
        p=self.proposal([comparison(f,claim_quantity_quote='USD 640,000,000',reference_quantity_quote='USD 640,000,000')])
        self.assertEqual(apply_policy(c,p,[f])[0],'contradicted')

    def test_conflicting_reference_values_both_refuting_claim_remain_unverifiable(self):
        a=self.fact.model_copy(update={'value':'Series A','statement':'FixtureChip 라운드는 Series A다.'})
        b=a.model_copy(update={'fact_id':'other_fact','reference_id':'other_ref','value':'Series B','statement':'FixtureChip 라운드는 Series B다.'})
        verdict,_,conflicts,_=apply_policy(claim('FixtureChip 라운드는 Series C다.'),
            self.proposal([comparison(a,'contradicts'),comparison(b,'contradicts')]),[a,b])
        self.assertEqual(verdict,'unverifiable')
        self.assertEqual(set(conflicts),{a.fact_id,b.fact_id})

    def test_real_ragas_two_modes_and_custom_assessments_are_saved_in_same_evaluation(self):
        from tests.test_evaluation_claims import ClaimsTests, LABELS
        from tests.test_evaluation_ragas_adapter import RagasAdapterTests
        from evaluation.ragas_adapter import RagasAdapter, evaluate_inputs
        from evaluation.stage_samples import prepare_stage_package, ragas_inputs
        from evaluation.storage import EvaluationStorage
        fixture=ClaimsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        directory,case=fixture.reviewed_dataset()
        rec=fixture.recorder()
        rec.manifest.as_of_date=case.as_of_date
        with rec.activate():
            target=fixture.call(rec,'evaluate_business',LABELS['cases'][9]['text'])
        rec.finish(exc=ValueError('가상 baseline'))
        package=prepare_stage_package(rec.directory,fixture.extractor,dataset_directory=directory,
            case_bindings={target.invocation_id+'|FixtureChip|business':case.case_id},allow_synthetic=True)
        fact=package['dataset']['facts'][0]
        judge=FixedJudge(JudgmentProposal(comparisons=[FactComparison(fact_id=fact['fact_id'],relation='supports',
            same_entity=True,same_attribute=True,same_event=True,same_time=True,same_conditions=True,
            claim_quantity_quote='2곳',reference_quantity_quote='2곳',reason='가상 고객 수 2곳 일치')],reason='한국어 오라클'))
        RagasAdapterTests.setUpClass()
        adapter=RagasAdapter(llm=RagasAdapterTests.llm,model=ModelSettings(provider='fixture',model='deterministic'))
        inputs=ragas_inputs(package)
        output=asyncio.run(evaluate_inputs(EvaluationStorage(rec.directory.parent),rec.run_id,inputs,adapter,
            dataset_version=package['dataset']['manifest']['version'],reference_sha256=package['dataset']['manifest']['sha256'],
            input_sha256=rec.manifest.input_snapshot.sha256,evidence_sha256=rec.manifest.evidence_snapshot.sha256,
            stage_package=package,factual_judge=judge,reference_dataset_directory=directory,allow_synthetic=True))
        assessments=[json.loads(l) for l in (output/'fact_assessments.jsonl').read_text().splitlines()]
        self.assertEqual(assessments[0]['verdict'],'verified')
        self.assertEqual(assessments[0]['evaluation_id'],output.name)
        rows=[json.loads(l) for l in (output/'factual_sample_results.jsonl').read_text().splitlines()]
        r=rows[0]['data']
        self.assertEqual(r['custom']['custom_verified_over_n'],1)
        for name in ['factual_precision','factual_recall']:
            self.assertEqual(r['ragas'][name]['status'],'completed')
            self.assertIsNotNone(r['ragas'][name]['result']['value'])
        self.assertEqual(r['ragas']['factual_precision']['response_sha256'],r['ragas']['factual_recall']['response_sha256'])
        self.assertEqual(r['ragas']['factual_precision']['reference_sha256'],r['ragas']['factual_recall']['reference_sha256'])
        self.assertTrue((output/'.frozen').exists())
        manifest=json.loads((output/'evaluation.json').read_text())
        self.assertIsNotNone(manifest['evaluator']['custom_factual_configuration'])
        sources=json.loads((output/'independent_sources.json').read_text())['data']
        self.assertEqual(Path(sources[0]['archived_snapshot']['path']).read_bytes(),
                         (directory/sources[0]['original_snapshot']['path']).read_bytes())
        class FailedJudge(FixedJudge):
            def judge(self,*args):
                raise TimeoutError('가상 judge timeout')
        failed_output=asyncio.run(evaluate_inputs(EvaluationStorage(rec.directory.parent),rec.run_id,inputs,adapter,
            dataset_version=package['dataset']['manifest']['version'],reference_sha256=package['dataset']['manifest']['sha256'],
            input_sha256=rec.manifest.input_snapshot.sha256,evidence_sha256=rec.manifest.evidence_snapshot.sha256,
            stage_package=package,factual_judge=FailedJudge(None),reference_dataset_directory=directory,allow_synthetic=True))
        failed_manifest=json.loads((failed_output/'evaluation.json').read_text())
        self.assertEqual(failed_manifest['quality_evaluation_status'],'failed')
        failed_rows=[json.loads(l)['data'] for l in (failed_output/'factual_sample_results.jsonl').read_text().splitlines()]
        self.assertIsNone(failed_rows[0]['custom']['custom_verified_over_n'])
        self.assertEqual(failed_rows[0]['custom']['counts']['pending'],1)
        self.assertTrue((failed_output/'.frozen').exists())
        original=(directory/sources[0]['original_snapshot']['path']).read_bytes()
        class EditingJudge(FixedJudge):
            def judge(self,*args):
                (directory/sources[0]['original_snapshot']['path']).write_bytes(b'changed after validated input was read')
                return super().judge(*args)
        archived_output=asyncio.run(evaluate_inputs(EvaluationStorage(rec.directory.parent),rec.run_id,inputs,adapter,
            dataset_version=package['dataset']['manifest']['version'],reference_sha256=package['dataset']['manifest']['sha256'],
            input_sha256=rec.manifest.input_snapshot.sha256,evidence_sha256=rec.manifest.evidence_snapshot.sha256,
            stage_package=package,factual_judge=EditingJudge(judge.proposal),reference_dataset_directory=directory,allow_synthetic=True))
        copied=json.loads((archived_output/'independent_sources.json').read_text())['data'][0]
        self.assertEqual(Path(copied['archived_snapshot']['path']).read_bytes(),original)
        self.assertEqual(copied['archived_snapshot']['sha256'],copied['original_snapshot']['sha256'])
