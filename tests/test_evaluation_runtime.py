"""Resource accounting regression: public provider fields and request boundaries."""
import asyncio
from datetime import date
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from pydantic import ValidationError

from evaluation.models import ModelSettings, RagasSampleInput
from evaluation.recording import ObservedRunnable, RunRecorder
from evaluation.runtime import (PriceTable, UsageCallback, UsageCollector, invoke_usage,
                                local_embedding, tokens)

REPO = Path(__file__).resolve().parents[1]
MODEL = ModelSettings(provider='openai', model='fixture-model')


def price_table():
    # Synthetic prices, never presented as current provider prices.
    return PriceTable(version='fixture-v1', as_of_date=date(2026, 1, 1), currency='USD',
        source='synthetic test fixture', models={'openai/fixture-model': {
            'input_per_million': 2, 'output_per_million': 8, 'cached_input_per_million': 1}},
        search_per_call={'tavily': .02})


def message():
    return AIMessage(content='fixture', usage_metadata={
        'input_tokens': 1000, 'output_tokens': 100, 'total_tokens': 1100,
        'input_token_details': {'cache_read': 200}})


class CallbackModel:
    def invoke(self, inputs, config=None):
        run_id = uuid4()
        response = LLMResult(generations=[[ChatGeneration(message=message())]])
        for callback in (config or {}).get('callbacks', []):
            callback.on_chat_model_start({}, [[inputs]], run_id=run_id)
            callback.on_llm_end(response, run_id=run_id)
        return {'parsed': 'structured fixture'}


