"""#29 generation harness, baseline preservation, paired evaluation and reports."""
import argparse
import asyncio
import hashlib
from contextlib import nullcontext
import importlib
import typing
import json
import tempfile
from pathlib import Path

from .experiment import EXPERIMENT, EXPERIMENT_RUNS, SearchReplay, canonical, export_search
from .paired import campaign_report, file_hash, paired_report, preserve, read, verify_receipt, write_report
from .storage import _redact_value


def save_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as file:
        json.dump(_redact_value(value), file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write('\n')


def run_experiment(entrypoint, kwargs, *, role, data_mode, replay_path=None, receipt=None):
    if role == 'refactored' and receipt is None:
        raise ValueError('refactored generation requires a preserved baseline receipt')
    if receipt:
        verify_receipt(receipt)
    if data_mode == 'frozen' and replay_path is None:
        raise ValueError('frozen generation requires a search replay snapshot (empty is explicit)')
    if data_mode == 'live' and replay_path is not None:
        raise ValueError('live generation cannot use frozen replay')
    replay = SearchReplay.load(replay_path) if replay_path else None
    token = EXPERIMENT.set({'role': role, 'data_mode': data_mode, 'entrypoint': entrypoint,
        'kwargs': kwargs, 'search_snapshot_sha256': file_hash(replay_path) if replay_path else None,
        'baseline_run_id': receipt['run_id'] if receipt else None})
    runs = []
    runs_token = EXPERIMENT_RUNS.set(runs)
    try:
        # Import under the scope too: service initialization may perform search.
        with replay.activate() if replay else nullcontext():
            module, name = entrypoint.split(':', 1)
            function = getattr(importlib.import_module(module), name)
            annotations = typing.get_type_hints(function)
            arguments = {key: Path(value) if annotations.get(key) is Path else value for key, value in kwargs.items()}
            function(**arguments)
            if not runs:
                raise ValueError('entrypoint did not create an instrumented generation run')
            return {'role': role, 'data_mode': data_mode, 'run_directories': runs}
    finally:
        EXPERIMENT.reset(token)
        EXPERIMENT_RUNS.reset(runs_token)
        if receipt:
            verify_receipt(receipt)


async def evaluate_pair(run_directories, package_directories, dataset_directory, adapter, *,
                        output, receipt, resume=None, reason=None, factual_judge=None,
                        support_judge=None, coverage_judge=None, allow_synthetic=False):
    """New IDs on every resume; preserve successful and failed previous attempts."""
    from .dataset import load_dataset
    from .models import RunManifest
    from .ragas_adapter import evaluate_inputs
    from .stage_samples import load_stage_package, ragas_inputs
    from .storage import EvaluationStorage
    if verify_receipt(receipt) != Path(run_directories[0]).resolve():
        raise ValueError('baseline receipt scope mismatch')
    if Path(run_directories[0]).resolve() == Path(run_directories[1]).resolve():
        raise ValueError('paired evaluation requires distinct generation runs')
    dataset = load_dataset(dataset_directory, require_ready=True, allow_synthetic=allow_synthetic)
    packages = [load_stage_package(Path(p), Path(r)) for r, p in zip(run_directories, package_directories, strict=True)]
    if packages[0]['mode'] != packages[1]['mode']:
        raise ValueError('paired evaluation mode mismatch')
    for package in packages:
        if not package.get('dataset') or package['dataset']['manifest']['sha256'] != dataset.manifest.sha256:
            raise ValueError('both packages must use the same reviewed reference dataset')
    previous = read(Path(resume)) if resume else None
    request = {'run_directories': [str(Path(d).resolve()) for d in run_directories],
        'package_directories': [str(Path(d).resolve()) for d in package_directories],
        'package_sha256': [hashlib.sha256(canonical(p)).hexdigest() for p in packages],
        'dataset_sha256': dataset.manifest.sha256,
        'adapter_configuration_sha256': evaluator_fingerprint(adapter, factual_judge, support_judge, coverage_judge),
        'judge_configurations': [j.configuration() if j else None for j in (factual_judge, support_judge, coverage_judge)]}
    if previous and (not reason or previous['request'] != request):
        raise ValueError('resume needs a reason and unchanged runs/packages/reference/evaluator; new references require a fresh BOTH-side evaluation')
    journal = {'schema_version': 'paired-evaluation-attempt-v1', 'request': request,
               'previous_attempt': str(resume) if resume else None, 'reason': reason, 'evaluations': [], 'status': 'running'}
    output = Path(output)
    save_new(output, journal)
    def checkpoint():
        temporary = output.with_name(output.name + '.tmp')
        with temporary.open('w') as file:
            json.dump(_redact_value(journal), file, ensure_ascii=False, indent=2)
            file.write('\n')
        temporary.replace(output)
    try:
        for index, (run, package) in enumerate(zip(run_directories, packages, strict=True)):
            run = Path(run).resolve()
            manifest = RunManifest.model_validate_json((run / 'run.json').read_text())
            old = previous['evaluations'][index] if previous and len(previous['evaluations']) > index else None
            existing = set((run / 'evaluations').glob('*')) if (run / 'evaluations').exists() else set()
            try:
                evaluated = await evaluate_inputs(EvaluationStorage(run.parent), manifest.run_id,
                    ragas_inputs(package), adapter, dataset_version=dataset.manifest.version,
                    reference_sha256=dataset.manifest.sha256, input_sha256=manifest.input_snapshot.sha256,
                    evidence_sha256=manifest.evidence_snapshot.sha256, stage_package=package,
                    reference_dataset_directory=dataset_directory, factual_judge=factual_judge,
                    support_judge=support_judge, coverage_judge=coverage_judge, allow_synthetic=allow_synthetic,
                    previous_evaluation_id=Path(old).name if old else None, resume_reason=reason if old else None)
                journal['evaluations'].append(str(evaluated))
            except BaseException:
                created = set((run / 'evaluations').glob('*')) - existing
                if len(created) == 1:
                    journal['evaluations'].append(str(created.pop()))
                raise
            checkpoint()
        journal['status'] = 'completed'
    except BaseException as error:
        journal.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        checkpoint()
        verify_receipt(receipt)
    return journal


def adapter_fingerprint(adapter):
    # Use the same official adapter serializer; temporary prompt snapshot paths
    # are excluded by configuration() from its reproducible fingerprint.
    from .storage import EvaluationStorage
    with tempfile.TemporaryDirectory() as temporary:
        storage = EvaluationStorage(Path(temporary))
        return adapter.configuration(storage, storage.create_run('configuration')).configuration_sha256


def evaluator_fingerprint(adapter, factual_judge=None, support_judge=None, coverage_judge=None):
    """The same sequential custom configuration hashes used by evaluate_inputs."""
    fingerprint = adapter_fingerprint(adapter)
    for field, previous_key, judge in (
        ('custom_factual_configuration', 'ragas_configuration_sha256', factual_judge),
        ('custom_grounding_configuration', 'previous_configuration_sha256', support_judge),
        ('custom_coverage_configuration', 'previous_configuration_sha256', coverage_judge)):
        if judge is not None:
            fingerprint = hashlib.sha256(canonical({previous_key: fingerprint, field: judge.configuration()})).hexdigest()
    return fingerprint


def main():
    parser = argparse.ArgumentParser(description='#29 불변 baseline / frozen·live / 품질 비교')
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('capture')
    capture.add_argument('run', type=Path)
    capture.add_argument('--output', type=Path, required=True)
    capture.add_argument('--instrumentation-only', action='store_true')
    capture.add_argument('--note')
    search = sub.add_parser('export-search')
    search.add_argument('run', type=Path)
    search.add_argument('--output', type=Path, required=True)
    execute = sub.add_parser('run')
    execute.add_argument('--entrypoint', required=True, help='module:function; must use existing RunRecorder integration')
    execute.add_argument('--kwargs', type=Path, required=True, help='explicit JSON function arguments')
    execute.add_argument('--role', choices=['baseline', 'refactored'], required=True)
    execute.add_argument('--data-mode', choices=['frozen', 'live'], required=True)
    execute.add_argument('--search-snapshot', type=Path)
    execute.add_argument('--baseline-receipt', type=Path)
    evaluate = sub.add_parser('evaluate-pair')
    evaluate.add_argument('baseline_run', type=Path)
    evaluate.add_argument('after_run', type=Path)
    evaluate.add_argument('--baseline-package', type=Path, required=True)
    evaluate.add_argument('--after-package', type=Path, required=True)
    evaluate.add_argument('--dataset', type=Path, required=True)
    evaluate.add_argument('--baseline-receipt', type=Path, required=True)
    evaluate.add_argument('--model', default='gpt-4.1-mini')
    evaluate.add_argument('--max-retries', type=int, default=1)
    evaluate.add_argument('--output', type=Path, required=True)
    evaluate.add_argument('--resume', type=Path)
    evaluate.add_argument('--reason')
    report = sub.add_parser('compare')
    report.add_argument('baseline_evaluation', type=Path)
    report.add_argument('after_evaluation', type=Path)
    report.add_argument('--data-mode', choices=['frozen', 'live'], required=True)
    report.add_argument('--baseline-receipt', type=Path, required=True)
    report.add_argument('--output', type=Path, required=True)
    campaign = sub.add_parser('campaign')
    campaign.add_argument('manifest', type=Path, help='JSON list including every attempted generation pair')
    campaign.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            save_new(args.output, preserve(args.run, instrumentation_only=args.instrumentation_only, note=args.note))
        elif args.command == 'export-search':
            save_new(args.output, export_search(args.run))
        elif args.command == 'run':
            result = run_experiment(args.entrypoint, read(args.kwargs), role=args.role, data_mode=args.data_mode,
                replay_path=args.search_snapshot, receipt=read(args.baseline_receipt) if args.baseline_receipt else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'evaluate-pair':
            from .ragas_adapter import RagasAdapter
            from .factual import LangChainFactualJudge
            from .grounding import LangChainSupportJudge
            from .coverage import LangChainCoverageJudge
            if args.max_retries < 0:
                parser.error('--max-retries must be non-negative')
            asyncio.run(evaluate_pair([args.baseline_run, args.after_run], [args.baseline_package, args.after_package],
                args.dataset, RagasAdapter.openai(args.model, max_retries=args.max_retries), output=args.output,
                receipt=read(args.baseline_receipt), resume=args.resume, reason=args.reason,
                factual_judge=LangChainFactualJudge.openai(args.model),
                support_judge=LangChainSupportJudge.openai(args.model), coverage_judge=LangChainCoverageJudge.openai(args.model)))
        elif args.command == 'campaign':
            save_new(args.output, campaign_report(read(args.manifest)))
        else:
            write_report(paired_report(args.baseline_evaluation, args.after_evaluation, data_mode=args.data_mode,
                baseline_receipt=read(args.baseline_receipt)), args.output)
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
