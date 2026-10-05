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
    evaluate.add_argument('--samples', type=Path, required=True, help='RagasSampleInput JSONL')
    evaluate.add_argument('--dataset-version', required=True)
    evaluate.add_argument('--reference-sha256', required=True)
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
    samples = [RagasSampleInput.model_validate_json(line)
               for line in args.samples.read_text().splitlines() if line.strip()]
    result = asyncio.run(evaluate_inputs(
        EvaluationStorage(directory.parent), directory.name, samples, RagasAdapter.openai(args.model),
        dataset_version=args.dataset_version, reference_sha256=args.reference_sha256,
        input_sha256=manifest.input_snapshot.sha256, evidence_sha256=manifest.evidence_snapshot.sha256,
        previous_evaluation_id=args.previous_evaluation_id, resume_reason=args.resume_reason,
    ))
    print(result)


if __name__ == '__main__':
    main()
