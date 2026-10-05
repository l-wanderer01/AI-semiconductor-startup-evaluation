"""#20 저장 참조 검증과 명시적으로 준비된 RAGAS 입력의 후처리 평가 CLI."""
import argparse
import asyncio
import json
from pathlib import Path

from .models import RagasSampleInput, RunManifest
from .storage import EvaluationStorage
from .validation import validate_run


def main():
    parser = argparse.ArgumentParser(description='평가 기록 검증 / RAGAS 후처리')
    sub = parser.add_subparsers(dest='command', required=True)
    validate = sub.add_parser('validate')
    validate.add_argument('run_directory', type=Path)
    evaluate = sub.add_parser('evaluate')
    evaluate.add_argument('run_directory', type=Path)
    inputs = evaluate.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--samples', type=Path, help='#20 수동 RagasSampleInput JSONL')
    inputs.add_argument('--dataset', type=Path, help='#21 사람 검토/발행된 고정 데이터셋')
    evaluate.add_argument('--bindings', type=Path, help='--dataset 사용 시 실제 호출과 원문 응답 구간 매핑')
    evaluate.add_argument('--dataset-version')
    evaluate.add_argument('--reference-sha256')
    evaluate.add_argument('--model', default='gpt-4.1-mini')
    evaluate.add_argument('--previous-evaluation-id')
    evaluate.add_argument('--resume-reason')
    args = parser.parse_args()
    directory = args.run_directory.resolve()
    if args.command == 'validate':
        errors = validate_run(directory)
        print(json.dumps({'valid': not errors, 'errors': errors}, ensure_ascii=False, indent=2))
        raise SystemExit(1 if errors else 0)
    from .ragas_adapter import RagasAdapter, evaluate_inputs
    manifest = RunManifest.model_validate_json((directory / 'run.json').read_text())
    preparation = None
    if args.dataset:
        if not args.bindings or args.dataset_version or args.reference_sha256:
            parser.error('--dataset에는 --bindings만 지정합니다. 버전/해시는 검증된 데이터셋에서 읽습니다.')
        from .dataset import load_dataset, prepare_run_samples
        from .dataset_models import RunCaseBinding
        dataset = load_dataset(args.dataset, require_ready=True)
        bindings = [RunCaseBinding.model_validate(row) for row in json.loads(args.bindings.read_text(encoding='utf-8'))]
        samples, preparation = prepare_run_samples(args.dataset, directory, bindings)
        preparation['dataset'] = dataset.model_dump(mode='json')
        preparation['bindings'] = [b.model_dump(mode='json') for b in bindings]
        args.dataset_version = dataset.manifest.version
        args.reference_sha256 = dataset.manifest.sha256
    else:
        if args.bindings or not args.dataset_version or not args.reference_sha256:
            parser.error('--samples에는 --dataset-version과 --reference-sha256이 필요합니다.')
        samples = [RagasSampleInput.model_validate_json(line)
                   for line in args.samples.read_text().splitlines() if line.strip()]
    result = asyncio.run(evaluate_inputs(
        EvaluationStorage(directory.parent), directory.name, samples, RagasAdapter.openai(args.model),
        dataset_version=args.dataset_version, reference_sha256=args.reference_sha256,
        input_sha256=manifest.input_snapshot.sha256, evidence_sha256=manifest.evidence_snapshot.sha256,
        previous_evaluation_id=args.previous_evaluation_id, resume_reason=args.resume_reason,
        preparation_metadata=preparation,
    ))
    print(result)


if __name__ == '__main__':
    main()
