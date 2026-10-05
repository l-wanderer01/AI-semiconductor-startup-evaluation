"""Provider usage only: no token estimation and no implicit current price assumptions."""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import wraps
import inspect
import os
from pathlib import Path
import time
from uuid import uuid4
from typing import Annotated

from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel, ConfigDict, Field

from .models import RuntimeMetrics, UsageRecord

SCOPE = ContextVar('usage_scope', default=None)
REQUEST_START = ContextVar('request_start', default=None)


class Rates(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    cached_input_per_million: float = Field(ge=0)


class PriceTable(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(min_length=1)
    as_of_date: date
    currency: str = Field(min_length=1)
    source: str = Field(min_length=1)
    models: dict[str, Rates] = Field(default_factory=dict)
    search_per_call: dict[str, Annotated[float, Field(ge=0, allow_inf_nan=False)]] = Field(default_factory=dict)

    def calculate(self, row):
        provenance = dict(currency=self.currency, price_version=self.version,
                          price_as_of_date=self.as_of_date)
        if row.cache_hit is True:
            return dict(cost=0, calculation_method='cache hit: no provider request', **provenance)
        if row.provider == 'local':
            return dict(cost=0, calculation_method='local execution: API fees only; hardware excluded', **provenance)
        if row.call_type == 'search' and row.provider in self.search_per_call:
            rate = self.search_per_call[row.provider]
            if row.status == 'succeeded':
                return dict(cost=rate, calculation_method='successful uncached request * price per call', **provenance)
        rate = self.models.get(f'{row.provider}/{row.model}')
        if rate and all(v is not None for v in (row.input_tokens, row.output_tokens, row.cached_input_tokens)):
            cost = ((row.input_tokens-row.cached_input_tokens)*rate.input_per_million
                    + row.cached_input_tokens*rate.cached_input_per_million
                    + row.output_tokens*rate.output_per_million)/1_000_000
            return dict(cost=cost, calculation_method='((input-cached)*input_rate + cached*cached_rate + output*output_rate)/1000000', **provenance)
        return dict(cost=None, unknown_reason='provider usage, cached token details, or matching price unavailable', **provenance)


def load_prices():
    path = os.getenv('EVALUATION_PRICE_TABLE')
    if path:
        return PriceTable.model_validate_json(Path(path).read_text(encoding='utf-8'))
    return PriceTable(version='unpriced-api-fees-v1', as_of_date=date(2026, 10, 5),
                      currency='USD', source='No external prices configured; local/cache API fees are zero')


def utc():
    return datetime.now(timezone.utc)


def tokens(response):
    """LangChain AIMessage and LLMResult public provider fields, including structured raw."""
    if isinstance(response, dict) and 'raw' in response:
        return tokens(response['raw'])
    usage = getattr(response, 'usage_metadata', None)
    metadata = getattr(response, 'response_metadata', None) or {}
    usage = usage or metadata.get('token_usage') or (getattr(response, 'llm_output', None) or {}).get('token_usage')
    if not usage and getattr(response, 'generations', None):
        # One request can contain multiple choices sharing the same usage; do not add them.
        for group in response.generations:
            for generation in group:
                message = getattr(generation, 'message', None)
                if message is not None:
                    found = tokens(message)
                    if found:
                        return found
    if not usage:
        return {}
    cached = (usage.get('input_token_details') or usage.get('prompt_tokens_details') or {}).get('cache_read', None)
    if cached is None:
        cached = (usage.get('prompt_tokens_details') or {}).get('cached_tokens')
    return dict(input_tokens=usage.get('input_tokens', usage.get('prompt_tokens')),
                output_tokens=usage.get('output_tokens', usage.get('completion_tokens')),
                cached_input_tokens=cached)


class UsageCollector:
    def __init__(self, run_id, purpose, *, evaluation_id=None, prices=None):
        self.run_id, self.purpose, self.evaluation_id = run_id, purpose, evaluation_id
        self.prices = prices or load_prices()
        self.rows = []
        self.started_at, self.clock = utc(), time.monotonic()
        self.endpoint = None

    def add(self, **fields):
        row = UsageRecord(run_id=self.run_id, evaluation_id=self.evaluation_id, purpose=self.purpose,
                          unknown_reason='usage unavailable', **fields)
        calculated = self.prices.calculate(row)
        if calculated['cost'] is not None:
            calculated['unknown_reason'] = None
        row = UsageRecord.model_validate({**row.model_dump(), **calculated})
        self.rows.append(row)
        return row

    @contextmanager
    def activate(self):
        token = SCOPE.set(self)
        try:
            yield self
        finally:
            SCOPE.reset(token)

    def stop(self, success, boundary):
        if self.endpoint is None:
            self.endpoint = dict(ended_at=utc(), duration_seconds=time.monotonic()-self.clock,
                                 status='succeeded' if success else 'failed', boundary=boundary)

    def summary(self):
        end = self.endpoint or dict(duration_seconds=time.monotonic()-self.clock, status='running')
        known = end.get('duration_seconds') is not None and all(r.cost is not None for r in self.rows)
        currencies = {r.currency for r in self.rows if r.cost is not None}
        known = known and len(currencies) <= 1
        states = {r.cache_hit for r in self.rows if r.call_type == 'search' and r.cache_hit is not None}
        return RuntimeMetrics(purpose=self.purpose, started_at=self.started_at,
            completed_at=end.get('ended_at') if end['status'] == 'succeeded' else None,
            usage=self.rows, cost=sum(r.cost for r in self.rows) if known else None,
            currency=next(iter(currencies), self.prices.currency),
            cache_state='mixed' if len(states)==2 else ('warm' if states=={True} else ('cold' if states=={False} else 'unknown')),
            unknown_reason=None if known else 'phase not started or one or more calls have unknown cost; total is not a partial sum', **end)

    def save(self, storage, directory, filename='runtime.json'):
        storage.write_json(directory, filename, self.summary())
        storage.write_json(directory, filename.replace('.json', '_prices.json'), self.prices)
        storage.write_json(directory, filename.replace('.json', '_usage.json'), self.rows)


class UsageCallback(BaseCallbackHandler):
    """Records actual public callback calls/retries; hidden SDK retries remain unknown."""
    def __init__(self, collector, model=None, metric=None, attempt=1):
        self.collector, self.model, self.metric, self.attempt = collector, model, metric, attempt
        self.active = {}
        self.count = 0

    def _start(self, run_id, **kwargs):
        self.active[str(run_id)] = (utc(), time.monotonic(), kwargs.get('invocation_params') or {}, 0)

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._start(run_id, **kwargs)

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        self._start(run_id, **kwargs)

    def on_retry(self, retry_state, *, run_id, **kwargs):
        key = str(run_id)
        if key in self.active:
            start, clock, params, retries = self.active[key]
            self.active[key] = (start, clock, params, retries+1)

    def _end(self, run_id, response=None, success=True):
        start, clock, params, retries = self.active.pop(str(run_id), (utc(), time.monotonic(), {}, 0))
        metadata = getattr(response, 'response_metadata', None) or getattr(response, 'llm_output', None) or {}
        self.collector.add(invocation_id='usage_'+str(run_id), call_type='llm',
            provider=getattr(self.model, 'provider', 'unknown'),
            model=metadata.get('model_name') or params.get('model') or getattr(self.model, 'model', None),
            attempt=self.attempt, metric_name=self.metric, status='succeeded' if success else 'failed',
            started_at=start, ended_at=utc(), duration_seconds=time.monotonic()-clock,
            retry_count=retries, **tokens(response))
        self.count += 1

    def on_llm_end(self, response, *, run_id, **kwargs):
        self._end(run_id, response)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, success=False)

    def close(self):
        for key in list(self.active):
            self._end(key, success=False)


def invoke_usage(runnable, inputs, args, kwargs, collector, model=None, metric=None):
    if collector is None:
        return runnable.invoke(inputs, *args, **kwargs)
    capture = UsageCallback(collector, model, metric)
    start, clock = utc(), time.monotonic()
    options = dict(kwargs)
    # Lightweight test doubles without Runnable config remain supported.
    signature = inspect.signature(runnable.invoke)
    if args and 'config' in signature.parameters:
        options['config'], args = args[0], args[1:]
    if 'config' in signature.parameters or any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values()):
        config = dict(options.get('config') or {})
        existing = config.get('callbacks')
        config['callbacks'] = [capture] if existing is None else (existing + [capture] if isinstance(existing, list) else existing.copy())
        if existing is not None and not isinstance(existing, list):
            config['callbacks'].add_handler(capture, inherit=True)
        options['config'] = config
    response, succeeded = None, False
    try:
        response = runnable.invoke(inputs, *args, **options)
        succeeded = True
        return response
    finally:
        capture.close()
        if not capture.count:
            collector.add(invocation_id='usage_'+uuid4().hex, call_type='llm',
                provider=getattr(model, 'provider', 'unknown'), model=getattr(model, 'model', None),
                metric_name=metric, attempt=1, started_at=start, ended_at=utc(),
                duration_seconds=time.monotonic()-clock, status='succeeded' if succeeded else 'failed', **tokens(response))