class RuntimeTests(unittest.TestCase):
    def collector(self, purpose='generation'):
        return UsageCollector('run_fixture', purpose, prices=price_table())

    def recorder(self, root):
        return RunRecorder(root=root / 'runs', execution_path='agents', inputs={},
            settings={'llm_enabled': False}, requested_formats=['md'], repository=REPO)

    def test_structured_callback_preserves_usage_without_double_counting(self):
        collector = self.collector()
        observed = []
        class ExistingCallback:
            def on_chat_model_start(self, *args, **kwargs):
                observed.append('start')
            def on_llm_end(self, *args, **kwargs):
                observed.append('end')
        result = invoke_usage(CallbackModel(), 'query', (), {'config': {'callbacks': [ExistingCallback()]}}, collector, MODEL)
        self.assertEqual(result['parsed'], 'structured fixture')
        self.assertEqual(observed, ['start', 'end'])
        self.assertEqual(len(collector.rows), 1)
        row = collector.rows[0]
        self.assertEqual((row.input_tokens, row.output_tokens, row.cached_input_tokens), (1000, 100, 200))
        self.assertAlmostEqual(row.cost, .0026)
        self.assertEqual(row.price_version, 'fixture-v1')

    def test_missing_cached_details_or_model_price_keeps_cost_unknown(self):
        collector = self.collector()
        invoke_usage(SimpleNamespace(invoke=lambda p: AIMessage(content='text', usage_metadata={
            'input_tokens': 5, 'output_tokens': 2, 'total_tokens': 7})), 'query', (), {}, collector, MODEL)
        self.assertEqual(collector.rows[0].input_tokens, 5)
        self.assertIsNone(collector.rows[0].cached_input_tokens)
        self.assertIsNone(collector.rows[0].cost)
        self.assertIsNone(collector.summary().cost)
        self.assertIsNone(tokens('no metadata').get('input_tokens'))

    def test_provider_token_usage_and_structured_raw(self):
        response = SimpleNamespace(response_metadata={'token_usage': {'prompt_tokens': 12,
            'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 0}}})
        self.assertEqual(tokens({'raw': response}), dict(input_tokens=12, output_tokens=3, cached_input_tokens=0))

    def test_failed_call_and_callback_retry_are_preserved(self):
        collector = self.collector('ragas_evaluation')
        capture = UsageCallback(collector, MODEL, 'faithfulness', attempt=2)
        run_id = uuid4()
        capture.on_llm_start({}, ['query'], run_id=run_id)
        capture.on_retry(None, run_id=run_id)
        capture.on_llm_error(ValueError('failure'), run_id=run_id)
        row = collector.rows[0]
        self.assertEqual((row.metric_name, row.attempt, row.retry_count, row.status), ('faithfulness', 2, 1, 'failed'))
        self.assertIsNone(row.cost)
        self.assertEqual(row.retry_visibility, 'provider_internal_unknown')

    def test_actual_ragas_langchain_wrapper_propagates_provider_usage(self):
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.outputs import ChatResult
        from langchain_core.prompt_values import StringPromptValue
        from ragas.llms import LangchainLLMWrapper
        from evaluation.ragas_adapter import RagasAdapter
        from test_evaluation_ragas_adapter import RagasAdapterTests
        RagasAdapterTests.setUpClass()
        fixture = RagasAdapterTests()
        class UsageChat(BaseChatModel):
            @property
            def _llm_type(self):
                return 'fixture'
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                prompt = StringPromptValue(text='\n'.join(m.content for m in messages))
                content = fixture.llm.generate_text(prompt).generations[0][0].text
                answer = message().model_copy(update={'content': content})
                return ChatResult(generations=[ChatGeneration(message=answer)])
        adapter = RagasAdapter(llm=LangchainLLMWrapper(UsageChat()), model=MODEL)
        collector = self.collector('ragas_evaluation')
        with collector.activate():
            result, trace = asyncio.run(adapter.score(fixture.sample()))
        self.assertEqual(result.evaluation_status, 'completed', result.reason)
        self.assertGreater(len(collector.rows), 1)
        self.assertTrue(all(r.input_tokens == 1000 and r.cost == .0026 for r in collector.rows))
        self.assertEqual({r.metric_name for r in collector.rows}, {'factual_precision'})

    def test_unsupported_or_failed_runnable_is_unknown_not_free(self):
        collector = self.collector()
        def fail(p):
            raise ValueError('fixture')
        with self.assertRaises(ValueError):
            invoke_usage(SimpleNamespace(invoke=fail), 'query', (), {}, collector, MODEL)
        self.assertEqual(collector.rows[0].status, 'failed')
        self.assertIsNone(collector.rows[0].input_tokens)
        self.assertIsNone(collector.summary().cost)

    def test_cold_warm_mixed_search_cost(self):
        collector = self.collector()
        for cache_hit in (False, True):
            collector.add(invocation_id='search_'+str(cache_hit), call_type='search', provider='tavily',
                          attempt=1, cache_hit=cache_hit, status='succeeded')
            self.assertEqual(collector.summary().cache_state, 'cold' if not cache_hit else 'mixed')
        self.assertEqual([r.cost for r in collector.rows], [.02, 0])
        self.assertEqual(collector.summary().cost, .02)

    def test_actual_tavily_cache_miss_hit_and_failure_usage(self):
        from investment_pipeline.tavily import TavilySearchClient
        from investment_pipeline.config import settings
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            rec = self.recorder(root)
            rec.usage.prices = price_table()
            client = TavilySearchClient('fixture-key')
            response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'results': []})
            with patch.object(settings, 'enable_live_research', True), \
                 patch.object(settings, 'research_cache_dir', root / 'cache'), rec.activate():
                with patch('investment_pipeline.tavily.requests.post', return_value=response) as post:
                    client.search('query', category='technology')
                    client.search('query', category='technology')
                    self.assertEqual(post.call_count, 1)
                with patch('investment_pipeline.tavily.requests.post', side_effect=OSError('fixture')):
                    with self.assertRaises(OSError):
                        client.search('failed query', category='technology')
            self.assertEqual([r.cache_hit for r in rec.usage.rows], [False, True, False])
            self.assertEqual([r.status for r in rec.usage.rows], ['succeeded', 'succeeded', 'failed'])
            self.assertEqual([r.cost for r in rec.usage.rows], [.02, 0, None])

    def test_custom_structured_usage_is_separate(self):
        from evaluation.runtime import custom_runnable
        model = SimpleNamespace(with_structured_output=lambda *args, **kwargs: CallbackModel())
        chain = custom_runnable(model, object, {'provider': 'openai', 'model': 'fixture-model'}, 'custom_factual')
        collector = self.collector('custom_evaluation')
        with collector.activate():
            result = chain.invoke('fixture')
        self.assertEqual(result['parsed'], 'structured fixture')
        self.assertEqual(collector.rows[0].purpose, 'custom_evaluation')
        self.assertEqual(collector.rows[0].metric_name, 'custom_factual')
        self.assertAlmostEqual(collector.rows[0].cost, .0026)

    def test_unknown_call_does_not_become_partial_total(self):
        collector = self.collector()
        collector.add(invocation_id='known', call_type='search', provider='tavily', attempt=1, cache_hit=True)
        collector.add(invocation_id='unknown', call_type='llm', provider='openai', attempt=1)
        self.assertIsNone(collector.summary().cost)

    def test_price_validation_rejects_invalid_search_rates(self):
        for value in (-1, float('nan'), float('inf')):
            with self.assertRaises(ValidationError):
                PriceTable(version='v1', as_of_date=date.today(), currency='USD', source='fixture',
                           search_per_call={'tavily': value})

    def test_generation_endpoint_excludes_later_postprocessing(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            clock = [10.0]
            with patch('evaluation.runtime.time.monotonic', side_effect=lambda: clock[0]):
                rec = self.recorder(root)
                path = root / 'report.md'
                path.write_text('report')
                clock[0] = 40
                rec.artifact(path, 'report_original.md', 'md')
                rec.generation_complete()
                clock[0] = 200
                rec.finish()
            metrics = json.loads((rec.directory / 'metrics.json').read_text())
            self.assertEqual(metrics['generation_runtime']['duration_seconds'], 30)
            self.assertEqual(metrics['generation_runtime']['status'], 'succeeded')
            self.assertIsNotNone(metrics['generation_runtime']['completed_at'])
            self.assertEqual(metrics['generation_runtime']['cost'], 0)
            self.assertFalse(rec.manifest.llm_enabled)
            self.assertEqual(rec.manifest.duration_seconds, 190)

    def test_initialization_and_missing_artifacts_fail_without_completion(self):
        with tempfile.TemporaryDirectory() as t:
            rec = self.recorder(Path(t))
            rec.finish(exc=ValueError('initialization failed'))
            runtime = json.loads((rec.directory / 'generation_runtime.json').read_text())
            self.assertEqual(runtime['status'], 'failed')
            self.assertIsNone(runtime['completed_at'])
            self.assertGreaterEqual(runtime['duration_seconds'], 0)

    def test_local_embedding_initialization_and_retrieval_are_recorded(self):
        with tempfile.TemporaryDirectory() as t:
            rec = self.recorder(Path(t))
            @local_embedding
            def embed(texts):
                return [[1.0] for text in texts]
            with rec.activate():
                self.assertEqual(embed(['text']), [[1.0]])
            self.assertEqual(rec.usage.rows[0].call_type, 'embedding')
            self.assertEqual(rec.usage.rows[0].provider, 'local')
            self.assertEqual(rec.usage.rows[0].cost, 0)
            self.assertIsNone(rec.usage.rows[0].input_tokens)

    def test_embedding_observer_preserves_query_encoding_options(self):
        from agents.service import ObservedEmbeddings
        embeddings = ObservedEmbeddings.model_construct(
            model_name='fixture', encode_kwargs={'normalize_embeddings': True},
            query_encode_kwargs={'normalize_embeddings': False, 'prompt': 'query'})
        with patch.object(ObservedEmbeddings, '_embed', return_value=[[1.0]]) as embed:
            self.assertEqual(embeddings.embed_query('text'), [1.0])
            embed.assert_called_once_with(['text'], {'normalize_embeddings': False, 'prompt': 'query'})

    def test_observed_generation_usage_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as t, patch.dict('os.environ', {'OPENAI_API_KEY': 'fixture-secret-value'}):
            rec = self.recorder(Path(t))
            rec.usage.prices = price_table()
            runnable = ObservedRunnable(CallbackModel(), kind='llm', name='fixture', model=MODEL)
            with rec.activate():
                runnable.invoke('Bearer fixture-secret-value')
            rec.finish(exc=ValueError('fixture-secret-value'))
            self.assertEqual(len(rec.usage.rows), 1)
            self.assertNotIn('fixture-secret-value', '\n'.join(p.read_text() for p in rec.directory.iterdir() if p.is_file()))

    def test_evaluation_usage_separate_from_frozen_generation_with_retry(self):
        from test_evaluation_ragas_adapter import RagasAdapterTests
        from evaluation.ragas_adapter import evaluate_inputs
        RagasAdapterTests.setUpClass()
        fixture = RagasAdapterTests()
        adapter = fixture.adapter()
        class Metric:
            llm = fixture.llm
            count = 0
            async def single_turn_ascore(self, native, callbacks):
                self.count += 1
                run_id = uuid4()
                for callback in callbacks:
                    callback.on_llm_start({}, ['fixture'], run_id=run_id)
                    if self.count == 1:
                        callback.on_llm_error(ValueError('fixture'), run_id=run_id)
                    else:
                        callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message())]]), run_id=run_id)
                if self.count == 1:
                    raise ValueError('fixture')
                return 1.0
        adapter.metrics['factual_precision'] = Metric()
        adapter.max_retries = 1
        with tempfile.TemporaryDirectory() as t, patch('evaluation.runtime.load_prices', return_value=price_table()):
            rec = self.recorder(Path(t))
            with rec.activate():
                with rec.span('generation', {}) as span:
                    span['output'] = 'fixture'
            rec.finish(exc=ValueError('fixture'))
            before = {p.name: p.read_bytes() for p in rec.directory.iterdir() if p.is_file()}
            sample = fixture.sample(origin_invocation_id=rec.records[0].invocation_id)
            directory = asyncio.run(evaluate_inputs(rec.storage, rec.run_id, [sample], adapter,
                dataset_version='fixture', reference_sha256='a'*64,
                input_sha256=rec.manifest.input_snapshot.sha256,
                evidence_sha256=rec.manifest.evidence_snapshot.sha256))
            self.assertTrue((directory / '.frozen').exists())
            self.assertEqual(before, {p.name: p.read_bytes() for p in rec.directory.iterdir() if p.is_file()})
            metrics = json.loads((directory / 'metrics.json').read_text())
            rows = metrics['ragas_runtime']['usage']
            self.assertEqual([r['attempt'] for r in rows], [1, 2])
            self.assertEqual([r['status'] for r in rows], ['failed', 'succeeded'])
            self.assertIsNone(metrics['ragas_runtime']['cost'])
            self.assertEqual(metrics['custom_runtime']['usage'], [])
            self.assertEqual({r['metric_name'] for r in rows}, {'factual_precision'})
            self.assertEqual({r['purpose'] for r in rows}, {'ragas_evaluation'})


if __name__ == '__main__':
    unittest.main()
