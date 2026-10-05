"""#24 실제 원문/문맥/검색/인용 분리. 가상 한국어 오라클, 유료 호출 없음."""
import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path

from evaluation.dataset import canonical_bytes,sha256
from evaluation.grounding import (LangChainSupportJudge,check_support,citation_assessment,citation_targets,
    diagnostic_results,evaluate_grounding,grounding_assessment,prepare_grounding)
from evaluation.grounding_models import SupportComparison,SupportDocument,SupportProposal
from evaluation.models import ModelSettings,RagasSampleInput,SnapshotRef
from evaluation.recording import RunRecorder,company_scope
from evaluation.stage_samples import prepare_stage_package,ragas_inputs
from tests.test_evaluation_claims import LABELS,OracleExtractor
from tests.test_evaluation_factual import claim,sample

GOOD=LABELS['cases'][9]['text']
BAD=LABELS['cases'][8]['text']


class FixtureJudge:
    """좁은 기대값을 재생하는 시험용 오라클. 실제 모델 추론 정확도가 아니다."""
    def configuration(self):
        return {'policy_version':'actual-context-citation-v1','model_settings':{'provider':'fixture'},'sha256':'a'*64}
    def judge(self,claim,documents):
        rows=[]
        number=re.search(r'\d+곳',claim.atomic_statement)
        for doc in documents:
            other=re.search(r'\d+곳',doc.text)
            related='고객' in claim.atomic_statement and '고객' in doc.text
            relation='supported' if claim.atomic_statement in doc.text else ('contradicted' if related and number and other else 'unsupported')
            rows.append(SupportComparison(document_id=doc.document_id,relation=relation,same_entity=True,same_time=True,same_conditions=True,
                quote=doc.text if relation!='unsupported' else None,start=0 if relation!='unsupported' else None,
                end=len(doc.text) if relation!='unsupported' else None,claim_quantity_quote=number.group() if related and number and other else None,
                document_quantity_quote=other.group() if related and number and other else None,reason='가상 한국어 문장/고객 수 오라클'))
        return SupportProposal(comparisons=rows,reason='가상 원문만 검사'),{'fixture':True}


