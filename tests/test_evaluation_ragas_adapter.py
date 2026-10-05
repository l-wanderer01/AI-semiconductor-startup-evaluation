"""고정 RAGAS의 실제 metric API를 deterministic LLM으로 검증한다. 외부 API 없음."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.outputs import Generation, LLMResult

from evaluation.models import ModelSettings, RagasSampleInput
from evaluation.ragas_adapter import RagasAdapter, evaluate_inputs, input_problem
from evaluation.storage import EvaluationStorage


class RagasAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ragas.llms.base import BaseRagasLLM
        from ragas.run_config import RunConfig
        class FixtureLLM(BaseRagasLLM):
            def is_finished(self, response):
                return True
            def generate_text(self, prompt, n=1, **kwargs):
                text = prompt.to_string()
                statement = '기업 A는 PoC를 진행 중이다.'
                if 'Decompose and break down' in text:
                    payload = {'claims': [statement]}
                elif 'Given a question and an answer' in text:
                    payload = {'statements': [statement]}
                elif 'classifications' in text and 'attributed' in text:
                    payload = {'classifications': [{'statement': statement, 'reason': '원문 지지', 'attributed': 1}]}
                elif 'statements' in text:
                    payload = {'statements': [{'statement': statement, 'reason': '원문 지지', 'verdict': 1}]}
                else:
                    payload = {'reason': '관련 문맥', 'verdict': 1}
                return LLMResult(generations=[[Generation(text=json.dumps(payload, ensure_ascii=False))] for _ in range(n)])
            async def agenerate_text(self, prompt, n=1, **kwargs):
                return self.generate_text(prompt, n=n, **kwargs)
        cls.llm = FixtureLLM(run_config=RunConfig(max_retries=1))

    def adapter(self):
        return RagasAdapter(llm=self.llm, model=ModelSettings(provider='fixture', model='deterministic'))

    def sample(self, metric='factual_precision', **updates):
        fields = dict(sample_id='sample_1', evaluation_id='eval_1', metric_name=metric,
                      user_input='기업 A의 진행 상태는?', response='기업 A는 PoC를 진행 중이다.',
                      reference='기업 A는 PoC를 진행 중이다.', retrieved_contexts=['기업 A는 PoC를 진행 중이다.'],
                      context_kind='none' if metric.startswith('factual_') else ('delivered' if metric == 'faithfulness' else 'retrieved'),
                      origin_invocation_id='retrieval_1' if metric.startswith('context_') else 'generation_1')
        fields.update(updates)
        return RagasSampleInput(**fields)

    def test_all_five_actual_metrics_and_public_trace(self):
        adapter = self.adapter()
        for metric in adapter.metrics:
            with self.subTest(metric=metric):
                result, trace = asyncio.run(adapter.score(self.sample(metric)))
                self.assertEqual(result.evaluation_status, 'completed', result.reason)
                self.assertAlmostEqual(result.value, 1.0, places=5)
                self.assertTrue(trace.raw_response['attempts'][0]['callbacks'])
                self.assertIsNone(trace.denominator)
                self.assertEqual(trace.mapping_status, 'unsupported')

    def test_missing_empty_and_absent_retrieval_are_not_zero_scores(self):
        for sample, status in [(self.sample(reference=None), 'missing'),
                               (self.sample(response=''), 'not_applicable'),
                               (self.sample('faithfulness', retrieved_contexts=[]), 'not_applicable'),
                               (self.sample('context_precision', origin_invocation_id=None), 'missing')]:
            result, _ = asyncio.run(self.adapter().score(sample))
            self.assertEqual(result.evaluation_status, status)
            self.assertIsNone(result.value)

    def test_zero_nan_and_retry_error_are_distinct(self):
        adapter = self.adapter()
        class Score:
            def __init__(self, value):
                self.value = value
            async def single_turn_ascore(self, *args, **kwargs):
                if isinstance(self.value, Exception):
                    raise self.value
                return self.value
        for value, status, expected in [(0.0, 'completed', 0.0), (float('nan'), 'completed', None),
                                        (ValueError('API failure'), 'error', None)]:
            adapter.metrics['factual_precision'] = Score(value)
            adapter.max_retries = 1
            result, trace = asyncio.run(adapter.score(self.sample()))
            self.assertEqual(result.evaluation_status, status)
            self.assertEqual(result.value, expected)
            self.assertEqual(len(trace.raw_response['attempts']), 2 if status == 'error' else 1)
            if status == 'error':
                self.assertEqual(trace.raw_response['attempts'][1]['previous_invocation_id'],
                                 trace.raw_response['attempts'][0]['invocation_id'])

    def test_configuration_records_actual_prompts_and_real_library_version(self):
        with tempfile.TemporaryDirectory() as t:
            storage = EvaluationStorage(Path(t))
            run = storage.create_run('run_1')
            directory = storage.create_evaluation('run_1', 'eval_1')
            config = self.adapter().configuration(storage, directory)
            self.assertEqual(config.ragas_version, '0.4.3')
            self.assertEqual({m.language for m in config.metrics}, {'ko'})
            for metric in config.metrics:
                content = Path(metric.prompt.snapshot.path).read_text()
                self.assertIn('한국어 입력', content)
                self.assertTrue(metric.trace_limitations)

    def test_postprocessing_does_not_change_run_and_can_create_new_evaluation(self):
        with tempfile.TemporaryDirectory() as t:
            storage = EvaluationStorage(Path(t))
            from evaluation.recording import RunRecorder
            recorder = RunRecorder(root=Path(t), execution_path='agents', inputs={}, settings={},
                                   requested_formats=['md'], repository=Path(__file__).resolve().parents[1])
            run = recorder.directory
            original = storage.write_text(run, 'report_original.md', '원본 보고서').sha256
            with recorder.activate():
                with recorder.span('fixture_generation', {}) as span:
                    span['output'] = 'fixture'
            recorder.finish(exc=ValueError('fixture generation'))
            sample = self.sample(origin_invocation_id=recorder.records[0].invocation_id)
            args = dict(dataset_version='fixture', reference_sha256='a'*64,
                        input_sha256=recorder.manifest.input_snapshot.sha256,
                        evidence_sha256=recorder.manifest.evidence_snapshot.sha256)
            first = asyncio.run(evaluate_inputs(storage, run.name, [sample], self.adapter(), **args))
            second = asyncio.run(evaluate_inputs(storage, run.name, [sample], self.adapter(),
                                                previous_evaluation_id=first.name, resume_reason='rerun', **args))
            self.assertNotEqual(first, second)
            rules = json.loads((second / 'rule_conformance.json').read_text())['data']
            self.assertFalse(rules['ragas_combined'])
            self.assertIsNone(rules['generation_summary']['policies']['agents-baseline-v1']['K'])
            self.assertTrue(any(r['value'] == 1.0 for r in
                [json.loads(line) for line in (second / 'ragas_results.jsonl').read_text().splitlines()]))
            self.assertTrue((first / '.frozen').exists())
            self.assertTrue((second / '.frozen').exists())
            from evaluation.recording import digest
            self.assertEqual(digest((run / 'report_original.md').read_bytes()), original)
            manifest = json.loads((second / 'evaluation.json').read_text())
            self.assertEqual(manifest['quality_evaluation_status'], 'completed')
            self.assertEqual(manifest['previous_evaluation_id'], first.name)

class InputLineageTests(unittest.TestCase):
    def test_context_replacement_and_wrong_retrieval_origin_are_rejected(self):
        from types import SimpleNamespace
        from langchain_core.documents import Document
        from evaluation.recording import ObservedRunnable, RunRecorder
        from evaluation.validation import validate_ragas_input
        with tempfile.TemporaryDirectory() as t:
            rec = RunRecorder(root=Path(t), execution_path='agents', inputs={}, settings={},
                              requested_formats=['md'], repository=Path(__file__).resolve().parents[1])
            retriever = ObservedRunnable(SimpleNamespace(invoke=lambda q: [
                Document(page_content='실제 검색 원문', metadata={'source': 'fixture'})]),
                kind='retrieval', name='retrieval')
            llm = ObservedRunnable(SimpleNamespace(invoke=lambda p: '응답'), kind='llm', name='llm')
            with rec.activate():
                retriever.invoke('실제 검색 query')
                llm.invoke('실제 검색 원문')
            rec.finish(exc=ValueError('fixture'))
            context = rec.contexts[0]
            sample = RagasSampleInput(sample_id='sample', evaluation_id='eval', metric_name='faithfulness',
                                      user_input=Path(rec.records[-1].input_snapshot.path).read_text(), response='응답', retrieved_contexts=[context.text],
                                      context_kind='delivered', context_ids=[context.context_id],
                                      origin_invocation_id=context.invocation_id)
            self.assertEqual(validate_ragas_input(rec.directory, sample), [])
            altered = sample.model_copy(update={'retrieved_contexts': ['몰래 추가한 정답 자료']})
            self.assertTrue(validate_ragas_input(rec.directory, altered))
            retrieval = rec.retrievals[0]
            search = RagasSampleInput(sample_id='search', evaluation_id='eval', metric_name='context_precision',
                                     user_input='query', reference='정답', retrieved_contexts=[c.text for c in retrieval.candidates],
                                     context_ids=[c.context_id for c in retrieval.candidates], context_kind='retrieved',
                                     origin_invocation_id=context.invocation_id)
            self.assertTrue(validate_ragas_input(rec.directory, search))


if __name__ == '__main__':
    unittest.main()
