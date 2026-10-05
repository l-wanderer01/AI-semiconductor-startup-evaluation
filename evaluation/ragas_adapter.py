"""RAGAS 0.4.3 single_turn_ascore API를 프로젝트 계약으로 정규화한다."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
from importlib.metadata import version
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

from .models import (EvaluationManifest, EvaluatorConfiguration, MetricConfiguration, ModelSettings, RunManifest, MetricsRecord,
                     PromptVersion, RagasMetricResult, RagasSampleInput, RagasTraceRecord)
from .recording import SnapshotPayload, digest, identifier, json_value, now
from .storage import EvaluationStorage, _redact_value

RAGAS_VERSION = '0.4.3'
ADAPTER_VERSION = '0.2.0'
API_VERSION = 'SingleTurnSample.single_turn_ascore'
KOREAN_INSTRUCTION = ('\n한국어 입력을 그대로 평가하세요. 숫자·통화·단위·시점·기업명과 비교 조건을 보존하고, '
                      'PoC·검증·계약·양산을 구분하세요. 이유와 분해된 주장은 한국어로 작성하되 JSON schema를 지키세요.')
IMPLEMENTATIONS = {
    'factual_precision': 'FactualCorrectness', 'factual_recall': 'FactualCorrectness',
    'faithfulness': 'Faithfulness', 'context_recall': 'LLMContextRecall',
    'context_precision': 'LLMContextPrecisionWithReference',
}
REQUIRED = {
    'factual_precision': ['response', 'reference'], 'factual_recall': ['response', 'reference'],
    'faithfulness': ['user_input', 'response', 'retrieved_contexts'],
    'context_recall': ['user_input', 'reference', 'retrieved_contexts'],
    'context_precision': ['user_input', 'reference', 'retrieved_contexts'],
}


def input_problem(sample: RagasSampleInput):
    for field in REQUIRED[sample.metric_name.value]:
        value = getattr(sample, field)
        if value is None:
            return 'missing', f'{field} is missing'
        if isinstance(value, str) and not value.strip():
            return 'not_applicable', f'{field} is empty; metric has no evaluable input'
        if isinstance(value, list) and (not value or not any(v.strip() for v in value)):
            return 'not_applicable', 'no applicable context'
    if sample.metric_name.value.startswith('context_') and not sample.origin_invocation_id:
        return 'missing', 'retrieval metrics require an actual retrieval invocation'
    return None


class TraceCapture(BaseCallbackHandler):
    """공개 callback에서 제공된 원응답·판정만 보존한다. 분모는 추정하지 않는다."""
    def __init__(self):
        self.rows = []
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        self.rows.append({'kind': 'chain_start', 'run_id': str(run_id), 'parent_run_id': str(parent_run_id) if parent_run_id else None,
                          'name': (serialized or {}).get('name'), 'input': json_value(inputs)})
    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self.rows.append({'kind': 'chain_end', 'run_id': str(run_id), 'output': json_value(outputs)})
    def on_llm_end(self, response, *, run_id, **kwargs):
        self.rows.append({'kind': 'llm_end', 'run_id': str(run_id), 'response': json_value(response)})
    def on_chain_error(self, error, *, run_id, **kwargs):
        self.rows.append({'kind': 'chain_error', 'run_id': str(run_id), 'error': str(error)})


class RagasAdapter:
    def __init__(self, *, llm, model: ModelSettings, timeout_seconds=120.0, max_retries=0):
        os.environ['RAGAS_DO_NOT_TRACK'] = 'true'
        if version('ragas') != RAGAS_VERSION:
            raise RuntimeError(f'ragas=={RAGAS_VERSION} is required')
        from ragas.dataset_schema import SingleTurnSample
        from ragas.metrics._factual_correctness import FactualCorrectness
        from ragas.metrics._faithfulness import Faithfulness
        from ragas.metrics._context_recall import LLMContextRecall
        from ragas.metrics._context_precision import LLMContextPrecisionWithReference
        self.sample_type = SingleTurnSample
        self.model, self.timeout_seconds, self.max_retries = model, timeout_seconds, max_retries
        self.metrics = {
            'factual_precision': FactualCorrectness(llm=llm, mode='precision', atomicity='high', coverage='high'),
            'factual_recall': FactualCorrectness(llm=llm, mode='recall', atomicity='high', coverage='high'),
            'faithfulness': Faithfulness(llm=llm),
            'context_recall': LLMContextRecall(llm=llm),
            'context_precision': LLMContextPrecisionWithReference(llm=llm),
        }
        self.prompt_data = {}
        for name, metric in self.metrics.items():
            prompts = copy.deepcopy(metric.get_prompts())
            for prompt in prompts.values():
                prompt.instruction += KOREAN_INSTRUCTION
                prompt.language = 'korean'
            metric.set_prompts(**prompts)
            if hasattr(metric, 'language'):
                metric.language = 'korean'
            self.prompt_data[name] = {key: {
                'instruction': prompt.instruction, 'language': prompt.language,
                'examples': json_value(prompt.examples),
                'input_schema': prompt.input_model.model_json_schema(),
                'output_schema': prompt.output_model.model_json_schema(),
            } for key, prompt in prompts.items()}

    @classmethod
    def openai(cls, model_name='gpt-4.1-mini', *, max_retries=0):
        os.environ['RAGAS_DO_NOT_TRACK'] = 'true'
        from langchain_openai import ChatOpenAI
        from ragas.llms import LangchainLLMWrapper
        from ragas.run_config import RunConfig
        model = ModelSettings(provider='openai', model=model_name, temperature=0.0,
                              max_retries=0, timeout_seconds=120)
        llm = LangchainLLMWrapper(ChatOpenAI(model=model_name, temperature=0, max_retries=0, timeout=120),
                                 run_config=RunConfig(max_retries=1, timeout=120))
        return cls(llm=llm, model=model, max_retries=max_retries)

    def configuration(self, storage, directory):
        metric_configs = []
        for name in self.metrics:
            ref = storage.write_snapshot(directory, name + '_prompts.json', SnapshotPayload(data=self.prompt_data[name]))
            factual = name.startswith('factual_')
            metric_configs.append(MetricConfiguration(
                metric_name=name, implementation=IMPLEMENTATIONS[name],
                mode=name.removeprefix('factual_') if factual else None,
                atomicity='high' if factual else None, coverage='high' if factual else None,
                prompt=PromptVersion(prompt_id=name, version='ko-explicit-instruction-v1', sha256=ref.sha256, snapshot=ref),
                language='ko', trace_supported=True,
                trace_limitations=['public callbacks only; raw/custom claim ID mapping and counts unsupported'],
            ))
        settings = {'ragas_version': RAGAS_VERSION, 'api_version': API_VERSION, 'adapter_version': ADAPTER_VERSION,
                    'schema_version': '0.1.0', 'model': self.model.model_dump(mode='json'),
                    'metrics': [m.model_dump(mode='json') for m in metric_configs],
                    'max_retries': self.max_retries, 'cache_enabled': False}
        runtime_settings = {'sample_timeout_seconds': self.timeout_seconds,
                            'adapter_max_retries': self.max_retries,
                            'library_retry_attempts': getattr(getattr(next(iter(self.metrics.values())).llm, 'run_config', None), 'max_retries', None),
                            'prompt_parse_retries': 3,
                            'library_metric_retries': {name: getattr(metric, 'max_retries', None)
                                                       for name, metric in self.metrics.items()}}
        storage.write_snapshot(directory, 'adapter_settings.json', SnapshotPayload(data=runtime_settings))
        hash_settings = copy.deepcopy(settings)
        hash_settings['runtime_settings'] = runtime_settings
        for metric in hash_settings['metrics']:
            metric['prompt'].pop('snapshot', None)
        return EvaluatorConfiguration(**settings, configuration_sha256=digest(json.dumps(
            hash_settings, sort_keys=True, ensure_ascii=False).encode()))

    async def score(self, sample: RagasSampleInput):
        problem = input_problem(sample)
        if problem:
            status, reason = problem
            result = RagasMetricResult(evaluation_id=sample.evaluation_id, sample_id=sample.sample_id,
                                       metric_name=sample.metric_name, evaluation_status=status, reason=reason)
            return result, RagasTraceRecord(evaluation_id=sample.evaluation_id, sample_id=sample.sample_id,
                                            metric_name=sample.metric_name, raw_response={'attempts': []},
                                            mapping_status='unsupported', unavailable_reason=reason)
        metric = self.metrics[sample.metric_name.value]
        native = self.sample_type(**{field: getattr(sample, field) for field in REQUIRED[sample.metric_name.value]})
        attempts = []
        value, reason, status = None, None, 'error'
        invocation = None
        for attempt in range(1, self.max_retries + 2):
            capture = TraceCapture()
            previous = invocation
            invocation = identifier('eval_inv')
            row = {'invocation_id': invocation, 'previous_invocation_id': previous,
                   'attempt': attempt, 'started_at': now().isoformat()}
            try:
                raw = await asyncio.wait_for(metric.single_turn_ascore(native, callbacks=[capture]), self.timeout_seconds)
                value = float(raw)
                row['returned_score'] = value if math.isfinite(value) else None
                if not math.isfinite(value):
                    status, value, reason = 'completed', None, 'RAGAS returned an undefined score (NaN/Inf)'
                elif not 0 <= value <= 1:
                    raise ValueError('RAGAS score outside 0..1')
                else:
                    status, reason = 'completed', None
            except Exception as exc:
                status, value, reason = 'error', None, f'{type(exc).__name__}: {exc}'
                row['error'] = reason
            row.update(ended_at=now().isoformat(), callbacks=capture.rows)
            attempts.append(row)
            if status == 'completed':
                break
        result = RagasMetricResult(evaluation_id=sample.evaluation_id, sample_id=sample.sample_id,
                                   metric_name=sample.metric_name, evaluation_status=status, value=value,
                                   reason=reason, invocation_id=invocation)
        trace = RagasTraceRecord(evaluation_id=sample.evaluation_id, sample_id=sample.sample_id,
                                metric_name=sample.metric_name, invocation_id=invocation,
                                raw_response=_redact_value({'attempts': attempts}), mapping_status='unsupported',
                                unavailable_reason='callbacks preserve raw responses; custom claim mapping/counts not provided')
        return result, trace


async def evaluate_inputs(storage: EvaluationStorage, run_id: str, samples: list[RagasSampleInput],
                          adapter: RagasAdapter, *, dataset_version: str,
                          reference_sha256: str, input_sha256: str, evidence_sha256: str,
                          previous_evaluation_id=None, resume_reason=None, preparation_metadata=None, stage_package=None):
    """불변 생성 run 아래 독립 평가를 만든다. 재개도 새 ID로 저장한다."""
    if not samples:
        raise ValueError('evaluation requires at least one sample')
    keys = [(s.sample_id, s.metric_name) for s in samples]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate sample/metric pair')
    run_directory = storage.root / run_id
    run_manifest = RunManifest.model_validate_json((run_directory / 'run.json').read_text())
    if not (run_directory / '.frozen').is_file():
        raise ValueError('postprocessing requires a frozen generation run')
    if input_sha256 != run_manifest.input_snapshot.sha256 or evidence_sha256 != run_manifest.evidence_snapshot.sha256:
        raise ValueError('evaluation input/evidence hash does not match generation run')
    if stage_package is not None:
        if (stage_package['run_id'], stage_package['input_sha256'], stage_package['evidence_sha256']) != (run_id, input_sha256, evidence_sha256):
            raise ValueError('stage package generation scope mismatch')
        dataset = stage_package.get('dataset')
        if dataset is not None and (dataset['manifest']['version'], dataset['manifest']['sha256']) != (dataset_version, reference_sha256):
            raise ValueError('stage package reference scope mismatch')
    if previous_evaluation_id:
        storage._validate_component(previous_evaluation_id)
        previous = EvaluationManifest.model_validate_json(
            (run_directory / 'evaluations' / previous_evaluation_id / 'evaluation.json').read_text())
        if previous.run_id != run_id:
            raise ValueError('previous evaluation belongs to a different run')
    evaluation_id = identifier('evaluation')
    directory = storage.create_evaluation(run_id, evaluation_id)
    configuration = adapter.configuration(storage, directory)
    count = len({s.sample_id for s in samples})
    manifest = EvaluationManifest(evaluation_id=evaluation_id, run_id=run_id,
                                  previous_evaluation_id=previous_evaluation_id, resume_reason=resume_reason,
                                  mode=stage_package['mode'] if stage_package else 'end_to_end', dataset_version=dataset_version,
                                  reference_sha256=reference_sha256, input_sha256=input_sha256,
                                  evidence_sha256=evidence_sha256, evaluator=configuration,
                                  started_at=now(), quality_evaluation_status='running', sample_count=count)
    storage.write_json(directory, 'evaluation.json', manifest)
    if stage_package and stage_package['mode'] == 'isolated' and stage_package['isolated_contract']['evaluator_configuration_sha256'] != configuration.configuration_sha256:
        from .recording import error_record
        failure = ValueError('isolated evaluator configuration mismatch')
        values = manifest.model_dump(mode='json')
        values.update(quality_evaluation_status='failed', ended_at=now(), error=error_record(failure))
        storage.update_manifest(directory, 'evaluation.json', EvaluationManifest.model_validate(values))
        storage.freeze(directory)
        raise failure
    if preparation_metadata is not None:
        storage.write_json(directory, 'dataset_preparation.json', SnapshotPayload(data=preparation_metadata))
    custom_claims = []
    if stage_package is not None:
        from .models import ClaimRecord
        from .claim_models import StageSample
        storage.write_json(directory, 'stage_package.json', SnapshotPayload(data=stage_package))
        for row in stage_package['claims']:
            claim = ClaimRecord.model_validate({**row, 'evaluation_id': evaluation_id})
            storage.append_jsonl(directory, 'claims.jsonl', claim)
            custom_claims.append(claim)
        for row in stage_package['samples']:
            storage.append_jsonl(directory, 'stage_samples.jsonl', StageSample.model_validate({**row, 'evaluation_id': evaluation_id}))
    results = []
    clock = __import__('time').monotonic()
    from .models import EventRecord
    def event(kind, **fields):
        storage.append_jsonl(directory, 'events.jsonl', EventRecord(event_id=identifier('event'), run_id=run_id,
            evaluation_id=evaluation_id, event_type=kind, timestamp=now(), **fields))
    event('start', details={'scope': 'evaluation'})
    for original in samples:
        sample = original.model_copy(update={'evaluation_id': evaluation_id})
        storage.append_jsonl(directory, 'ragas_samples.jsonl', sample)
        event('start', details={'sample_id': sample.sample_id, 'metric': sample.metric_name.value})
        from .validation import validate_ragas_input
        problems = validate_ragas_input(run_directory, sample)
        if problems:
            reason = '; '.join(problems)
            result = RagasMetricResult(evaluation_id=evaluation_id, sample_id=sample.sample_id,
                                      metric_name=sample.metric_name, evaluation_status='missing', reason=reason)
            trace = RagasTraceRecord(evaluation_id=evaluation_id, sample_id=sample.sample_id,
                                    metric_name=sample.metric_name, raw_response={'attempts': []},
                                    mapping_status='unsupported', unavailable_reason=reason)
        else:
            result, trace = await adapter.score(sample)
        if stage_package is not None:
            from .ragas_claim_mapping import map_raw_claims
            records = map_raw_claims(trace, sample, custom_claims)
            for record in records:
                storage.append_jsonl(directory, 'ragas_claim_mappings.jsonl', SnapshotPayload(data=record))
        for attempt in trace.raw_response.get('attempts', []):
            if attempt['attempt'] > 1:
                event('retry', details={'sample_id': sample.sample_id, 'attempt': attempt['attempt'],
                                       'previous_invocation_id': attempt['previous_invocation_id']})
        event('failure' if result.evaluation_status == 'error' else 'end',
              details={'sample_id': sample.sample_id, 'metric': sample.metric_name.value,
                       'status': result.evaluation_status.value, 'reason': result.reason})
        storage.append_jsonl(directory, 'ragas_results.jsonl', result)
        trace.trace_snapshot = storage.write_snapshot(directory, identifier('trace') + '.json',
                                                      SnapshotPayload(data=trace.raw_response))
        storage.append_jsonl(directory, 'ragas_traces.jsonl', trace)
        results.append(result)
    complete = sum(all(r.evaluation_status in {'completed', 'not_applicable'} for r in results if r.sample_id == sid)
                   for sid in {s.sample_id for s in samples})
    values = manifest.model_dump(mode='json')
    storage.write_json(directory, 'metrics.json', MetricsRecord(run_id=run_id, evaluation_id=evaluation_id,
        scope='run', sample_count=count, ragas_results=results))
    event('end', details={'scope': 'evaluation', 'completed_sample_count': complete})
    values.update(ended_at=now(), duration_seconds=__import__('time').monotonic()-clock, completed_sample_count=complete,
                  quality_evaluation_status='completed' if complete == count else ('partial' if complete else 'failed'))
    storage.update_manifest(directory, 'evaluation.json', EvaluationManifest.model_validate(values))
    storage.freeze(directory)
    return directory