def document(text,kind='source_evidence',id='evidence_1',source='source_1'):
    return SupportDocument(document_id=id,kind=kind,text=text,sha256=sha256(text.encode()),source_id=source,
        evidence_ids=[id] if kind=='source_evidence' else [],snapshot=SnapshotRef(path='fixture',sha256=sha256(text.encode())))


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.judge=FixtureJudge()

    def recorder(self):
        rec=RunRecorder(root=self.root/'runs',execution_path='agents',inputs={'question':'고객 수 질문'},settings={},
            requested_formats=['md'],repository=Path(__file__).resolve().parents[1])
        rec.candidates=['FixtureChip']
        return rec

    def call(self,rec,text,context,operation='evaluate_business',company='FixtureChip'):
        with rec.span(operation,{'request':'고객 수 설명','context':context},kind='llm',company=company) as state:
            rec.delivered(context,state['record'])
            state['output']=text
        return state['record']

    def package(self,*,output=GOOD,context=GOOD,citation=None,intermediate=False,collected=GOOD):
        rec=self.recorder()
        with rec.activate():
            e=rec.add_evidence(collected,source='https://fixture.invalid/original',url='https://fixture.invalid/original',company='FixtureChip')
            if intermediate:
                self.call(rec,BAD,GOOD,operation='company_research')
            suffix=' [SOURCE:'+e.source_id+']'+(' https://fixture.invalid/original' if citation=='known_alias' else '') if citation in {'known','known_alias'} else ' https://fixture.invalid/missing' if citation=='missing' else ''
            target=self.call(rec,output+suffix,context)
        rec.finish(exc=ValueError('가상 run'))
        package=prepare_stage_package(rec.directory,OracleExtractor())
        return rec,package,target,e

    def test_true_claim_absent_from_actual_context_not_grounded(self):
        rec,p,target,e=self.package(context='제공 문맥에는 기업의 위치만 있다.')
        bundle=prepare_grounding(p,rec.directory)
        groundings,_,_=evaluate_grounding(bundle,self.judge,'eval_1')
        delivered=next(g for g in groundings if g.basis=='delivered_context')
        self.assertFalse(delivered.grounded)
        self.assertFalse(delivered.supporting_evidence_ids)
        self.assertNotIn(e.evidence_id,bundle['scopes'][p['samples'][0]['sample_id']]['source_document_ids'])

    def test_wrong_summary_faithful_but_original_evidence_contradicts(self):
        rec,p,target,e=self.package(output=BAD,context=BAD,intermediate=True)
        bundle=prepare_grounding(p,rec.directory)
        grounds,_,_=evaluate_grounding(bundle,self.judge,'eval_1')
        sid=next(s['sample_id'] for s in p['samples'] if s['invocation_id']==target.invocation_id and s['claim_ids'])
        delivered=next(g for g in grounds if g.sample_id==sid and g.basis=='delivered_context')
        original=next(g for g in grounds if g.sample_id==sid and g.basis=='source_evidence')
        self.assertTrue(delivered.grounded)
        self.assertFalse(original.grounded)
        self.assertEqual(original.effective_relations[e.evidence_id],'contradicted')
        self.assertEqual(original.source_locations[0].quote,GOOD)
        self.assertTrue(bundle['scopes'][sid]['upstream_invocation_ids'])

    def test_wrong_original_source_can_be_grounded_while_factually_contradicted(self):
        from evaluation.factual import assess_claim
        from evaluation.dataset import load_dataset
        from tests.test_evaluation_claims import ClaimsTests
        from tests.test_evaluation_factual import FixedJudge,comparison
        fixture=ClaimsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        dataset_dir,case=fixture.reviewed_dataset()
        dataset=load_dataset(dataset_dir,require_ready=True,allow_synthetic=True)
        rec,p,target,e=self.package(output=BAD,context=BAD,citation='known',collected=BAD)
        bundle=prepare_grounding(p,rec.directory)
        grounds,cites,_=evaluate_grounding(bundle,self.judge,'eval_1')
        self.assertTrue(all(g.grounded for g in grounds))
        self.assertEqual(cites[0].verdict,'supported')
        c=bundle['claims'][0]
        s=bundle['samples'][0]
        s.as_of_date=case.as_of_date
        fact=dataset.facts[0]
        from evaluation.factual_models import JudgmentProposal
        proposal=JudgmentProposal(comparisons=[comparison(fact,'contradicts',claim_quantity_quote='20곳',reference_quantity_quote='2곳')],reason='독립 기준 고객 2곳')
        result=assess_claim(c,s,dataset,FixedJudge(proposal),'eval_1')
        self.assertEqual(result.verdict,'contradicted')

    def test_citation_contradiction_inaccessible_and_uncited_denominators(self):
        rec,p,_,_=self.package(output=BAD,citation='known')
        _,cites,metrics=evaluate_grounding(prepare_grounding(p,rec.directory),self.judge,'eval_1')
        self.assertEqual(cites[0].verdict,'contradicted')
        self.assertEqual(metrics[0]['citations']['applicable_claim_source_pairs'],1)
        self.assertEqual(metrics[0]['citations']['support_rate'],0)
        rec,p,_,_=self.package(citation='missing')
        _,cites,metrics=evaluate_grounding(prepare_grounding(p,rec.directory),self.judge,'eval_1')
        self.assertEqual(cites[0].verdict,'inaccessible')
        self.assertEqual(metrics[0]['citations']['inaccessible'],1)
        rec,p,_,_=self.package()
        _,cites,metrics=evaluate_grounding(prepare_grounding(p,rec.directory),self.judge,'eval_1')
        self.assertEqual(cites,[])
        self.assertIsNone(metrics[0]['citations']['support_rate'])
        self.assertTrue(metrics[0]['uncited_claim_ids'])

    def test_original_support_proof_and_source_ids_saved(self):
        rec,p,_,e=self.package(citation='known')
        grounds,cites,metrics=evaluate_grounding(prepare_grounding(p,rec.directory),self.judge,'eval_1')
        source=next(g for g in grounds if g.basis=='source_evidence')
        self.assertTrue(source.grounded)
        self.assertEqual(source.supporting_evidence_ids,[e.evidence_id])
        self.assertEqual(source.source_locations[0].quote,GOOD)
        self.assertEqual(source.policy_version,'actual-context-citation-v1')
        self.assertEqual(cites[0].cited_source_id,e.source_id)
        self.assertEqual(cites[0].human_review_status,'pending')
        self.assertEqual(metrics[0]['citations']['support_rate'],1)

    def test_numbered_footnote_resolves_archived_source_without_fetching_url(self):
        rec=self.recorder()
        with rec.activate():
            e=rec.add_evidence(GOOD,source='https://fixture.invalid/original',url='https://fixture.invalid/original',company='FixtureChip')
            self.call(rec,GOOD+' [1]\n\n[1]: https://fixture.invalid/original',GOOD)
        rec.finish(exc=ValueError('가상 run'))
        p=prepare_stage_package(rec.directory,OracleExtractor())
        _,citations,metrics=evaluate_grounding(prepare_grounding(p,rec.directory),self.judge,'eval_1')
        self.assertEqual(len(citations),1)
        self.assertEqual(citations[0].cited_source_id,e.source_id)
        self.assertEqual(citations[0].verdict,'supported')
        self.assertEqual(metrics[0]['citations']['applicable_claim_source_pairs'],1)

    def test_actual_retrieval_query_rank_source_and_replacement_validation(self):
        from tests.test_evaluation_claims import ClaimsTests
        from langchain_core.documents import Document
        from evaluation.validation import validate_ragas_input
        fixture=ClaimsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        dataset_directory,case=fixture.reviewed_dataset()
        rec=self.recorder()
        rec.manifest.as_of_date=case.as_of_date
        query='생성 질문과 다른 실제 고객 검색 query'
        with rec.activate(),company_scope('FixtureChip'):
            with rec.span('search',{'query':query},kind='retrieval') as state:
                rec.retrieval(query,[Document(page_content='순위 1의 무관 자료',metadata={'source':'https://fixture.invalid/unrelated'}),
                    Document(page_content=GOOD,metadata={'source':'https://fixture.invalid/relevant'})],state['record'])
                state['output']='검색 결과'
                retrieval_id=state['record'].invocation_id
            target=self.call(rec,GOOD,GOOD)
            with company_scope('OtherChip'),rec.span('other_search',{},kind='retrieval') as state:
                rec.retrieval('다른 회사 query',[Document(page_content='다른 회사 자료',metadata={'source':'other'})],state['record'])
                state['output']='다른 검색 결과'
        rec.finish(exc=ValueError('가상 run'))
        p=prepare_stage_package(rec.directory,OracleExtractor(),dataset_directory=dataset_directory,
            case_bindings={target.invocation_id+'|FixtureChip|business':case.case_id},allow_synthetic=True)
        bundle=prepare_grounding(p,rec.directory)
        inputs=ragas_inputs(p)
        search=next(s for s in inputs if s.metric_name=='context_precision')
        faith=next(s for s in inputs if s.metric_name=='faithfulness')
        self.assertEqual(search.user_input,query)
        self.assertNotEqual(search.user_input,faith.user_input)
        self.assertIsNone(faith.reference)
        self.assertEqual(search.retrieved_contexts,['순위 1의 무관 자료',GOOD])
        self.assertEqual(validate_ragas_input(rec.directory,search),[])
        for changes in [{'user_input':'틀린 query'},{'retrieved_contexts':list(reversed(search.retrieved_contexts))},
                        {'evidence_ids':list(reversed(search.evidence_ids))},{'origin_invocation_id':target.invocation_id}]:
            self.assertTrue(validate_ragas_input(rec.directory,search.model_copy(update=changes)))
        diagnostics=diagnostic_results(bundle,inputs,[])
        actual=next(r for r in diagnostics if r['sample_id']==p['samples'][0]['sample_id'])
        self.assertEqual(len(actual['searches']),1)
        self.assertEqual(actual['searches'][0]['invocation_id'],retrieval_id)
        self.assertEqual(actual['searches'][0]['query'],query)
        self.assertEqual([c['rank'] for c in actual['searches'][0]['ranked_candidates']],[1,2])

    def test_corrupt_quote_url_only_and_missing_comparison_are_errors(self):
        d=document('FixtureChip 고객 자료 https://fixture.invalid/source')
        class Judge:
            def __init__(self,rows):self.rows=rows
            def judge(self,*args):return SupportProposal(comparisons=self.rows,reason='가상 악성 제안'),{'raw':'oracle'}
        rows=SupportComparison(document_id=d.document_id,relation='supported',same_entity=True,same_time=True,same_conditions=True,
            quote='없는 문장',start=0,end=5,reason='원문 아닌 quote')
        for items in [[],[rows],[rows,rows]]:
            with self.assertRaises(ValueError):check_support(claim(GOOD),[d],Judge(items))
        result=grounding_assessment(claim(GOOD),sample(),'source_evidence',[d],True,Judge([rows]),'eval_1')
        self.assertEqual(result.evaluation_status,'error')
        self.assertEqual(result.raw_output,{'raw':'oracle'})
        url='https://fixture.invalid/source'
        start=d.text.index(url)
        rows=rows.model_copy(update={'quote':url,'start':start,'end':start+len(url)})
        with self.assertRaisesRegex(ValueError,'URL/citation'):
            check_support(claim(GOOD),[d],Judge([rows]))

    def test_numeric_conditions_and_wrong_company_cannot_pass(self):
        d=document('FixtureChip 처리량은 2배다.')
        c=claim('FixtureChip 처리량은 2 TOPS다.',section='technology')
        rows=SupportComparison(document_id=d.document_id,relation='supported',same_entity=True,same_time=True,same_conditions=True,
            quote=d.text,start=0,end=len(d.text),claim_quantity_quote='2 TOPS',document_quantity_quote='2배',reason='잘못된 단위 제안')
        class Judge:
            def judge(self,*args):return SupportProposal(comparisons=[rows],reason='고정 비교'),{}
        self.assertEqual(check_support(c,[d],Judge())[0],'unsupported')
        rows.claim_quantity_quote=rows.document_quantity_quote=None
        rows.same_conditions=False
        self.assertEqual(check_support(c,[d],Judge())[0],'unsupported')
        d=document(GOOD)
        d.company_ids=['OtherChip']
        self.assertEqual(check_support(claim(GOOD),[d],self.judge)[0],'unsupported')
        global_text='전체 시장은 작년보다 성장했다.'
        d=document(global_text)
        d.company_ids=['OtherChip']
        c=claim(global_text).model_copy(update={'company_id':None,'section_id':'market'})
        self.assertEqual(check_support(c,[d],self.judge)[0],'supported')

    def test_conflicting_contexts_do_not_pass(self):
        relation,ids,_,conflict,_,_,_=check_support(claim(GOOD),[document(GOOD,id='one'),document(BAD,id='two')],self.judge)
        self.assertEqual(relation,'unsupported')
        self.assertEqual(ids,[])
        self.assertEqual(set(conflict),{'one','two'})

    def test_judge_failure_and_missing_input_never_become_completed_zero(self):
        class Failed:
            def judge(self,*args):raise TimeoutError('가상 timeout')
        result=grounding_assessment(claim(GOOD),sample(),'source_evidence',[document(GOOD)],True,Failed(),'eval_1')
        self.assertEqual(result.evaluation_status,'error')
        self.assertIsNone(result.grounded)
        result=grounding_assessment(claim(GOOD),sample(),'delivered_context',[],False,self.judge,'eval_1')
        self.assertEqual(result.evaluation_status,'missing')
        self.assertIsNone(result.grounded)

    def test_failed_generation_with_captured_prompt_is_saved_as_failed_not_empty_success(self):
        from evaluation.ragas_adapter import RagasAdapter,evaluate_inputs
        from evaluation.storage import EvaluationStorage
        from tests.test_evaluation_ragas_adapter import RagasAdapterTests
        rec=self.recorder()
        try:
            with rec.activate(),rec.span('evaluate_business',{'request':'고객 요약'},kind='llm',company='FixtureChip') as state:
                rec.delivered(GOOD,state['record'])
                raise ValueError('가상 generation failure')
        except ValueError as exc:
            rec.finish(exc=exc)
        p=prepare_stage_package(rec.directory,OracleExtractor())
        RagasAdapterTests.setUpClass()
        adapter=RagasAdapter(llm=RagasAdapterTests.llm,model=ModelSettings(provider='fixture',model='deterministic'))
        output=asyncio.run(evaluate_inputs(EvaluationStorage(rec.directory.parent),rec.run_id,[],adapter,
            dataset_version='no-reference-context-only-v1',reference_sha256='0'*64,input_sha256=rec.manifest.input_snapshot.sha256,
            evidence_sha256=rec.manifest.evidence_snapshot.sha256,stage_package=p,support_judge=self.judge))
        manifest=json.loads((output/'evaluation.json').read_text())
        metrics=json.loads((output/'grounding_metrics.json').read_text())['data']
        self.assertEqual(manifest['quality_evaluation_status'],'failed')
        self.assertEqual(metrics[0]['status'],'partial')
        self.assertIsNone(metrics[0]['grounding']['delivered_context']['grounded_rate'])
        self.assertTrue((output/'.frozen').exists())

    def test_duplicate_markers_same_source_count_once_and_ambiguous_binding_pending(self):
        rec,p,_,e=self.package(citation='known_alias')
        bundle=prepare_grounding(p,rec.directory)
        targets=citation_targets(bundle,bundle['claims'][0])
        self.assertEqual(len(targets),1)
        targets[0]['binding']='ambiguous'
        result=citation_assessment(bundle['claims'][0],bundle['samples'][0],targets[0],self.judge,'eval_1')
        self.assertEqual(result.evaluation_status,'missing')
        self.assertIsNone(result.verdict)

    def test_reference_never_becomes_faithfulness_context_and_tampered_input_rejected(self):
        rec,p,_,_=self.package(context='보고서 형식만 제공')
        p['samples'][0]['reference']=GOOD
        bundle=prepare_grounding(p,rec.directory)
        self.assertFalse(any(d.text==GOOD and d.kind!='source_evidence' for d in bundle['documents'].values()))
        grounds,_,_=evaluate_grounding(bundle,self.judge,'eval_1')
        self.assertFalse(next(g for g in grounds if g.basis=='delivered_context').grounded)
        p['samples'][0]['user_input']='몰래 추가한 정답'
        with self.assertRaisesRegex(ValueError,'actual input'):
            prepare_grounding(p,rec.directory)

    def test_received_function_input_matches_original_without_fake_llm_context(self):
        rec=self.recorder()
        with rec.activate():
            e=rec.add_evidence(GOOD,source='received-source',company='FixtureChip')
            with rec.span('evaluate_business',{'context':GOOD},kind='function',company='FixtureChip') as state:
                state['output']=GOOD
        rec.finish(exc=ValueError('가상 run'))
        p=prepare_stage_package(rec.directory,OracleExtractor())
        bundle=prepare_grounding(p,rec.directory)
        grounds,_,_=evaluate_grounding(bundle,self.judge,'eval_1')
        self.assertTrue(all(g.grounded for g in grounds))
        self.assertIn(e.evidence_id,bundle['scopes'][p['samples'][0]['sample_id']]['source_document_ids'])

    def test_global_report_tracks_only_actually_delivered_company_outputs(self):
        rec=self.recorder()
        with rec.activate():
            e=rec.add_evidence(GOOD,source='original',company='FixtureChip')
            first=self.call(rec,GOOD,GOOD)
            self.call(rec,BAD,'다른 입력',operation='evaluate_risk')
            final=self.call(rec,GOOD,GOOD,operation='generate_investment_report',company=None)
        rec.finish(exc=ValueError('가상 run'))
        context=next(c for c in rec.contexts if c.invocation_id==final.invocation_id)
        self.assertEqual(context.upstream_invocation_ids,[first.invocation_id])

    def test_structured_langchain_support_judge_records_schema_and_raw(self):
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda
        class Model:
            def with_structured_output(self,schema,include_raw):
                self.schema,self.include_raw=schema,include_raw
                return RunnableLambda(lambda messages:{'parsed':SupportProposal(comparisons=[],reason='자료 없음'),
                    'raw':AIMessage(content='가상 raw'),'parsing_error':None})
        m=Model()
        judge=LangChainSupportJudge(m,model_settings={'provider':'fixture'})
        p,raw=judge.judge(claim(GOOD),[])
        self.assertEqual(raw['content'],'가상 raw')
        self.assertEqual(m.schema,SupportProposal)
        self.assertTrue(m.include_raw)
        self.assertEqual(len(judge.configuration()['sha256']),64)

    def test_ragas_actual_search_rank_affects_precision_and_missing_evidence_recall(self):
        from ragas.llms.base import BaseRagasLLM
        from ragas.run_config import RunConfig
        from langchain_core.outputs import Generation,LLMResult
        from evaluation.ragas_adapter import RagasAdapter
        class RankedFixtureLLM(BaseRagasLLM):
            def is_finished(self,response):return True
            def generate_text(self,prompt,n=1,**kwargs):
                text=prompt.to_string()
                decoder=json.JSONDecoder()
                values=[]
                for i,char in enumerate(text):
                    if char=='{':
                        try:
                            value,_=decoder.raw_decode(text[i:])
                            if isinstance(value,dict) and 'question' in value and 'context' in value:values.append(value)
                        except ValueError:pass
                data=values[-1]
                relevant='관련 가상 근거' in data['context']
                payload={'classifications':[{'statement':GOOD,'reason':'가상 근거 확보','attributed':int(relevant)}]} if 'classifications' in text and 'attributed' in text else {'reason':'가상 query 관련성','verdict':int(relevant)}
                return LLMResult(generations=[[Generation(text=json.dumps(payload,ensure_ascii=False))] for _ in range(n)])
            async def agenerate_text(self,prompt,n=1,**kwargs):return self.generate_text(prompt,n=n,**kwargs)
        adapter=RagasAdapter(llm=RankedFixtureLLM(run_config=RunConfig(max_retries=1)),model=ModelSettings(provider='fixture',model='ranked-oracle'))
        def s(metric,contexts):
            return RagasSampleInput(sample_id='search',evaluation_id='eval_1',metric_name=metric,user_input='실제 고객 query',
                reference=GOOD,retrieved_contexts=contexts,context_kind='retrieved',origin_invocation_id='actual_search')
        low,_=asyncio.run(adapter.score(s('context_precision',['무관 가상 자료','관련 가상 근거: '+GOOD])))
        high,_=asyncio.run(adapter.score(s('context_precision',['관련 가상 근거: '+GOOD,'무관 가상 자료'])))
        missing,_=asyncio.run(adapter.score(s('context_recall',['무관 가상 자료'])))
        present,_=asyncio.run(adapter.score(s('context_recall',['관련 가상 근거: '+GOOD])))
        self.assertAlmostEqual(low.value,0.5,places=5, msg=low.reason)
        self.assertAlmostEqual(high.value,1,places=5, msg=high.reason)
        self.assertEqual(missing.value,0,missing.reason)
        self.assertEqual(present.value,1,present.reason)

    def test_full_evaluation_archives_context_citation_and_search_diagnostics(self):
        from evaluation.ragas_adapter import RagasAdapter,evaluate_inputs
        from evaluation.storage import EvaluationStorage
        from tests.test_evaluation_ragas_adapter import RagasAdapterTests
        RagasAdapterTests.setUpClass()
        rec,p,_,e=self.package(citation='known')
        originals={str(path):sha256(path.read_bytes()) for path in rec.directory.rglob('*') if path.is_file()}
        adapter=RagasAdapter(llm=RagasAdapterTests.llm,model=ModelSettings(provider='fixture',model='deterministic'))
        output=asyncio.run(evaluate_inputs(EvaluationStorage(rec.directory.parent),rec.run_id,ragas_inputs(p),adapter,
            dataset_version='no-reference-context-only-v1',reference_sha256='0'*64,input_sha256=rec.manifest.input_snapshot.sha256,
            evidence_sha256=rec.manifest.evidence_snapshot.sha256,stage_package=p,support_judge=self.judge))
        grounds=[json.loads(l) for l in (output/'grounding_assessments.jsonl').read_text().splitlines()]
        cites=[json.loads(l) for l in (output/'citation_assessments.jsonl').read_text().splitlines()]
        self.assertEqual(len(grounds),2)
        self.assertTrue(all(g['grounded'] and g['evaluation_id']==output.name for g in grounds))
        self.assertEqual(cites[0]['verdict'],'supported')
        proof=grounds[0]['source_locations'][0]
        self.assertEqual(Path(proof['snapshot']['path']).read_text()[proof['start_offset']:proof['end_offset']],proof['quote'])
        diag=json.loads((output/'grounding_diagnostics.jsonl').read_text().splitlines()[0])['data']
        self.assertEqual(diag['context_non_support_degree'],0)
        self.assertEqual(diag['searches'],[])
        manifest=json.loads((output/'evaluation.json').read_text())
        self.assertEqual(manifest['quality_evaluation_status'],'completed')
        self.assertIsNotNone(manifest['evaluator']['custom_grounding_configuration'])
        self.assertTrue((output/'.frozen').exists())
        self.assertTrue(all(sha256(Path(name).read_bytes())==digest for name,digest in originals.items()))

    def test_cli_reference_free_grounding_uses_explicit_absence_marker(self):
        import contextlib
        import io
        from unittest.mock import patch
        from evaluation.__main__ import main
        from evaluation.ragas_adapter import RagasAdapter
        from evaluation.stage_samples import write_stage_package
        from tests.test_evaluation_ragas_adapter import RagasAdapterTests
        rec,p,_,_=self.package(citation='known')
        archive=write_stage_package(p,self.root/'stage-package')
        RagasAdapterTests.setUpClass()
        adapter=RagasAdapter(llm=RagasAdapterTests.llm,model=ModelSettings(provider='fixture',model='deterministic'))
        capture=io.StringIO()
        with patch('sys.argv',['evaluation','evaluate',str(rec.directory),'--stage-package',str(archive),'--custom-grounding']),\
             patch('evaluation.ragas_adapter.RagasAdapter.openai',return_value=adapter),\
             patch('evaluation.grounding.LangChainSupportJudge.openai',return_value=self.judge),contextlib.redirect_stdout(capture):
            main()
        directory=Path(capture.getvalue().strip())
        manifest=json.loads((directory/'evaluation.json').read_text())
        self.assertEqual(manifest['dataset_version'],'no-reference-context-only-v1')
        self.assertEqual(manifest['reference_sha256'],sha256(canonical_bytes({'reference':None,'purpose':'generation_context_only'})))
        self.assertEqual(manifest['quality_evaluation_status'],'completed')
