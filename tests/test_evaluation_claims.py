"""#22 원문/분해/중복/단계/전파/실제 RAGAS callback 연동. 유료 API 없음."""
import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path

from evaluation.claim_extraction import (EXTRACTOR_VERSION, PROMPT, LangChainClaimExtractor,
    candidate_location, json_units, markdown_units, normalize_statement, validate_candidate)
from evaluation.claim_models import AtomicCandidate, ExtractedAtoms, IsolatedContract, StageSample
from evaluation.dataset import canonical_bytes, publish_dataset, sha256
from evaluation.extraction_labels import compare_labels
from evaluation.models import ModelSettings, RagasSampleInput, SnapshotRef
from evaluation.ragas_claim_mapping import map_raw_claims
from evaluation.recording import RunRecorder
from evaluation.seed_dataset import build_seed
from evaluation.stage_samples import (ROLE_MAPPING_VERSION, load_stage_package, prepare_stage_package,
                                     ragas_inputs, write_stage_package)

LABELS = json.loads((Path(__file__).parent / 'fixtures/claim_labels_ko.json').read_text())


class OracleExtractor:
    version, prompt, model_settings = EXTRACTOR_VERSION, PROMPT, {'provider': 'fixture', 'model': 'oracle-replay'}
    def extract(self, unit, companies):
        atoms = []
        for case in LABELS['cases']:
            if case['text'] not in unit.text:
                continue
            for index, statement in enumerate(case['facts']):
                quote = case['quotes'][index]
                start = unit.text.index(quote)
                kind, category = case['kinds'][index], case['categories'][index]
                atoms.append(AtomicCandidate(statement=statement, quote=quote, start=start, end=start+len(quote),
                    company_id='FixtureChip', section_id='technology' if category == 'benchmark' else 'business',
                    kind=kind, category=category, relation_key=case['relations'][index],
                    numbers=re.findall(r'\d[\d,.]*(?:%|배|억|만|천)?', quote), units=[], time_conditions=[], comparison_conditions=[],
                    critical=category != 'other', citation_markers=[]))
        return ExtractedAtoms(atoms=atoms, no_claim_reason=None if atoms else '오라클 label에 평가 대상 서술 없음'), {'oracle_atoms': [a.model_dump(mode='json') for a in atoms]}


class ClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.extractor = OracleExtractor()

    def recorder(self, settings=None):
        rec = RunRecorder(root=self.root/'runs', execution_path='agents', inputs={'question': '고정 시험 입력'}, settings=settings or {},
                          requested_formats=['md'], repository=Path(__file__).resolve().parents[1])
        rec.candidates = ['FixtureChip', 'OtherChip']
        return rec

    def call(self, rec, operation, text, *, company='FixtureChip', upstream=None):
        with rec.span(operation, {'request': '평가하라', 'context': upstream or ''}, kind='llm', company=company) as state:
            rec.delivered(upstream or '고정 입력 문맥', state['record'])
            state['output'] = text
        return state['record']

    def frozen(self, *, polish=False):
        rec = self.recorder()
        with rec.activate():
            before = '# 보고서\n### FixtureChip\n#### Business\n' + LABELS['cases'][0]['text'] + '\n' + LABELS['cases'][1]['text'] + '\n'
            after = before + '| Company | Stage | Funding |\n|---|---|---|\n' + LABELS['cases'][4]['text'] + '\n'
            self.call(rec, 'evaluate_business', LABELS['cases'][0]['text'])
            self.call(rec, 'generate_investment_report', before, company=None, upstream=LABELS['cases'][0]['text'])
            if polish:
                self.call(rec, 'polish_report_to_korean', after, company=None, upstream=before)
            else:
                after = before
            file = self.root/'delivered.md'
            file.write_text(after, encoding='utf-8')
            rec.artifact(file, 'report_original.md', 'md')
        rec.finish(exc=ValueError('fixture run; 실제 보고서 성공 주장 없음'))
        return rec

    def test_handwritten_oracles_preserve_atomic_counts_kinds_and_numeric_conditions(self):
        for case in LABELS['cases']:
            ref = SnapshotRef(path='unused', sha256='0'*64)
            unit = next(markdown_units(case['text'], ref, ['FixtureChip'], company='FixtureChip', section='business'))
            result, _ = self.extractor.extract(unit, ['FixtureChip'])
            for atom in result.atoms:
                validate_candidate(unit, atom, ['FixtureChip'])
            comparison = compare_labels(case, [a.model_dump(mode='json') for a in result.atoms])
            self.assertEqual(comparison['missing'], [], case['id'])
            self.assertEqual(comparison['unexpected'], [], case['id'])
        self.assertFalse(LABELS['human_reviewed'])

    def test_label_comparison_detects_omission_wrong_split_and_kind(self):
        case = LABELS['cases'][0]
        result = compare_labels(case, [{'statement': case['facts'][0], 'kind': 'opinion'}])
        self.assertEqual(result['matched_count'], 0)
        self.assertEqual(len(result['missing']), 2)
        self.assertEqual(len(result['unexpected']), 1)

    def test_final_polished_artifact_and_body_table_duplicates_have_occurrences(self):
        rec = self.frozen(polish=True)
        before = {p: sha256(p.read_bytes()) for p in rec.directory.rglob('*') if p.is_file()}
        package = prepare_stage_package(rec.directory, self.extractor)
        final = [s for s in package['samples'] if s['stage'] == 'final' and s['company_id']=='FixtureChip']
        self.assertTrue(final)
        claims = [c for c in package['claims'] if c['sample_id'] in {s['sample_id'] for s in final}]
        funding = next(c for c in claims if c['atomic_statement']==LABELS['cases'][0]['facts'][0])
        self.assertEqual(len(funding['occurrences']), 2)
        self.assertFalse(any('투자 추천' in s['response'] for s in final if s['response']))
        self.assertTrue(any(a['candidate']['kind']=='decision' for a in package['atoms']))
        self.assertTrue(any(s['logical_role']=='report_writer' for s in package['samples']))
        self.assertTrue(any(s['logical_role']=='language_polish' for s in package['samples']))
        self.assertEqual(before, {p: sha256(p.read_bytes()) for p in rec.directory.rglob('*') if p.is_file()})
        out = write_stage_package(package, self.root/'package')
        self.assertEqual(load_stage_package(out, rec.directory)['run_id'], rec.run_id)

    def test_json_escaped_quote_surrogate_pairs_and_decimals_map_to_original(self):
        text = json.dumps({'data': '😀 '+LABELS['cases'][0]['text']}, ensure_ascii=True)
        ref = SnapshotRef(path='unused', sha256=sha256(text.encode()))
        unit = next(json_units(text, ref, section='business', company='FixtureChip', companies=['FixtureChip']))
        atoms, _ = self.extractor.extract(unit, ['FixtureChip'])
        for atom in atoms.atoms:
            location = candidate_location(unit, atom)
            self.assertEqual(text[location.start_offset:location.end_offset], location.quote)
            self.assertEqual(json.loads('"'+location.quote+'"'), atom.quote)
        self.assertNotEqual(normalize_statement('USD 1.25'), normalize_statement('USD 125'))

    def test_extraction_failure_is_error_and_never_silently_filtered(self):
        rec = self.frozen()
        class Failed(OracleExtractor):
            def extract(self, unit, companies):
                raise ValueError('fixture parse error')
        package = prepare_stage_package(rec.directory, Failed())
        self.assertTrue(any(s['evaluation_status']=='error' and s['response'] is None for s in package['samples']))
        self.assertTrue(any(a['status']=='error' and 'parse error' in a['error'] for a in package['extractions']))
        self.assertEqual(ragas_inputs(package), [])

    def test_invalid_numeric_company_and_position_are_rejected(self):
        case = LABELS['cases'][6]
        unit = next(markdown_units(case['text'], SnapshotRef(path='unused', sha256='0'*64), ['FixtureChip'], company='FixtureChip'))
        result, _ = self.extractor.extract(unit, ['FixtureChip'])
        atom = result.atoms[0]
        for update in [{'company_id':'OtherChip'}, {'start':0,'end':2}, {'statement':'FixtureChip은 USD 640,000,000을 조달했다.'}, {'statement':atom.statement+' 2035년에 이루어졌다.'}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_candidate(unit, atom.model_copy(update=update), ['FixtureChip'])

    def test_failure_missing_and_pure_routing_are_explicit(self):
        rec = self.recorder()
        with rec.activate():
            try:
                with rec.span('evaluate_technology', {}, company='FixtureChip'):
                    raise RuntimeError('fixture failure')
            except RuntimeError:
                pass
            self.call(rec, 'evaluate_team', None)
            self.call(rec, 'apply_investment_policy', 'top3')
        rec.finish(exc=ValueError('fixture'))
        package = prepare_stage_package(rec.directory, self.extractor)
        self.assertTrue(any(s['evaluation_status']=='error' for s in package['samples']))
        self.assertTrue(any(s['evaluation_status']=='missing' for s in package['samples']))
        self.assertTrue(any(c['logical_role']=='apply_investment_policy' and c['status']=='not_applicable' for c in package['coverage']))
        self.assertTrue(any(c['company_id']=='OtherChip' and c['logical_role']=='technology' and c['status']=='missing' for c in package['coverage']))

    def test_inherited_and_new_candidates_use_actual_delivered_upstream_only(self):
        rec = self.recorder()
        with rec.activate():
            upstream = LABELS['cases'][8]['text']
            first = self.call(rec, 'company_research', upstream)
            second = self.call(rec, 'evaluate_business', upstream+'\n'+LABELS['cases'][0]['text'], upstream=upstream)
            third = self.call(rec, 'evaluate_risk', upstream)  # 연결 없는 동일 문장
        rec.finish(exc=ValueError('fixture'))
        package = prepare_stage_package(rec.directory, self.extractor)
        linked = [c for c in package['claims'] if c['invocation_id']==second.invocation_id]
        self.assertTrue(any(c['error_origin_candidate']=='inherited' for c in linked))
        self.assertTrue(any(c['error_origin_candidate']=='new' for c in linked))
        unlinked = [c for c in package['claims'] if c['invocation_id']==third.invocation_id]
        self.assertTrue(all(c['error_origin_candidate']=='unknown' for c in unlinked))
        self.assertTrue(all(l['human_review_status']=='pending' and l['certainty']=='candidate_only' for l in package['claim_links']))

    def test_isolated_cannot_relabel_full_run(self):
        rec = self.frozen()
        with self.assertRaises(ValueError):
            prepare_stage_package(rec.directory, self.extractor, mode='isolated')

    def test_no_automatic_narrow_reference_and_actual_context_ragas_inputs(self):
        rec = self.frozen()
        package = prepare_stage_package(rec.directory, self.extractor)
        inputs = ragas_inputs(package)
        self.assertTrue(inputs)
        self.assertTrue(all(s.metric_name=='faithfulness' for s in inputs))
        self.assertTrue(all(s.reference is None for s in inputs))
        for sample in package['samples']:
            self.assertIn('factual_precision', sample['unsupported_metric_reasons'])

    def test_archive_tampering_and_response_edit_are_rejected(self):
        rec = self.frozen()
        package = prepare_stage_package(rec.directory, self.extractor)
        output = write_stage_package(package, self.root/'package')
        (output/'claims.jsonl').write_text('edited')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            load_stage_package(output, rec.directory)
        pending = next(s for s in package['samples'] if s['evaluation_status']=='pending')
        pending['response'] = '수정된 응답'
        output = write_stage_package(package, self.root/'edited')
        with self.assertRaisesRegex(ValueError, 'differs from custom claims'):
            load_stage_package(output, rec.directory)

    def test_langchain_structured_extractor_preserves_raw_and_parsing_error(self):
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda
        class Model:
            def with_structured_output(self, schema, include_raw):
                self.schema, self.include_raw = schema, include_raw
                return RunnableLambda(lambda messages: {'parsed': ExtractedAtoms(atoms=[], no_claim_reason='의견만 있음'),
                                                        'raw': AIMessage(content='{"atoms":[]}'), 'parsing_error': None})
        model = Model()
        extractor = LangChainClaimExtractor(model, model_settings={'provider':'fixture'})
        unit = next(markdown_units('투자가 유망할 것이다.', SnapshotRef(path='unused',sha256='0'*64), []))
        parsed, raw = extractor.extract(unit, [])
        self.assertEqual(parsed.no_claim_reason, '의견만 있음')
        self.assertTrue(model.include_raw)
        self.assertEqual(model.schema, ExtractedAtoms)
        self.assertIn('atoms', raw['content'])

    def reviewed_dataset(self):
        from evaluation.dataset import reference_payload
        dataset, files = build_seed(synthetic=True)
        case = dataset.manifest.cases[0]
        fact, source, item, review = dataset.facts[0], dataset.sources[0], dataset.required_items[0], dataset.case_reviews[0]
        dataset.manifest.cases, dataset.facts, dataset.sources = [case], [fact], [source]
        dataset.required_items, dataset.case_reviews = [item], [review]
        text = LABELS['cases'][9]['text']
        raw = text.encode()
        source.snapshot.sha256 = sha256(raw)
        fact.statement, fact.category, fact.value, fact.unit = text, 'customers', 2, 'companies'
        fact.source_locations[0].snapshot = source.snapshot
        fact.source_locations[0].quote, fact.source_locations[0].end_offset = text, len(text)
        fact.conditions = ['현재 자료에 명시된 고객 수']
        files[source.snapshot.path] = raw
        reference_raw = canonical_bytes(reference_payload(dataset, case.case_id))+b'\n'
        files[case.reference_snapshot.path] = reference_raw
        case.reference_snapshot.sha256 = sha256(reference_raw)
        files = {p:files[p] for p in [source.snapshot.path, case.evidence_snapshot.path, case.reference_snapshot.path]}
        return publish_dataset(dataset, files, self.root/'datasets'), case

    def test_corrected_claim_candidate_requires_actual_upstream_and_verified_reference(self):
        directory, case = self.reviewed_dataset()
        rec = self.recorder()
        rec.manifest.as_of_date = case.as_of_date
        with rec.activate():
            bad = self.call(rec, 'company_research', LABELS['cases'][8]['text'])
            corrected = self.call(rec, 'evaluate_business', LABELS['cases'][9]['text'], upstream=LABELS['cases'][8]['text'])
        rec.finish(exc=ValueError('fixture'))
        binding = {corrected.invocation_id+'|FixtureChip|business': case.case_id}
        package = prepare_stage_package(rec.directory, self.extractor, dataset_directory=directory, case_bindings=binding, allow_synthetic=True)
        claims = [c for c in package['claims'] if c['invocation_id']==corrected.invocation_id]
        self.assertEqual(claims[0]['error_origin_candidate'], 'corrected')
        self.assertTrue(claims[0]['upstream_claim_ids'])
        self.assertTrue(any(s.metric_name=='factual_precision' for s in ragas_inputs(package)))
        output = write_stage_package(package, self.root/'corrected-package')
        load_stage_package(output, rec.directory)

    def test_valid_isolated_mode_preserves_contract_and_rejects_evaluator_mismatch(self):
        directory, case = self.reviewed_dataset()
        from evaluation.dataset import load_dataset
        dataset = load_dataset(directory)
        rec = self.recorder({'evaluation_mode':'isolated', 'evaluator_configuration_sha256':'a'*64})
        rec.manifest.as_of_date = case.as_of_date
        with rec.activate():
            target = self.call(rec, 'evaluate_business', LABELS['cases'][9]['text'])
        rec.finish(exc=ValueError('fixture'))
        contract = IsolatedContract(input_sha256=rec.manifest.input_snapshot.sha256, evidence_sha256=rec.manifest.evidence_snapshot.sha256,
            reference_sha256=dataset.manifest.sha256, evaluator_configuration_sha256='a'*64,
            role_mapping_version=ROLE_MAPPING_VERSION, dataset_version=dataset.manifest.version, target_invocation_ids=[target.invocation_id])
        package = prepare_stage_package(rec.directory, self.extractor, dataset_directory=directory, mode='isolated', isolated_contract=contract,
            case_bindings={target.invocation_id+'|FixtureChip|business':case.case_id}, allow_synthetic=True)
        self.assertTrue(all(s['mode']=='isolated' for s in package['samples']))
        self.assertFalse(any(s['stage']=='final' for s in package['samples']))
        with self.assertRaisesRegex(ValueError,'evaluator configuration mismatch'):
            prepare_stage_package(rec.directory, self.extractor, dataset_directory=directory, mode='isolated',
                isolated_contract=contract.model_copy(update={'evaluator_configuration_sha256':'b'*64}), allow_synthetic=True)

    def test_additional_research_no_new_evidence_preserves_score_change_without_success(self):
        from evaluation.models import AdditionalResearchRecord
        rec = self.recorder()
        with rec.activate():
            before = self.call(rec, 'technical_eval', {'text':LABELS['cases'][5]['text'], 'score':3})
            research = self.call(rec, 'technical_additional_research', '새 조사 메모만 생성')
            after = self.call(rec, 'technical_eval', {'text':LABELS['cases'][5]['text'], 'score':4}, upstream=LABELS['cases'][5]['text'])
            rec.research.append(AdditionalResearchRecord(run_id=rec.run_id, company_id='FixtureChip', section_id='technology',
                invocation_id=research.invocation_id, trigger='score_below_4', questions=['벤치마크 원문 확보'], input_updated=True,
                state_update=research.output_snapshot, outcome='no_new_evidence', termination_reason='baseline_notes_only_then_recheck',
                policy_version=rec.manifest.policy_version, reevaluation_invocation_id=after.invocation_id))
        rec.finish(exc=ValueError('fixture'))
        package = prepare_stage_package(rec.directory, self.extractor)
        transition = package['research_transitions'][0]
        self.assertEqual(transition['evidence_outcome'], 'no_new_evidence')
        self.assertTrue(transition['score_changed'])
        self.assertEqual(transition['success_verdict'],'not_established')
        self.assertTrue(transition['before_sample_ids'])
        self.assertTrue(transition['after_sample_ids'])

    def test_actual_ragas_trace_maps_native_claims_without_reusing_custom_denominator(self):
        from tests.test_evaluation_ragas_adapter import RagasAdapterTests
        from evaluation.ragas_adapter import RagasAdapter
        from evaluation.models import ClaimRecord, SourceLocation
        RagasAdapterTests.setUpClass()
        adapter = RagasAdapter(llm=RagasAdapterTests.llm, model=ModelSettings(provider='fixture',model='deterministic'))
        statement = '기업 A는 PoC를 진행 중이다.'
        sample = RagasSampleInput(sample_id='sample_trace',evaluation_id='eval_trace',metric_name='factual_precision',
            user_input='기업 A 상태?',response=statement,reference='기업 A는 PoC 중이고 계약 미체결이다.',context_kind='none',origin_invocation_id='generation')
        _, trace = asyncio.run(adapter.score(sample))
        claim = ClaimRecord(claim_id='custom_1',run_id='run_1',evaluation_id='eval_trace',sample_id=sample.sample_id,
            invocation_id='generation',company_id='기업 A',section_id='business',atomic_statement=statement,claim_kind='fact',critical=True,
            occurrences=[SourceLocation(snapshot=SnapshotRef(path='fixture',sha256='0'*64),start_offset=0,end_offset=len(statement),quote=statement)],extractor_version=EXTRACTOR_VERSION)
        records = map_raw_claims(trace,sample,[claim])
        self.assertTrue(records)
        self.assertEqual(trace.mapping_status,'partial')
        self.assertTrue(any(r['custom_claim_ids']==['custom_1'] and r['original_locations'] for r in records))
        self.assertIsNone(trace.denominator)
        self.assertIsNone(trace.numerator)
        self.assertTrue(all(r['metric_name']=='factual_precision' for r in records))

    def test_stage_evaluator_saves_custom_samples_and_native_mapping(self):
        from tests.test_evaluation_ragas_adapter import RagasAdapterTests
        from evaluation.ragas_adapter import RagasAdapter, evaluate_inputs
        from evaluation.storage import EvaluationStorage
        RagasAdapterTests.setUpClass()
        rec = self.frozen()
        package = prepare_stage_package(rec.directory,self.extractor)
        adapter = RagasAdapter(llm=RagasAdapterTests.llm,model=ModelSettings(provider='fixture',model='deterministic'))
        samples = ragas_inputs(package)
        output = asyncio.run(evaluate_inputs(EvaluationStorage(rec.directory.parent),rec.run_id,samples,adapter,
            dataset_version='fixture-no-reference',reference_sha256='0'*64,input_sha256=rec.manifest.input_snapshot.sha256,
            evidence_sha256=rec.manifest.evidence_snapshot.sha256,stage_package=package))
        rows = [json.loads(l) for l in (output/'claims.jsonl').read_text().splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all(c['evaluation_id']==output.name for c in rows))
        self.assertTrue((output/'stage_samples.jsonl').exists())
        self.assertTrue((output/'stage_package.json').exists())
        self.assertTrue((output/'.frozen').exists())

    def test_saved_custom_and_metric_specific_native_label_comparison(self):
        from evaluation.label_cli import compare_package
        rec=self.frozen()
        package=prepare_stage_package(rec.directory,self.extractor)
        sample=next(s for s in package['samples'] if s['stage']=='final' and s['company_id']=='FixtureChip' and s['evaluation_status']=='pending')
        native=[{'sample_id':sample['sample_id'],'source':'response','metric_name':'factual_precision','attempt':1,'callback_run_id':'callback_1','statement':LABELS['cases'][0]['facts'][0]},
                {'sample_id':sample['sample_id'],'source':'response','metric_name':'faithfulness','attempt':1,'callback_run_id':'callback_2','statement':'오분해된 가상 사실'}]
        result=compare_package(LABELS,package,native)
        self.assertTrue(result['custom_units'])
        self.assertTrue(all(not u['missing'] and not u['unexpected'] for u in result['custom_units']))
        self.assertEqual(len(result['native_sets']),2)
        self.assertTrue(any(n['unexpected'] for n in result['native_sets']))
        self.assertFalse(result['human_reviewed'])

    def test_function_received_input_is_used_without_inventing_retrieval_or_llm_context(self):
        from evaluation.validation import validate_ragas_input
        rec=self.recorder()
        with rec.activate():
            with rec.span('evaluate_business',{'request':'고객 수 정리','context':LABELS['cases'][9]['text']},kind='function',company='FixtureChip') as state:
                state['output']=LABELS['cases'][9]['text']
        rec.finish(exc=ValueError('fixture'))
        package=prepare_stage_package(rec.directory,self.extractor)
        inputs=ragas_inputs(package)
        sample=next(s for s in inputs if s.metric_name=='faithfulness')
        self.assertEqual(sample.context_kind,'received_input')
        self.assertEqual(validate_ragas_input(rec.directory,sample),[])
        changed=sample.model_copy(update={'retrieved_contexts':['만들어낸 근거']})
        self.assertTrue(validate_ragas_input(rec.directory,changed))
        self.assertFalse(any(s.metric_name.value.startswith('context_') for s in inputs))

    def test_isolated_evaluator_config_mismatch_is_frozen_failure_without_scoring(self):
        from tests.test_evaluation_ragas_adapter import RagasAdapterTests
        from evaluation.dataset import load_dataset
        from evaluation.ragas_adapter import RagasAdapter, evaluate_inputs
        from evaluation.storage import EvaluationStorage
        directory,case=self.reviewed_dataset()
        dataset=load_dataset(directory)
        rec=self.recorder({'evaluation_mode':'isolated','evaluator_configuration_sha256':'a'*64})
        rec.manifest.as_of_date=case.as_of_date
        with rec.activate():
            target=self.call(rec,'evaluate_business',LABELS['cases'][9]['text'])
        rec.finish(exc=ValueError('fixture'))
        contract=IsolatedContract(input_sha256=rec.manifest.input_snapshot.sha256,evidence_sha256=rec.manifest.evidence_snapshot.sha256,
            reference_sha256=dataset.manifest.sha256,evaluator_configuration_sha256='a'*64,role_mapping_version=ROLE_MAPPING_VERSION,
            dataset_version=dataset.manifest.version,target_invocation_ids=[target.invocation_id])
        package=prepare_stage_package(rec.directory,self.extractor,dataset_directory=directory,mode='isolated',isolated_contract=contract,
            allow_synthetic=True,case_bindings={target.invocation_id+'|FixtureChip|business':case.case_id})
        RagasAdapterTests.setUpClass()
        adapter=RagasAdapter(llm=RagasAdapterTests.llm,model=ModelSettings(provider='fixture',model='deterministic'))
        with self.assertRaisesRegex(ValueError,'isolated evaluator configuration mismatch'):
            asyncio.run(evaluate_inputs(EvaluationStorage(rec.directory.parent),rec.run_id,ragas_inputs(package),adapter,
                dataset_version=dataset.manifest.version,reference_sha256=dataset.manifest.sha256,input_sha256=rec.manifest.input_snapshot.sha256,
                evidence_sha256=rec.manifest.evidence_snapshot.sha256,stage_package=package))
        evaluation=next((rec.directory/'evaluations').iterdir())
        manifest=json.loads((evaluation/'evaluation.json').read_text())
        self.assertEqual(manifest['quality_evaluation_status'],'failed')
        self.assertTrue((evaluation/'.frozen').exists())
        self.assertFalse((evaluation/'ragas_results.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
