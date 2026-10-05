"""실제 Agent API 없이 기록 연결과 실패·coverage·동결을 검증한다."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.documents import Document

from evaluation.models import RunManifest
from evaluation.recording import ObservedRunnable, RunRecorder, traced
from evaluation.validation import validate_run

REPOSITORY = Path(__file__).resolve().parents[1]


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def recorder(self, path='agents', formats=None):
        return RunRecorder(root=self.root / 'runs', execution_path=path, inputs={'request': 'test'},
                           settings={'model': 'fixture', 'llm_enabled': False},
                           requested_formats=formats or ['md'], repository=REPOSITORY)

    def test_failure_preserves_parent_trace_and_freezes(self):
        rec = self.recorder()
        @traced('child')
        def child():
            raise ValueError('injected failure')
        @traced('parent')
        def parent():
            child()
        with self.assertRaises(ValueError):
            try:
                with rec.activate():
                    parent()
            except ValueError as exc:
                rec.finish(exc=exc)
                raise
        self.assertEqual(validate_run(rec.directory), [])
        records = rec.records
        self.assertEqual(records[0].parent_invocation_id, records[1].invocation_id)
        self.assertTrue((rec.directory / '.frozen').exists())
        self.assertEqual(rec.manifest.generation_status, 'failed')

    def test_bad_route_missing_company_and_failed_artifact_are_not_success(self):
        rec = self.recorder('investment_pipeline')
        rec.candidates = ['A', 'B']
        with rec.activate():
            with rec.span('investment_supervisor', {}, kind='node', company='A') as span:
                span['output'] = {'supervisor_route_state': 'invalid'}
        rec.artifact(self.root / 'missing.md', 'report_original.md', 'md')
        rec.finish(result={'branch': 'top3', 'report_markdown': 'report', 'decisions': []})
        checks = json.loads((rec.directory / 'checks.json').read_text())
        self.assertTrue(any(c['check_id'].startswith('route_') and c['status'] == 'fail' for c in checks))
        self.assertTrue(any(c['check_id'] == 'decision_company_coverage' and c['status'] == 'fail' for c in checks))
        self.assertFalse(rec.manifest.execution_succeeded)

    def test_retrieval_and_actual_prompt_are_separate_and_secret_redacted(self):
        rec = self.recorder()
        docs = [Document(page_content='기업 A는 PoC 진행 중이다.', metadata={'source': 'source.md'})]
        retrieval = ObservedRunnable(SimpleNamespace(invoke=lambda q: docs), kind='retrieval', name='search')
        llm = ObservedRunnable(SimpleNamespace(invoke=lambda p: 'output'), kind='llm', name='llm')
        with rec.activate():
            retrieval.invoke('query')
            llm.invoke('실제 문맥: 기업 A는 PoC 진행 중이다. Bearer hidden-secret')
        rec.finish(exc=ValueError('fixture'))
        self.assertEqual(validate_run(rec.directory), [])
        self.assertEqual(rec.retrievals[0].candidates[0].rank, 1)
        self.assertEqual(rec.contexts[0].evidence_ids, [next(iter(rec.evidence))])
        self.assertNotIn('hidden-secret', (rec.directory / 'delivered_contexts.jsonl').read_text())
        self.assertIsNone(rec.retrievals[0].company_id)

    def test_tampered_snapshot_and_unknown_lineage_are_detected(self):
        rec = self.recorder()
        with rec.activate():
            with rec.span('fixture', {}) as span:
                span['output'] = 'output'
        rec.finish(exc=ValueError('fixture'))
        Path(rec.records[0].input_snapshot.path).write_text('tampered')
        self.assertTrue(any('hash mismatch' in e for e in validate_run(rec.directory)))

    def test_service_real_graph_with_fake_llm_preserves_result(self):
        from agents.service import InvestmentAnalysisService
        from agents.models import ServiceConfig
        config = ServiceConfig()
        class FakeLLM:
            def __init__(self, schema=None):
                self.schema = schema
            def with_structured_output(self, schema):
                return FakeLLM(schema)
            def invoke(self, messages):
                text = '\n'.join(m.content for m in messages)
                if self.schema is None:
                    return SimpleNamespace(content='한국어 fixture 보고서와 시장 분석')
                if self.schema.__name__ == 'CompanyList':
                    return self.schema(companies=['A', 'B'])
                company = re.search(r'회사명: ([^\n]+)', text).group(1)
                if self.schema.__name__ == 'AgentEvaluation':
                    return self.schema(company_name=company, score=3, rationale='근거 검토', strengths=[],
                                       risks=[], diligence_questions=[])
                return self.schema(company_name=company, thesis='검토', strengths=[], risks=[], diligence_questions=[],
                                   **{key: 3 for key in ['technology_score','market_score','business_score',
                                                       'team_score','risk_score','competition_score']})
        docs = [Document(page_content='A와 B 기업의 투자 검토 자료', metadata={'file_name': 'source.md', 'source': 'source.md'})]
        (self.root / 'source.md').write_text(docs[0].page_content)
        service = InvestmentAnalysisService.__new__(InvestmentAnalysisService)
        service.base_dir, service.config = self.root, config
        service.output_dir = self.root / 'outputs'
        service.output_dir.mkdir()
        service.prompt_dir = REPOSITORY / 'prompts'
        service.source_files, service.documents = [self.root / 'source.md'], docs
        service._build_prompts()
        service.llm = ObservedRunnable(FakeLLM(), kind='llm', name='service_llm')
        service.retriever = ObservedRunnable(SimpleNamespace(invoke=lambda q: docs), kind='retrieval', name='service_retrieval')
        service._evaluation_recorder = self.recorder(formats=['md', 'json'])
        result = service.run()
        self.assertEqual(result.policy_decision, 'hold')
        self.assertEqual([x['total_score'] for x in result.evaluations], [18, 18])
        rec = service._evaluation_recorder
        self.assertEqual(validate_run(rec.directory), [])
        self.assertTrue(rec.manifest.execution_succeeded)
        self.assertEqual({r.company_id for r in rec.records if r.invocation_type == 'llm'}, {None, 'A', 'B'})
        self.assertEqual((rec.directory / 'report_original.md').read_text(), result.final_report)

    def test_pipeline_real_graph_offline_matches_uninstrumented_output(self):
        from investment_pipeline import graph, services
        from investment_pipeline.config import settings
        from investment_pipeline.models import CompanyProfile
        company = CompanyProfile(name='Fixture', stage='Seed')
        with patch.object(settings, 'enable_live_research', False), patch.object(settings, 'enable_llm_enrichment', False), \
             patch.object(services, 'get_knowledge_base', return_value=None):
            for cache in [services._company_research_cache, services._market_research_cache,
                          services._company_evidence_kb_cache, services._market_evidence_kb_cache]:
                cache.clear()
            baseline = graph.run_pipeline('fixture', [company])
            rec = self.recorder('investment_pipeline', formats=['md'])
            rec.candidates = ['Fixture']
            with rec.activate():
                result = graph.run_pipeline('fixture', [company])
                with rec.span('save_outputs', {}) as span:
                    path = self.root / 'report.md'
                    path.write_text(result.report_markdown)
                    rec.artifact(path, 'report_original.md', 'md')
                    span['output'] = {'saved': True}
            rec.finish(result=result)
        self.assertEqual(result.model_dump(), baseline.model_dump())
        self.assertEqual(validate_run(rec.directory), [])
        self.assertEqual(rec.manifest.generation_status, 'succeeded')
        self.assertEqual(rec.manifest.workflow_status, 'failed')
        self.assertEqual(len(rec.research), 2)
        self.assertTrue(all(r.outcome == 'no_new_evidence' for r in rec.research))
        self.assertTrue(all(r.reevaluation_invocation_id for r in rec.research))

class EntryPointTests(unittest.TestCase):
    def test_initialization_failure_is_preserved(self):
        from agents.service import InvestmentAnalysisService
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(FileNotFoundError):
                InvestmentAnalysisService(Path(t))
            directory = next((Path(t) / 'evaluation_runs').iterdir())
            manifest = RunManifest.model_validate_json((directory / 'run.json').read_text())
            self.assertEqual(manifest.generation_status, 'failed')
            self.assertTrue((directory / '.frozen').exists())
            self.assertEqual(validate_run(directory), [])

    def test_cli_pdf_and_json_artifacts_and_pdf_failure(self):
        import os
        from investment_pipeline import cli, graph, services
        from investment_pipeline.config import settings
        from investment_pipeline.models import CompanyProfile
        company = CompanyProfile(name='Fixture', stage='Seed', technology_signal=5, market_signal=5,
                                 traction_signal=5, team_signal=5, competition_signal=5, risk_signal=1)
        with patch.object(settings, 'enable_live_research', False), patch.object(settings, 'enable_llm_enrichment', False), \
             patch.object(services, 'get_knowledge_base', return_value=None):
            fixture = graph.run_pipeline('fixture', [company])
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as t:
            try:
                os.chdir(t)
                input_path = Path('input.json')
                input_path.write_text(json.dumps({'companies': [company.model_dump()]}))
                with patch.object(cli, 'run_pipeline', return_value=fixture):
                    cli.main(input=input_path, output=Path('report.md'), domain='fixture',
                             live_research=False, llm_enrichment=False, polish_korean=False)
                first = next(Path('evaluation_runs').iterdir())
                manifest = RunManifest.model_validate_json((first / 'run.json').read_text())
                self.assertEqual(manifest.generation_status, 'succeeded')
                self.assertEqual({a.format for a in manifest.artifacts if a.status == 'saved'}, {'md', 'pdf', 'json'})
                self.assertEqual(validate_run(first), [])
                with patch.object(cli, 'run_pipeline', return_value=fixture), \
                     patch.object(cli, 'export_markdown_to_pdf', side_effect=OSError('injected PDF failure')):
                    with self.assertRaises(OSError):
                        cli.main(input=input_path, output=Path('failed.md'), domain='fixture',
                                 live_research=False, llm_enrichment=False, polish_korean=False)
                second = next(p for p in Path('evaluation_runs').iterdir() if p != first)
                failed = RunManifest.model_validate_json((second / 'run.json').read_text())
                self.assertEqual(failed.generation_status, 'failed')
                self.assertTrue((second / 'report_original.md').is_file())
                self.assertTrue(any(a.format == 'pdf' and a.status == 'error' for a in failed.artifacts))
            finally:
                os.chdir(original_cwd)

    def test_company_mixup_and_llm_fallback_are_visible(self):
        rec_dir = tempfile.TemporaryDirectory()
        self.addCleanup(rec_dir.cleanup)
        rec = RunRecorder(root=Path(rec_dir.name), execution_path='agents', inputs={}, settings={},
                          requested_formats=['md'], repository=REPOSITORY)
        rec.candidates = ['A']
        @traced('invoke_structured')
        def unavailable():
            return None
        with rec.activate():
            unavailable()
        rec.finish(result={'companies': ['A'], 'technology_evaluations': {'A': {'company_name': 'B'}}})
        checks = json.loads((rec.directory / 'checks.json').read_text())
        self.assertTrue(any(c['check_id'] == 'company_context_evaluate_technology_A' and c['status'] == 'fail' for c in checks))
        self.assertTrue(rec.manifest.fallback_used)
        self.assertIn('fallback', (rec.directory / 'events.jsonl').read_text())


if __name__ == '__main__':
    unittest.main()
