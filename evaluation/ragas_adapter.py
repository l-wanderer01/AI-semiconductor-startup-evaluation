"""RAGAS 0.4.3 single_turn_ascore API를 프로젝트 계약으로 정규화한다."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

from .models import (EvaluationManifest, EvaluatorConfiguration, MetricConfiguration, ModelSettings, RunManifest, MetricsRecord,
                     PromptVersion, RagasMetricResult, RagasSampleInput, RagasTraceRecord)
from .recording import SnapshotPayload, digest, identifier, json_value, now
from .storage import EvaluationStorage, _redact_value
from .runtime import SCOPE, UsageCallback, UsageCollector

RAGAS_VERSION = '0.4.3'
ADAPTER_VERSION = '0.6.0'
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
    def __init__(self, usage=None):
        self.rows = []
        self.usage = usage

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if self.usage:
            self.usage.on_chat_model_start(serialized, messages, run_id=run_id, **kwargs)

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        if self.usage:
            self.usage.on_llm_start(serialized, prompts, run_id=run_id, **kwargs)

    def on_retry(self, retry_state, *, run_id, **kwargs):
        if self.usage:
            self.usage.on_retry(retry_state, run_id=run_id, **kwargs)

    def on_llm_error(self, error, *, run_id, **kwargs):
        if self.usage:
            self.usage.on_llm_error(error, run_id=run_id, **kwargs)
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        self.rows.append({'kind': 'chain_start', 'run_id': str(run_id), 'parent_run_id': str(parent_run_id) if parent_run_id else None,
                          'name': (serialized or {}).get('name'), 'input': json_value(inputs)})
    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self.rows.append({'kind': 'chain_end', 'run_id': str(run_id), 'output': json_value(outputs)})
    def on_llm_end(self, response, *, run_id, **kwargs):
        if self.usage:
            self.usage.on_llm_end(response, run_id=run_id, **kwargs)
        self.rows.append({'kind': 'llm_end', 'run_id': str(run_id), 'response': json_value(response)})
    def on_chain_error(self, error, *, run_id, **kwargs):
        self.rows.append({'kind': 'chain_error', 'run_id': str(run_id), 'error': str(error)})


class RagasAdapter:
    def __init__(self, *, llm, model: ModelSettings, timeout_seconds=120.0, max_retries=0):
        if type(max_retries) is not int or max_retries < 0 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('retries must be a non-negative integer and timeout must be positive/finite')
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
        self.llm_adapter = type(llm).__module__ + '.' + type(llm).__qualname__
        self.temperature_policy = {'single_completion': llm.get_temperature(1),
                                   'multiple_completions': llm.get_temperature(2)} if hasattr(llm, 'get_temperature') else None
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
        model = ModelSettings(provider='openai', model=model_name, temperature=0.01,
                              max_retries=0, timeout_seconds=120)
        llm = LangchainLLMWrapper(ChatOpenAI(model=model_name, temperature=0.01, max_retries=0, timeout=120),
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
                trace_limitations=['public callbacks only; exact raw/custom mapping is partial; native counts unsupported'],
            ))
        settings = {'ragas_version': RAGAS_VERSION, 'api_version': API_VERSION, 'adapter_version': ADAPTER_VERSION,
                    'schema_version': '0.1.0', 'model': self.model.model_dump(mode='json'),
                    'metrics': [m.model_dump(mode='json') for m in metric_configs],
                    'max_retries': self.max_retries, 'cache_enabled': False}
        runtime_settings = {'sample_timeout_seconds': self.timeout_seconds,
                            'llm_temperature_policy': self.temperature_policy,
                            'korean_prompt_method': 'explicit_instruction_and_language_metadata; English few-shot examples retained',
                            'input_lineage_policy': 'actual-request-query-v2',
                            'adapter_max_retries': self.max_retries,
                            'library_retry_attempts': getattr(getattr(next(iter(self.metrics.values())).llm, 'run_config', None), 'max_retries', None),
                            'prompt_parse_retries': 3,
                            'library_metric_retries': {name: getattr(metric, 'max_retries', None)
                                                       for name, metric in self.metrics.items()}}
        storage.write_snapshot(directory, 'adapter_settings.json', SnapshotPayload(data=runtime_settings))
        dependencies = {}
        for package in ('ragas', 'langchain-core', 'langchain-openai', 'openai', 'pydantic'):
            try:
                dependencies[package] = version(package)
            except PackageNotFoundError:
                dependencies[package] = 'not-installed'
        settings.update(llm_adapter=self.llm_adapter, dependencies=dependencies,
                        runtime_settings=runtime_settings)
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
            collector = SCOPE.get()
            usage = UsageCallback(collector, self.model, sample.metric_name.value, attempt) if collector else None
            capture = TraceCapture(usage)
            call_clock = __import__('time').monotonic()
            previous = invocation
            invocation = identifier('eval_inv')
            row = {'invocation_id': invocation, 'previous_invocation_id': previous,
                   'attempt': attempt, 'started_at': now().isoformat()}
            try:
                raw = await asyncio.wait_for(metric.single_turn_ascore(native, callbacks=[capture]), self.timeout_seconds)
                value = float(raw)
                row['returned_score'] = value if math.isfinite(value) else None
                if not math.isfinite(value):
                    status, value, reason = 'error', None, 'RAGAS returned an undefined score (NaN/Inf)'
                elif not 0 <= value <= 1:
                    raise ValueError('RAGAS score outside 0..1')
                else:
                    status, reason = 'completed', None
            except Exception as exc:
                status, value, reason = 'error', None, f'{type(exc).__name__}: {exc}'
                row['error'] = reason
            if usage:
                usage.close()
                if not usage.count:
                    # Callback-unsupported adapters are an unknown operation, never free evaluation.
                    collector.add(invocation_id=invocation, call_type='llm', provider=self.model.provider,
                        model=self.model.model, attempt=attempt, metric_name=sample.metric_name.value,
                        status='unknown', started_at=row['started_at'], ended_at=now(),
                        duration_seconds=__import__('time').monotonic()-call_clock)
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


async def _evaluate_inputs_impl(storage: EvaluationStorage, run_id: str, samples: list[RagasSampleInput],
                          adapter: RagasAdapter, *, dataset_version: str,
                          reference_sha256: str, input_sha256: str, evidence_sha256: str,
                          previous_evaluation_id=None, resume_reason=None, preparation_metadata=None, stage_package=None,
                          factual_judge=None, reference_dataset_directory=None, allow_synthetic=False,
                          support_judge=None, coverage_judge=None, _runtime=None):
    """불변 생성 run 아래 독립 평가를 만든다. 재개도 새 ID로 저장한다."""
    if coverage_judge is not None and factual_judge is None:
        raise ValueError('required information coverage requires independent custom factual evaluation')
    if factual_judge is not None:
        if stage_package is None or reference_dataset_directory is None:
            raise ValueError('custom factual evaluation requires stage package and independent reference dataset')
        from .factual import factual_pairs
        from .dataset import load_dataset, safe_path, sha256
        independent_dataset = load_dataset(reference_dataset_directory, require_ready=True, allow_synthetic=allow_synthetic)
        if stage_package['dataset'] is None or independent_dataset.manifest.sha256 != stage_package['dataset']['manifest']['sha256']:
            raise ValueError('independent reference dataset hash mismatch')
        independent_source_bytes = {}
        for source in independent_dataset.sources:
            content = safe_path(reference_dataset_directory, source.snapshot.path).read_bytes()
            if sha256(content) != source.snapshot.sha256:
                raise ValueError('independent source changed before evaluation')
            independent_source_bytes[source.source_id] = content
        samples = factual_pairs(stage_package, samples)
    grounding_bundle = None
    if support_judge is not None:
        if stage_package is None:
            raise ValueError('custom grounding requires stage package')
        from .grounding import prepare_grounding
        grounding_bundle = prepare_grounding(stage_package, storage.root / run_id)
    if not samples and factual_judge is None and support_judge is None:
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
    # Rule conformance is preserved beside RAGAS, never averaged into an LLM score.
    rule_source = run_directory / 'rule_conformance.json'
    if rule_source.is_file():
        from .validation import validate_run
        rule_errors = validate_run(run_directory)
        if rule_errors:
            raise ValueError('generation rule sources failed integrity checks: ' + '; '.join(rule_errors))
    evaluation_id = identifier('evaluation')
    directory = storage.create_evaluation(run_id, evaluation_id)
    _runtime['directory'] = directory
    for collector in (_runtime['ragas'], _runtime['custom']):
        collector.evaluation_id = evaluation_id
    if rule_source.is_file():
        storage.write_json(directory, 'rule_conformance.json', SnapshotPayload(data={
            'run_id': run_id, 'evaluation_id': evaluation_id,
            'generation_summary_sha256': digest(rule_source.read_bytes()),
            'generation_checks_sha256': digest((run_directory / 'checks.json').read_bytes()),
            'generation_summary': json.loads(rule_source.read_text())['data'],
            'ragas_combined': False,
        }))
    configuration = adapter.configuration(storage, directory)
    if factual_judge is not None:
        custom_config = factual_judge.configuration()
        from .dataset import canonical_bytes
        configuration.custom_factual_configuration = custom_config
        configuration.configuration_sha256 = digest(canonical_bytes({'ragas_configuration_sha256': configuration.configuration_sha256,
                                                                      'custom_factual_configuration': custom_config}))
        storage.write_json(directory, 'custom_factual_configuration.json', SnapshotPayload(data=custom_config))
    if support_judge is not None:
        from .dataset import canonical_bytes
        grounding_config = support_judge.configuration()
        configuration.custom_grounding_configuration = grounding_config
        configuration.configuration_sha256 = digest(canonical_bytes({'previous_configuration_sha256':configuration.configuration_sha256,
                                                                      'custom_grounding_configuration':grounding_config}))
        storage.write_json(directory,'custom_grounding_configuration.json',SnapshotPayload(data=grounding_config))
    if coverage_judge is not None:
        from .dataset import canonical_bytes
        coverage_config = coverage_judge.configuration()
        configuration.custom_coverage_configuration = coverage_config
        configuration.configuration_sha256 = digest(canonical_bytes({
            'previous_configuration_sha256': configuration.configuration_sha256,
            'custom_coverage_configuration': coverage_config}))
        storage.write_json(directory, 'custom_coverage_configuration.json', SnapshotPayload(data=coverage_config))
    sample_ids = {s.sample_id for s in samples}
    if factual_judge is not None or support_judge is not None:
        sample_ids.update(s['sample_id'] for s in stage_package['samples'])
    count = len(sample_ids)
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
    if grounding_bundle is not None:
        for doc in grounding_bundle['documents'].values():
            doc.snapshot = storage.write_text(directory, identifier('grounding_document')+'.txt',doc.text,media_type='text/plain')
            storage.append_jsonl(directory,'grounding_documents.jsonl',doc)
    ragas_clock = __import__('time').monotonic()
    _runtime['ragas'].endpoint = None
    _runtime['ragas'].clock, _runtime['ragas'].started_at = ragas_clock, now()
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
            with _runtime['ragas'].activate():
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
    _runtime['ragas'].stop(all(r.evaluation_status in {'completed', 'not_applicable'} for r in results),
                           'selected RAGAS metrics; after report delivery')
    _runtime['custom'].endpoint = None
    _runtime['custom'].clock, _runtime['custom'].started_at = __import__('time').monotonic(), now()
    custom_assessments, custom_summaries = [], []
    if factual_judge is not None:
        from .factual import evaluate_custom, paired_results
        try:
            custom_assessments, custom_summaries = evaluate_custom(stage_package, reference_dataset_directory, factual_judge,
                                                                  evaluation_id, allow_synthetic=allow_synthetic)
        except Exception as failure:
            from .recording import error_record
            event('failure', details={'scope': 'custom_factual', 'reason': str(failure)})
            values = manifest.model_dump(mode='json')
            values.update(quality_evaluation_status='failed', ended_at=now(), error=error_record(failure))
            storage.update_manifest(directory, 'evaluation.json', EvaluationManifest.model_validate(values))
            raise
        storage.write_json(directory, 'independent_reference_dataset.json', SnapshotPayload(data=independent_dataset.model_dump(mode='json')))
        source_snapshots = []
        for source in independent_dataset.sources:
            ref = storage.write_bytes(directory, identifier('independent_source') + '.txt',
                independent_source_bytes[source.source_id], media_type='text/plain')
            source_snapshots.append({'source_id': source.source_id, 'original_snapshot': source.snapshot.model_dump(mode='json'),
                                     'archived_snapshot': ref.model_dump(mode='json')})
        storage.write_json(directory, 'independent_sources.json', SnapshotPayload(data=source_snapshots))
        for assessment in custom_assessments:
            storage.append_jsonl(directory, 'fact_assessments.jsonl', assessment)
        storage.write_json(directory, 'custom_factual_metrics.json', SnapshotPayload(data=custom_summaries))
        for row in paired_results(stage_package, samples, results, custom_summaries):
            storage.append_jsonl(directory, 'factual_sample_results.jsonl', SnapshotPayload(data=row))
    grounding_by_sample = {}
    if grounding_bundle is not None:
        from .grounding import evaluate_grounding, diagnostic_results
        try:
            groundings,citations,grounding_summaries = evaluate_grounding(grounding_bundle,support_judge,evaluation_id)
        except Exception as failure:
            from .recording import error_record
            event('failure',details={'scope':'custom_grounding','reason':str(failure)})
            values=manifest.model_dump(mode='json')
            values.update(quality_evaluation_status='failed',ended_at=now(),error=error_record(failure))
            storage.update_manifest(directory,'evaluation.json',EvaluationManifest.model_validate(values))
            raise
        for row in groundings:
            storage.append_jsonl(directory,'grounding_assessments.jsonl',row)
        for row in citations:
            storage.append_jsonl(directory,'citation_assessments.jsonl',row)
        storage.write_json(directory,'grounding_metrics.json',SnapshotPayload(data=grounding_summaries))
        for row in diagnostic_results(grounding_bundle,samples,results):
            storage.append_jsonl(directory,'grounding_diagnostics.jsonl',SnapshotPayload(data=row))
        grounding_by_sample = {s['sample_id']:s for s in grounding_summaries}
    coverage_by_sample = {}
    coverage_scope_complete = True
    if coverage_judge is not None:
        from .coverage import evaluate_coverage, paired_coverage_results
        try:
            required_rows, coverage_summaries = evaluate_coverage(stage_package, independent_dataset,
                coverage_judge, custom_assessments, evaluation_id)
            for row in required_rows:
                storage.append_jsonl(directory, 'required_information_assessments.jsonl', row)
            storage.write_json(directory, 'required_information_metrics.json', SnapshotPayload(data=coverage_summaries))
            for row in paired_coverage_results(stage_package, results, coverage_summaries):
                storage.append_jsonl(directory, 'recall_coverage_results.jsonl', SnapshotPayload(data=row))
            coverage_by_sample = {s['key']: s for s in coverage_summaries if s['scope'] == 'sample'}
            coverage_scope_complete = all(s['status'] in {'completed', 'not_applicable'} for s in coverage_summaries)
        except Exception as failure:
            from .recording import error_record
            event('failure', details={'scope': 'required_information', 'reason': str(failure)})
            values = manifest.model_dump(mode='json')
            values.update(quality_evaluation_status='failed', ended_at=now(), error=error_record(failure))
            storage.update_manifest(directory, 'evaluation.json', EvaluationManifest.model_validate(values))
            raise
    custom_by_sample = {s['key']: s for s in custom_summaries if s['scope'] == 'sample'}
    complete = sum(all(r.evaluation_status in {'completed', 'not_applicable'} for r in results if r.sample_id == sid)
                   and (sid not in custom_by_sample or custom_by_sample[sid]['status'] in {'completed', 'not_applicable'})
                   and (sid not in grounding_by_sample or grounding_by_sample[sid]['status']=='completed')
                   and (sid not in coverage_by_sample or coverage_by_sample[sid]['status'] in {'completed', 'not_applicable'}) for sid in sample_ids)
    values = manifest.model_dump(mode='json')
    _runtime['metrics'] = MetricsRecord(run_id=run_id, evaluation_id=evaluation_id,
        scope='run', sample_count=count, ragas_results=results,
        ragas_runtime=_runtime['ragas'].summary(),
        custom_runtime=_runtime['custom'].summary())
    event('end', details={'scope': 'evaluation', 'completed_sample_count': complete})
    values.update(ended_at=now(), duration_seconds=__import__('time').monotonic()-clock, completed_sample_count=complete,
                  quality_evaluation_status='completed' if complete == count and coverage_scope_complete else ('partial' if complete else 'failed'))
    storage.update_manifest(directory, 'evaluation.json', EvaluationManifest.model_validate(values))
    return directory


async def evaluate_inputs(storage, run_id, samples, adapter, **kwargs):
    """Save independent usage/timings even when a metric or custom check fails."""
    runtime = {'ragas': UsageCollector(run_id, 'ragas_evaluation'),
               'custom': UsageCollector(run_id, 'custom_evaluation')}
    # Failure before the corresponding phase starts has no measured phase duration.
    for name in ('ragas', 'custom'):
        runtime[name].endpoint = dict(duration_seconds=None, status='failed', boundary=name+' phase not started')
    success, failure = False, None
    try:
        with runtime['custom'].activate():
            directory = await _evaluate_inputs_impl(storage, run_id, samples, adapter, _runtime=runtime, **kwargs)
        success = True
        return directory
    except BaseException as exc:
        failure = exc
        raise
    finally:
        directory = runtime.get('directory')
        if directory:
            manifest_path = directory / 'evaluation.json'
            if manifest_path.exists():
                manifest = EvaluationManifest.model_validate_json(manifest_path.read_text())
                if not success and manifest.quality_evaluation_status == 'running':
                    values = manifest.model_dump(mode='json')
                    from .recording import error_record
                    values.update(quality_evaluation_status='failed', ended_at=now(), error=error_record(failure))
                    storage.update_manifest(directory, 'evaluation.json', EvaluationManifest.model_validate(values))
            for name in ('ragas', 'custom'):
                collector = runtime[name]
                completed = success and (not manifest_path.exists() or manifest.quality_evaluation_status == 'completed')
                collector.stop(completed, 'post-delivery ' + name + ' evaluation')
                collector.save(storage, directory, name + '_runtime.json')
            metrics = runtime.get('metrics') or MetricsRecord(
                run_id=run_id, evaluation_id=directory.name, scope='run')
            storage.write_json(directory, 'metrics.json', metrics.model_copy(update={
                'ragas_runtime': runtime['ragas'].summary(), 'custom_runtime': runtime['custom'].summary()}))
            if manifest_path.exists():
                storage.freeze(directory)
            elif failure:
                from .recording import error_record
                storage.write_json(directory, 'initialization_failure.json', error_record(failure))
