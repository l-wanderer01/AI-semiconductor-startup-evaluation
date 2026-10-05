"""계측 전용 기록기. 판단·점수·검색 결과를 변경하지 않고 실제 호출을 보존한다."""
from __future__ import annotations

import functools
import hashlib
import inspect
from importlib.metadata import PackageNotFoundError, version
import json
import math
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, JsonValue

from .models import (
    AdditionalResearchRecord, AgentOutputRecord, ArtifactRecord, DeliveredContext,
    ErrorRecord, EventRecord, EvidenceRecord, InvocationRecord, MetricsRecord,
    ModelSettings, PromptVersion, RetrievalRecord, RetrievedContext, RunManifest,
)
from .storage import EvaluationStorage, _redact_text, _redact_value

CURRENT: ContextVar[RunRecorder | None] = ContextVar('evaluation_run', default=None)
STACK: ContextVar[tuple[str, ...]] = ContextVar('evaluation_stack', default=())
COMPANY: ContextVar[str | None] = ContextVar('evaluation_company', default=None)


def now():
    return datetime.now(timezone.utc)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identifier(prefix: str) -> str:
    return f'{prefix}_{uuid4().hex}'


def json_value(value):
    """실제 값만 저장하고 객체 repr에서 인증 정보가 노출되는 것을 막는다."""
    if isinstance(value, BaseModel):
        return json_value(value.model_dump(mode='json'))
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return {'non_finite_float': str(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {'type': type(value).__name__, 'value_unavailable': True}


class SnapshotPayload(BaseModel):
    data: JsonValue


def error_record(exc, invocation_id=None):
    return ErrorRecord(error_type=type(exc).__name__, message=_redact_text(str(exc)) or type(exc).__name__,
                       invocation_id=invocation_id)


@contextmanager
def company_scope(company):
    token = COMPANY.set(company)
    try:
        yield
    finally:
        COMPANY.reset(token)


class RunRecorder:
    def __init__(self, *, root: Path, execution_path: str, inputs, settings: dict,
                 requested_formats: list[str], repository: Path, role='baseline'):
        from .experiment import EXPERIMENT, EXPERIMENT_RUNS
        experiment = EXPERIMENT.get()
        if experiment is not None:
            role = experiment['role']
        from .runtime import UsageCollector
        self.start_clock = time.monotonic()
        self.started_at = now()
        self.storage = EvaluationStorage(root)
        self.directory = self.storage.create_run(identifier('run'))
        if EXPERIMENT_RUNS.get() is not None:
            EXPERIMENT_RUNS.get().append(str(self.directory))
        self.run_id = self.directory.name
        self.usage = UsageCollector(self.run_id, 'generation')
        self.usage.clock, self.usage.started_at = self.start_clock, self.started_at
        self.records = []
        self.inflight = {}
        self.evidence = {}
        self.contexts = []
        self.retrievals = []
        self.outputs = []
        self.research = []
        self.artifacts = []
        self.finished = False
        self.fallback_reasons = []
        self.candidates = []
        self.settings = settings
        self.path = execution_path
        self.repository = repository.resolve()
        self.input_ref = self.snapshot('input_snapshot.json', inputs)
        if experiment is not None:
            self.snapshot('experiment.json', experiment)
        configuration = self.snapshot('settings_snapshot.json', settings)
        packages = {}
        for name in ['pydantic', 'langchain', 'langchain-core', 'langchain-openai', 'langgraph',
                     'openai', 'requests', 'ragas', 'qdrant-client', 'sentence-transformers',
                     'reportlab', 'pypdf']:
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                packages[name] = None
        import sys
        self.snapshot('environment_snapshot.json', {'python': sys.version, 'packages': packages})
        code = self._code_snapshot()
        self.manifest = RunManifest(
            run_id=self.run_id, comparison_role=role, execution_path=execution_path,
            contract_version='0.1.0', schema_version='0.1.0',
            policy_version=f'{execution_path}-baseline-v1', workflow_version=f'{execution_path}-v1',
            commit_sha=code['commit'], dirty=code['dirty'], dirty_diff_sha256=code['diff_hash'],
            included_paths=code['included'], excluded_paths=['.env', 'outputs', '.venv', '.venv-evaluation'],
            as_of_date=now().date(), input_snapshot=self.input_ref, settings_sha256=configuration.sha256,
            requested_formats=requested_formats, llm_enabled=settings.get('llm_enabled', True),
            live_research_enabled=settings.get('live_research_enabled', False),
            generation_model=ModelSettings(provider='openai', model=settings.get('model', 'unknown'),
                                           temperature=settings.get('temperature'),
                                           max_retries=settings.get('max_retries', 0)),
            started_at=self.started_at, generation_status='running',
        )
        self.storage.write_json(self.directory, 'run.json', self.manifest)
        self.event('start', details={'scope': 'run', 'instrumentation_only': True})

    def _code_snapshot(self):
        def git(*args):
            result = subprocess.run(['git', '-C', str(self.repository), *args],
                                    capture_output=True, check=True)
            return result.stdout
        try:
            commit = git('rev-parse', 'HEAD').decode().strip()
            diff = git('diff', 'HEAD', '--', 'app.py', 'agents', 'investment_pipeline',
                       'evaluation', 'prompts', 'requirements.txt', 'requirements-evaluation.txt',
                       'docs/evaluation')
            dirty = bool(git('status', '--porcelain'))
        except (OSError, subprocess.CalledProcessError):
            commit, diff, dirty = 'unavailable', b'', True
        # 미추적 계측 코드도 해시에 포함한다. 인증 파일과 생성물은 제외한다.
        files = {}
        for folder in ['agents', 'investment_pipeline', 'evaluation', 'prompts']:
            for path in sorted((self.repository / folder).rglob('*')):
                if path.is_file() and path.suffix in {'.py', '.md'}:
                    files[str(path.relative_to(self.repository))] = digest(path.read_bytes())
        for name in ['app.py', 'requirements.txt', 'requirements-evaluation.txt', 'docs/evaluation/contract.md']:
            path = self.repository / name
            if path.is_file():
                files[name] = digest(path.read_bytes())
        ref = self.snapshot('code_snapshot.json', {'commit': commit, 'files': files,
                                                   'tracked_diff': diff.decode(errors='replace')})
        return dict(commit=commit, dirty=dirty, diff_hash=ref.sha256, included=list(files))

    def snapshot(self, filename, value):
        return self.storage.write_snapshot(self.directory, filename, SnapshotPayload(data=json_value(value)))

    @contextmanager
    def activate(self):
        token = CURRENT.set(self)
        stack_token = STACK.set(())
        company_token = COMPANY.set(None)
        try:
            yield self
        finally:
            COMPANY.reset(company_token)
            STACK.reset(stack_token)
            CURRENT.reset(token)

    def note_fallback(self, reason):
        self.fallback_reasons.append(reason)
        self.event('fallback', STACK.get()[-1] if STACK.get() else None, details={'reason': reason})

    def event(self, event_type, invocation_id=None, **fields):
        event = EventRecord(event_id=identifier('event'), run_id=self.run_id,
                            invocation_id=invocation_id, event_type=event_type, timestamp=now(), **fields)
        self.storage.append_jsonl(self.directory, 'events.jsonl', event)

    @contextmanager
    def span(self, operation, inputs, *, kind='function', company=None, model=None):
        invocation_id = identifier('inv')
        company = company if company is not None else COMPANY.get()
        reevaluation = operation in {'technical_eval', 'market_eval'} or operation.startswith(('technical_eval.', 'market_eval.'))
        previous = next((r for r in reversed(self.records)
                         if reevaluation and r.operation_id == operation and r.company_id == company and r.status == 'succeeded'), None)
        record = InvocationRecord(
            run_id=self.run_id, operation_id=operation, invocation_id=invocation_id,
            parent_invocation_id=STACK.get()[-1] if STACK.get() else None,
            previous_invocation_id=previous.invocation_id if previous else None,
            link_reason='reevaluation' if previous else None,
            invocation_type=kind, agent_id=operation, node_id=operation if kind == 'node' else None,
            company_id=company, section_id=operation, status='running', started_at=now(),
            input_schema_version='0.1.0', output_schema_version='0.1.0', model=model,
            input_snapshot=self.snapshot(f'{invocation_id}_input.json', inputs),
        )
        self.inflight[invocation_id] = record
        state = {'record': record, 'output': None, 'fallback': None}
        clock = time.monotonic()
        self.event('start', invocation_id)
        stack_token = STACK.set(STACK.get() + (invocation_id,))
        company_token = COMPANY.set(company)
        try:
            yield state
        except BaseException as exc:
            record.status = 'failed'
            record.error = error_record(exc, invocation_id)
            self.event('failure', invocation_id, error=record.error, duration_seconds=time.monotonic() - clock)
            raise
        else:
            record.status = 'succeeded'
            record.output_snapshot = self.snapshot(f'{invocation_id}_output.json', state['output'])
            record.state_update = record.output_snapshot if kind in {'node', 'function'} else None
            if isinstance(state['output'], dict):
                route = state['output'].get('supervisor_route_state')
                if operation == 'ranking':
                    route = (state['output'].get('ranking_selection_state') or {}).get('branch')
                if route:
                    record.route = route
                    self.event('route_selected', invocation_id, route=route)
            output = AgentOutputRecord(run_id=self.run_id, invocation_id=invocation_id,
                                       agent_id=operation, company_id=company, section_id=operation,
                                       attempt=1, schema_version='0.1.0', snapshot=record.output_snapshot,
                                       response=json_value(state['output']), evidence_ids=record.evidence_ids,
                                       context_ids=record.context_ids)
            self.outputs.append(output)
            self.storage.append_jsonl(self.directory, 'agent_outputs.jsonl', output)
            if record.state_update:
                self.event('state_updated', invocation_id, state_update=record.state_update)
            self.event('end', invocation_id, duration_seconds=time.monotonic() - clock)
        finally:
            if state['fallback']:
                record.fallback_used = True
                record.fallback_reason = state['fallback']
                self.fallback_reasons.append(state['fallback'])
                self.event('fallback', invocation_id, details={'reason': state['fallback']})
            record.ended_at = now()
            record.duration_seconds = time.monotonic() - clock
            self.inflight.pop(invocation_id, None)
            self.records.append(record)
            self.storage.append_jsonl(self.directory, 'invocations.jsonl', record)
            COMPANY.reset(company_token)
            STACK.reset(stack_token)

    def add_evidence(self, text, *, source, title='', company=None, url=None):
        text = _redact_text(text)
        key = digest((source + '\n' + text).encode())
        evidence_id = 'evidence_' + key
        if evidence_id not in self.evidence:
            snapshot = self.storage.write_text(self.directory, f'{evidence_id}.txt', text,
                                               media_type='text/plain')
            self.evidence[evidence_id] = EvidenceRecord(
                evidence_id=evidence_id, source_id='source_' + digest(source.encode()), title=title or source,
                company_ids=[company] if company else [], url=url, collected_at=now(),
                as_of_date=self.manifest.as_of_date, snapshot=snapshot, content=text,
            )
        elif company and company not in self.evidence[evidence_id].company_ids:
            self.evidence[evidence_id].company_ids.append(company)
        return self.evidence[evidence_id]

    def retrieval(self, query, docs, invocation, *, web=False, cache_hit=None, error=None):
        candidates = []
        for rank, doc in enumerate(docs, 1):
            if web:
                text, source, title, url = doc.content, doc.url, doc.title, doc.url
                score = doc.score
            else:
                text = doc.page_content
                source = str(doc.metadata.get('source', 'unknown'))
                title = str(doc.metadata.get('title', source))
                url = source if source.startswith('http') else None
                score = None
            evidence = self.add_evidence(text, source=source, title=title, company=COMPANY.get(), url=url)
            candidates.append(RetrievedContext(context_id=identifier('context'), evidence_id=evidence.evidence_id,
                                               source_id=evidence.source_id, rank=rank, text=_redact_text(text),
                                               content_sha256=digest(_redact_text(text).encode()), score=score))
        row = RetrievalRecord(run_id=self.run_id, invocation_id=invocation.invocation_id,
                              company_id=COMPANY.get(), query=query, candidates=candidates,
                              started_at=invocation.started_at, ended_at=now(), cache_hit=cache_hit, error=error)
        self.retrievals.append(row)
        self.storage.append_jsonl(self.directory, 'retrievals.jsonl', row)
        invocation.evidence_ids = [c.evidence_id for c in candidates]

    def delivered(self, text, record):
        text = _redact_text(text)
        # 실제 입력에 포함된 원문만 연결한다. 기업별 문맥을 합치지 않는다.
        evidence_ids = [e.evidence_id for e in self.evidence.values() if e.content and e.content in text]
        def text_leaves(value):
            if isinstance(value, str):
                return [value] if len(value.strip()) >= 20 else []
            if isinstance(value, dict):
                return [leaf for v in value.values() for leaf in text_leaves(v)]
            if isinstance(value, list):
                return [leaf for v in value for leaf in text_leaves(v)]
            return []
        upstream = [r.invocation_id for r in self.outputs
                    if (record.company_id is None or r.company_id in {record.company_id, None})
                    and any(leaf in text for leaf in text_leaves(r.response))]
        retrieval_ids = [r.invocation_id for r in self.retrievals
                         if any(c.evidence_id in evidence_ids for c in r.candidates)]
        row = DeliveredContext(context_id=identifier('delivered'), invocation_id=record.invocation_id,
                               context_kind='mixed' if upstream and evidence_ids else ('intermediate_analysis' if upstream else 'source_evidence'), text=text,
                               sha256=digest(text.encode()), evidence_ids=evidence_ids,
                               retrieval_invocation_ids=retrieval_ids,
                               upstream_invocation_ids=upstream)
        record.evidence_ids = evidence_ids
        record.context_ids = [row.context_id]
        self.contexts.append(row)
        self.storage.append_jsonl(self.directory, 'delivered_contexts.jsonl', row)
        ref = self.storage.write_text(self.directory, f'{record.invocation_id}_prompt.txt', text,
                                      media_type='text/plain')
        record.prompt = PromptVersion(prompt_id=record.operation_id, version='actual-input-v1',
                                       sha256=ref.sha256, snapshot=ref)

    def additional_research(self, operation, state, output, record):
        section = 'technology' if operation.startswith('technical') else 'market'
        notes = output.get(f'{operation}_state', [])
        self.research.append(AdditionalResearchRecord(
            run_id=self.run_id, company_id=record.company_id, section_id=section,
            invocation_id=record.invocation_id, trigger='score_below_4', questions=notes,
            input_updated=bool(notes), state_update=record.output_snapshot,
            outcome='no_new_evidence', termination_reason='baseline_notes_only_then_recheck',
            policy_version=self.manifest.policy_version,
        ))

    def artifact(self, path: Path, filename: str, format: str):
        try:
            data = path.read_bytes()
            if not data:
                raise ValueError('empty artifact')
            if format == 'json':
                json.loads(data)
            elif format == 'md':
                data.decode('utf-8')
            elif format == 'pdf':
                from pypdf import PdfReader
                if len(PdfReader(path).pages) == 0:
                    raise ValueError('PDF has no pages')
            ref = self.storage.write_bytes(self.directory, filename, data,
                                           media_type={'json': 'application/json', 'md': 'text/markdown',
                                                       'pdf': 'application/pdf'}[format]) if format == 'pdf' else (
                self.storage.write_text(self.directory, filename, data.decode(),
                                        media_type='application/json' if format == 'json' else 'text/markdown'))
            stored = Path(ref.path).read_bytes()
            item = ArtifactRecord(artifact_id=identifier('artifact'), format=format, path=ref.path,
                                  status='saved', sha256=ref.sha256, size_bytes=len(stored), parseable=True)
        except Exception as exc:
            item = ArtifactRecord(artifact_id=identifier('artifact'), format=format, path=str(path),
                                  status='error', error=error_record(exc))
        self.artifacts.append(item)
        return item

    def _cache_state(self):
        states = {r.cache_hit for r in self.retrievals if r.cache_hit is not None}
        return 'mixed' if len(states) == 2 else ('warm' if states == {True} else ('cold' if states == {False} else 'unknown'))

    def generation_complete(self, exc=None):
        success = (exc is None and set(self.manifest.requested_formats).issubset(
            {a.format for a in self.artifacts if a.status == 'saved'})
            and all(a.status == 'saved' for a in self.artifacts if a.required))
        self.usage.stop(success, 'request accepted -> requested report formats and state saved')

    def finish(self, result=None, exc=None):
        if self.finished:
            return
        self.generation_complete(exc)
        custom_clock, custom_start = time.monotonic(), now()
        from .workflow import build_workflow, check_workflow
        from .validation import validate_run
        saved = {a.format for a in self.artifacts}
        for format in self.manifest.requested_formats:
            if format not in saved:
                self.artifacts.append(ArtifactRecord(artifact_id=identifier('artifact'), format=format,
                    path=str(self.directory / f'missing_artifact.{format}'), status='error',
                    error=error_record(exc or ValueError('requested artifact was not saved'))))
        evidence_ref = self.storage.write_snapshot(self.directory, 'evidence.json', list(self.evidence.values()))
        workflow = build_workflow(self)
        self.storage.write_json(self.directory, 'workflow.json', workflow)
        operations = {r.operation_id for r in self.records}
        for step in workflow.steps:
            if step.step_id not in operations and not step.required:
                self.event('skip', details={'operation_id': step.step_id,
                                           'reason': 'branch_not_selected_or_disabled',
                                           'enabled': step.enabled, 'supported': step.supported})
        self.storage.write_json(self.directory, 'decisions.json', SnapshotPayload(data=json_value(result)))
        # 품질 평가는 아직 실행하지 않았다. 빈 기록을 정상 점수로 대체하지 않는다.
        self.storage.write_text(self.directory, 'claims.jsonl', '', media_type='application/x-ndjson')
        checks = check_workflow(self, workflow, result)
        from .rules import check_recorded_run, load_spec
        rule_checks, rule_summary = check_recorded_run(self, result)
        policy_ref = self.storage.write_snapshot(self.directory, 'rule_policy.json', SnapshotPayload(data=load_spec()))
        rule_summary['policy_snapshot'] = policy_ref.model_dump(mode='json')
        checks_ref = self.storage.write_snapshot(self.directory, 'checks.json', checks + rule_checks)
        rule_summary['source_snapshots'].append(checks_ref.model_dump(mode='json'))
        self.storage.write_json(self.directory, 'rule_conformance.json', SnapshotPayload(data=rule_summary))
        self.storage.write_json(self.directory, 'additional_research.json', self.research)
        from .models import CheckCounts
        baseline_counts = rule_summary['policies'][f'{self.path}-baseline-v1']['counts']
        from .runtime import UsageCollector
        custom = UsageCollector(self.run_id, 'custom_evaluation', prices=self.usage.prices)
        custom.clock, custom.started_at = custom_clock, custom_start
        custom.stop(not bool(exc or any(c.status not in {'pass', 'not_applicable'} for c in checks)),
                    'post-delivery workflow and rule checks')
        custom.save(self.storage, self.directory, 'custom_runtime.json')
        self.storage.write_json(self.directory, 'metrics.json', MetricsRecord(
            run_id=self.run_id, scope='run', rule_counts=CheckCounts.model_validate(baseline_counts),
            generation_runtime=self.usage.summary(), custom_runtime=custom.summary()))
        self.usage.save(self.storage, self.directory, 'generation_runtime.json')
        self.event('failure' if exc else 'end', error=error_record(exc) if exc else None,
                   details={'scope': 'run'})
        values = self.manifest.model_dump(mode='json')
        values.pop('execution_succeeded', None)
        formats_saved = {a.format for a in self.artifacts if a.status == 'saved'}
        generation_ok = (exc is None and set(self.manifest.requested_formats).issubset(formats_saved)
                         and all(a.status == 'saved' for a in self.artifacts if a.required))
        values.update(ended_at=now(), duration_seconds=time.monotonic() - self.start_clock,
                      evidence_snapshot=evidence_ref, candidate_company_ids=self.candidates,
                      artifacts=self.artifacts, fallback_used=bool(self.fallback_reasons),
                      fallback_reasons=sorted(set(self.fallback_reasons)),
                      prompts=[r.prompt for r in self.records if r.prompt],
                      cache_state=self._cache_state(),
                      generation_status='succeeded' if generation_ok else 'failed',
                      workflow_status='passed' if all(c.status in {'pass', 'not_applicable'} for c in checks) else 'failed',
                      error=error_record(exc) if exc else None)
        manifest = RunManifest.model_validate(values)
        self.storage.update_manifest(self.directory, 'run.json', manifest)
        self.manifest = manifest
        validation = validate_run(self.directory)
        self.storage.write_json(self.directory, 'validation.json', SnapshotPayload(data=validation))
        if validation:
            values.update(workflow_status='failed')
            self.manifest = RunManifest.model_validate(values)
            self.storage.update_manifest(self.directory, 'run.json', self.manifest)
        self.storage.freeze(self.directory)
        self.finished = True


def traced(operation=None, *, kind='function'):
    def decorate(function):
        name = operation or function.__name__
        signature = inspect.signature(function)
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            recorder = CURRENT.get()
            if recorder is None:
                return function(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            inputs = {k: json_value(v) for k, v in bound.arguments.items() if k != 'self'}
            company = COMPANY.get()
            for value in bound.arguments.values():
                if hasattr(value, 'name') and type(value).__name__ == 'CompanyProfile':
                    company = value.name
                if isinstance(value, dict) and 'selected_company_context_state' in value:
                    company = value['selected_company_context_state'].name
            with recorder.span(name, inputs, kind=kind, company=company) as span:
                if (name == 'decision' and recorder.path == 'investment_pipeline'
                        and not (recorder.directory / 'scoring_policy_observed.json').exists()):
                    from investment_pipeline.scoring import STAGE_WEIGHTS
                    from investment_pipeline.config import settings
                    recorder.snapshot('scoring_policy_observed.json', {
                        'weights': STAGE_WEIGHTS,
                        'settings': {'selective_dd_threshold': settings.selective_dd_threshold,
                                     'high_priority_threshold': settings.high_priority_threshold},
                    })
                result = function(*args, **kwargs)
                span['output'] = json_value(result)
                if name in {'_search_company', '_search_market'}:
                    for item in result.evidence:
                        recorder.add_evidence(item.content, source=item.url, title=item.title,
                                              company=company, url=item.url)
                if name == '_write_common_outputs':
                    timestamp = bound.arguments['timestamp']
                    output_dir = bound.arguments['self'].output_dir
                    for prefix, extension in [('market_analysis', 'md'), ('evaluations', 'json'),
                                              ('agent_evaluations', 'json'), ('policy_decision', 'json')]:
                        recorder.artifact(output_dir / f'{prefix}_{timestamp}.{extension}',
                                          f'{prefix}_original.{extension}', extension)
                if name == 'extract_companies':
                    recorder.candidates = result.companies
                if name in {'invoke_structured', 'invoke_text'} and result is None:
                    span['fallback'] = 'llm_disabled_unavailable_or_failed'
                if name == 'extract_companies' and not result.companies:
                    span['fallback'] = 'company_extraction_empty'
                if name in {'route_after_policy', 'branch_selector', 'company_route'}:
                    span['record'].route = result
                    recorder.event('route_selected', span['record'].invocation_id, route=result)
            if name in {'technical_additional_research', 'market_additional_research'}:
                recorder.additional_research(name, inputs, span['output'], span['record'])
            return result
        return wrapped
    return decorate


class ObservedRunnable:
    """원래 Runnable을 그대로 호출한다. 보이지 않는 내부 재시도를 추정하지 않는다."""
    def __init__(self, runnable, *, kind, name, model=None):
        self.runnable, self.kind, self.name, self.model = runnable, kind, name, model

    def __getattr__(self, name):
        return getattr(self.runnable, name)

    def with_structured_output(self, *args, **kwargs):
        return ObservedRunnable(self.runnable.with_structured_output(*args, **kwargs), kind=self.kind,
                                name=self.name + '_structured', model=self.model)

    def invoke(self, inputs, *args, **kwargs):
        recorder = CURRENT.get()
        if recorder is None:
            return self.runnable.invoke(inputs, *args, **kwargs)
        company = COMPANY.get()
        if company is None and self.kind == 'llm':
            import re
            messages = inputs if isinstance(inputs, str) else '\n'.join(getattr(m, 'content', '') for m in inputs)
            match = re.search(r'회사명: ([^\n]+)', messages)
            company = match.group(1) if match else None
        operation = self.name
        if self.kind == 'llm':
            roles = [recorder.inflight[i].operation_id for i in STACK.get() if i in recorder.inflight]
            role = next((r for r in reversed(roles) if r.startswith('evaluate_') or r in {
                'technical_eval', 'market_eval', 'team_eval', 'risk_eval', 'competition_eval',
                'company_research', 'market_research', 'generate_investment_report', 'generate_hold_report',
                'rank_companies', 'extract_companies', 'analyze_market', 'polish_report_to_korean'}), None)
            if role:
                operation = role + '.' + self.name
        with recorder.span(operation, json_value(inputs), kind=self.kind, model=self.model, company=company) as span:
            if self.kind == 'llm':
                text = inputs if isinstance(inputs, str) else '\n'.join(
                    f'{getattr(item, "type", "message")}: {getattr(item, "content", "")}' for item in inputs)
                recorder.delivered(text, span['record'])
            try:
                if self.kind == 'llm':
                    from .runtime import invoke_usage
                    result = invoke_usage(self.runnable, inputs, args, kwargs, recorder.usage, self.model)
                else:
                    result = self.runnable.invoke(inputs, *args, **kwargs)
            except Exception as exc:
                if self.kind == 'retrieval':
                    recorder.retrieval(str(inputs), [], span['record'], error=error_record(exc, span['record'].invocation_id))
                raise
            span['output'] = json_value(result)
            if self.kind == 'retrieval':
                recorder.retrieval(str(inputs), result, span['record'])
        return result
