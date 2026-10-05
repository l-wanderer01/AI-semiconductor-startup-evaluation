"""기존 진입점에 기록기를 연결한다. 반환값과 예외를 유지한다."""
import functools
from pathlib import Path

from .models import ModelSettings, RetrievalSettings
from .recording import CURRENT, ObservedRunnable, RunRecorder, error_record, json_value, traced


def service_settings(config):
    return dict(llm_enabled=True, live_research_enabled=False, model=config.llm_model,
                temperature=config.temperature, recommendation_threshold=config.recommendation_threshold,
                top_k=config.top_k_companies, embedding_model=config.embedding_model,
                chunk_size=config.chunk_size, chunk_overlap=config.chunk_overlap,
                retrieval_k=config.retrieval_k, domain=config.domain, max_retries=2)


def service_initialization(function):
    @functools.wraps(function)
    def wrapped(self, base_dir, config=None):
        from agents.models import ServiceConfig
        config = config or ServiceConfig()
        rec = RunRecorder(root=Path(base_dir) / 'evaluation_runs', execution_path='agents',
                          inputs={'domain': config.domain}, settings=service_settings(config),
                          requested_formats=['md', 'json'], repository=Path(base_dir))
        self._evaluation_recorder = rec
        try:
            with rec.activate():
                traced('initialize_service')(function)(self, base_dir, config)
                for doc in self.documents:
                    rec.add_evidence(doc.page_content, source=doc.metadata['source'], title=doc.metadata['file_name'])
                rec.manifest.retrieval = RetrievalSettings(backend='faiss', dense_model=config.embedding_model,
                                                          top_k=config.retrieval_k, chunk_size=config.chunk_size,
                                                          chunk_overlap=config.chunk_overlap)
                self.llm = ObservedRunnable(self.llm, kind='llm', name='service_llm',
                                            model=ModelSettings(provider='openai', model=config.llm_model,
                                                                temperature=config.temperature, max_retries=2))
                self.retriever = ObservedRunnable(self.retriever, kind='retrieval', name='service_retrieval')
        except BaseException as exc:
            rec.finish(exc=exc)
            raise
    return wrapped


def service_execution(function):
    @functools.wraps(function)
    def wrapped(self, domain=None):
        rec = self._evaluation_recorder
        if rec.finished:
            rec = RunRecorder(root=self.base_dir / 'evaluation_runs', execution_path='agents',
                              inputs={'domain': domain or self.config.domain}, settings=service_settings(self.config),
                              requested_formats=['md', 'json'], repository=self.base_dir)
            self._evaluation_recorder = rec
            for doc in self.documents:
                rec.add_evidence(doc.page_content, source=doc.metadata['source'], title=doc.metadata['file_name'])
        # 초기화 입력과 별도로 실제 run 입력을 고정한다.
        rec.manifest.input_snapshot = rec.snapshot('request_snapshot.json', {'domain': domain or self.config.domain})
        try:
            with rec.activate():
                result = traced('service_run')(function)(self, domain)
                rec.candidates = result.companies
                with rec.span('save_outputs', {'output_path': result.output_path}) as span:
                    rec.artifact(Path(result.output_path), 'report_original.md', 'md')
                    state = rec.snapshot('state_original.json', result)
                    rec.artifact(Path(state.path), 'state_artifact.json', 'json')
                    span['output'] = {'artifacts': [a.model_dump(mode='json') for a in rec.artifacts]}
            rec.finish(result=result)
            return result
        except BaseException as exc:
            rec.finish(exc=exc)
            raise
    return wrapped


def observed_search(*, web=False):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(self, query, *args, **kwargs):
            rec = CURRENT.get()
            if rec is None:
                return function(self, query, *args, **kwargs)
            cache_hit = None
            if web:
                from investment_pipeline.config import settings
                category = kwargs['category']
                key = f'{category}__{query}'.replace('/', '_').replace(' ', '_')[:180]
                cache_hit = (settings.research_cache_dir / f'{key}.json').exists() if self.available else None
            with rec.span(type(self).__name__ + '_search', {'query': query, 'options': kwargs}, kind='retrieval') as span:
                try:
                    result = function(self, query, *args, **kwargs)
                except Exception as exc:
                    rec.retrieval(query, [], span['record'], web=web, cache_hit=cache_hit,
                                  error=error_record(exc, span['record'].invocation_id))
                    raise
                span['output'] = json_value(result)
                rec.retrieval(query, result, span['record'], web=web, cache_hit=cache_hit)
                if web and not self.available:
                    span['fallback'] = 'live_search_disabled_or_credentials_missing'
            return result
        return wrapped
    return decorate
