"""#27 paired re-evaluation: 동일 adapter로 양쪽을 새 ID에 실행하고 기존 평가 보존."""
import json
from pathlib import Path

from .models import EvaluationManifest, RagasSampleInput
from .ragas_adapter import evaluate_inputs
from .review import rows
from .storage import EvaluationStorage


def comparison(baseline_directory, after_directory):
    directories = [Path(p).resolve() for p in (baseline_directory, after_directory)]
    manifests = [EvaluationManifest.model_validate_json((d / 'evaluation.json').read_text()) for d in directories]
    baseline, after = manifests
    if baseline.run_id == after.run_id:
        raise ValueError('paired comparison requires distinct generation runs')
    for field in ('mode', 'dataset_version', 'reference_sha256'):
        if getattr(baseline, field) != getattr(after, field):
            raise ValueError('paired comparison scope mismatch: ' + field)
    if baseline.evaluator.configuration_sha256 != after.evaluator.configuration_sha256:
        raise ValueError('evaluator settings changed; re-evaluate BOTH baseline and after')
    if any(not (d / '.frozen').is_file() for d in directories):
        raise ValueError('paired comparison requires frozen evaluations')
    changed = baseline.input_sha256 != after.input_sha256 or baseline.evidence_sha256 != after.evidence_sha256
    if changed and baseline.mode == 'isolated':
        from .experiment import evidence_content
        from .models import RunManifest
        from .validation import validate_run
        runs = [d.parent.parent for d in directories]
        for run, evaluation in zip(runs, manifests):
            source = RunManifest.model_validate_json((run / 'run.json').read_text())
            if validate_run(run) or (evaluation.input_sha256, evaluation.evidence_sha256) != (
                    source.input_snapshot.sha256, source.evidence_snapshot.sha256):
                raise ValueError('isolated generation source integrity failed')
        if baseline.input_sha256 != after.input_sha256 or evidence_content(runs[0]) != evidence_content(runs[1]):
            raise ValueError('isolated comparison requires identical input/evidence content')
    return {'schema_version': 'paired-evaluation-v1', 'baseline_run_id': baseline.run_id,
            'after_run_id': after.run_id, 'baseline_evaluation_id': baseline.evaluation_id,
            'after_evaluation_id': after.evaluation_id,
            'evaluator_configuration_sha256': baseline.evaluator.configuration_sha256,
            'reference_sha256': baseline.reference_sha256, 'mode': baseline.mode,
            'input_or_evidence_changed': changed,
            'baseline_status': baseline.quality_evaluation_status.value,
            'after_status': after.quality_evaluation_status.value,
            'complete': all(m.quality_evaluation_status == 'completed' for m in manifests)}


async def reevaluate_pair(baseline_directory, after_directory, adapter, *, reason,
                          reference_dataset_directory=None, factual_judge=None,
                          support_judge=None, coverage_judge=None, allow_synthetic=False):
    directories = [Path(p).resolve() for p in (baseline_directory, after_directory)]
    if not reason or not reason.strip():
        raise ValueError('re-evaluation reason is required')
    manifests = [EvaluationManifest.model_validate_json((d / 'evaluation.json').read_text()) for d in directories]
    if manifests[0].run_id == manifests[1].run_id:
        raise ValueError('paired re-evaluation requires distinct generation runs')
    for field in ('mode', 'dataset_version', 'reference_sha256'):
        if getattr(manifests[0], field) != getattr(manifests[1], field):
            raise ValueError('baseline/after reference scope differs: ' + field)
    packages = []
    for directory, manifest in zip(directories, manifests, strict=True):
        if not (directory / '.frozen').is_file():
            raise ValueError('previous evaluation must be frozen')
        for field, judge in (('custom_factual_configuration', factual_judge),
                             ('custom_grounding_configuration', support_judge),
                             ('custom_coverage_configuration', coverage_judge)):
            if getattr(manifest.evaluator, field) is not None and judge is None:
                raise ValueError('cannot silently drop previously enabled ' + field)
        package_path = directory / 'stage_package.json'
        package = json.loads(package_path.read_text())['data'] if package_path.exists() else None
        if package and package['mode'] == 'isolated':
            # Original isolated lock is checked by evaluate_inputs. Never re-pin it implicitly.
            raise ValueError('isolated re-evaluation requires newly prepared stage packages pinned to the new evaluator; use evaluation evaluate for both runs')
        if (factual_judge or support_judge) and package is None:
            raise ValueError('custom re-evaluation requires preserved stage packages')
        packages.append(package)
    outputs = []
    for directory, manifest, package in zip(directories, manifests, packages, strict=True):
        samples = [RagasSampleInput.model_validate(r) for r in rows(directory / 'ragas_samples.jsonl')]
        new = await evaluate_inputs(EvaluationStorage(directory.parent.parent.parent), manifest.run_id, samples, adapter,
            dataset_version=manifest.dataset_version, reference_sha256=manifest.reference_sha256,
            input_sha256=manifest.input_sha256, evidence_sha256=manifest.evidence_sha256,
            previous_evaluation_id=manifest.evaluation_id, resume_reason=reason, stage_package=package,
            reference_dataset_directory=reference_dataset_directory, factual_judge=factual_judge,
            support_judge=support_judge, coverage_judge=coverage_judge, allow_synthetic=allow_synthetic)
        outputs.append(new)
    result = comparison(*outputs)
    result.update(previous_baseline_evaluation_id=manifests[0].evaluation_id,
                  previous_after_evaluation_id=manifests[1].evaluation_id,
                  baseline_directory=str(outputs[0]), after_directory=str(outputs[1]))
    return result