def custom_runnable(model, schema, settings, name):
    from .models import ModelSettings
    return CustomRunnable(model.with_structured_output(schema, include_raw=True),
                          ModelSettings(provider=settings.get('provider', 'unknown'), model=settings.get('model', 'unknown')), name)


class CustomRunnable:
    def __init__(self, runnable, model, metric):
        self.runnable, self.model, self.metric = runnable, model, metric

    def invoke(self, inputs, *args, **kwargs):
        return invoke_usage(self.runnable, inputs, args, kwargs, SCOPE.get(), self.model, self.metric)


def local_embedding(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        from .recording import CURRENT
        rec = CURRENT.get()
        if rec is None:
            return function(*args, **kwargs)
        start, clock, success = utc(), time.monotonic(), False
        try:
            result = function(*args, **kwargs)
            success = True
            return result
        finally:
            owner = args[0] if args else None
            model = getattr(owner, 'model_name', None) or rec.settings.get('embedding_model') or rec.settings.get('dense_embedding_model')
            rec.usage.add(invocation_id='embedding_'+uuid4().hex, call_type='embedding', provider='local',
                model=model, attempt=1, started_at=start, ended_at=utc(),
                duration_seconds=time.monotonic()-clock, status='succeeded' if success else 'failed')
    return wrapped



def request_boundary(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = REQUEST_START.set((time.monotonic(), utc()))
        try:
            return function(*args, **kwargs)
        finally:
            REQUEST_START.reset(token)
    return wrapped
