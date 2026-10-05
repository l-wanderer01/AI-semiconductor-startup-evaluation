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
    rules = sub.add_parser('rules', help='#26 결정론적 규칙 재검사 (원본 읽기 전용)')
    rules.add_argument('run_directory', type=Path)
    evaluate = sub.add_parser('evaluate')
    evaluate.add_argument('run_directory', type=Path)
    inputs = evaluate.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--samples', type=Path, help='#20 수동 RagasSampleInput JSONL')
    inputs.add_argument('--dataset', type=Path, help='#21 사람 검토/발행된 고정 데이터셋')
    inputs.add_argument('--stage-package', type=Path, help='#22 원자 주장/단계 sample 보관 디렉토리')
    evaluate.add_argument('--bindings', type=Path, help='--dataset 사용 시 실제 호출과 원문 응답 구간 매핑')
    evaluate.add_argument('--dataset-version')
    evaluate.add_argument('--reference-sha256')
    evaluate.add_argument('--model', default='gpt-4.1-mini')
    evaluate.add_argument('--max-retries', type=int, default=0, help='metric별 adapter 재시도 횟수')
    evaluate.add_argument('--custom-factual', action='store_true', help='#23 독립 기준 3상태 판정 및 핵심 오류 검사')
    evaluate.add_argument('--reference-dataset', type=Path, help='--custom-factual에 사용할 #22와 동일한 검토 완료 데이터셋')
    evaluate.add_argument('--judge-model', default='gpt-4.1-mini')
    evaluate.add_argument('--custom-grounding', action='store_true', help='#24 실제 문맥·원문 근거 지지 및 인용 검증')
    evaluate.add_argument('--grounding-model', default='gpt-4.1-mini')
    evaluate.add_argument('--required-information', action='store_true', help='#25 고정 필수 항목 충족률과 RAGAS recall 병행 평가')
    evaluate.add_argument('--coverage-model', default='gpt-4.1-mini')
    evaluate.add_argument('--previous-evaluation-id')
    evaluate.add_argument('--resume-reason')
    args = parser.parse_args()
    directory = args.run_directory.resolve()
    if args.command == 'rules':
        from .rule_cli import replay
        try:
            result = replay(directory)
        except (ValueError, OSError, KeyError) as exc:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
            raise SystemExit(2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        statuses = [c['status'] for c in result['checks']]
        raise SystemExit(2 if 'error' in statuses else 1 if 'fail' in statuses else 0)
    if args.command == 'validate':
        errors = validate_run(directory)
        print(json.dumps({'valid': not errors, 'errors': errors}, ensure_ascii=False, indent=2))
        raise SystemExit(1 if errors else 0)
    from .ragas_adapter import RagasAdapter, evaluate_inputs
    if args.max_retries < 0:
        parser.error('--max-retries는 0 이상이어야 합니다.')
    if args.required_information and not args.custom_factual:
        parser.error('--required-information에는 --custom-factual이 필요합니다.')
    if args.custom_factual and (not args.stage_package or not args.reference_dataset):
        parser.error('--custom-factual에는 --stage-package와 --reference-dataset이 필요합니다.')
    if args.reference_dataset and not args.custom_factual:
        parser.error('--reference-dataset은 --custom-factual과 함께 사용합니다.')
    if args.custom_grounding and not args.stage_package:
        parser.error('--custom-grounding에는 --stage-package가 필요합니다.')
    manifest = RunManifest.model_validate_json((directory / 'run.json').read_text())
    preparation = None
    stage_package = None
    if args.stage_package:
        if args.bindings or args.dataset_version or args.reference_sha256:
            parser.error('--stage-package는 패키지에 저장된 reference 버전/해시를 사용합니다.')
        from .stage_samples import load_stage_package, ragas_inputs
        stage_package = load_stage_package(args.stage_package, directory)
        if stage_package['dataset'] is None and args.custom_grounding and not args.custom_factual:
            from .dataset import canonical_bytes,sha256
            args.dataset_version='no-reference-context-only-v1'
            args.reference_sha256=sha256(canonical_bytes({'reference':None,'purpose':'generation_context_only'}))
        elif stage_package['dataset'] is None or stage_package['dataset']['purpose'] != 'baseline':
            parser.error('--stage-package 평가에는 사람 검토된 baseline 데이터셋이 필요합니다.')
        else:
            dataset_manifest = stage_package['dataset']['manifest']
            args.dataset_version, args.reference_sha256 = dataset_manifest['version'], dataset_manifest['sha256']
        samples = ragas_inputs(stage_package)
    elif args.dataset:
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
    factual_judge = None
    if args.custom_factual:
        from .factual import LangChainFactualJudge
        factual_judge = LangChainFactualJudge.openai(args.judge_model)
    support_judge = None
    if args.custom_grounding:
        from .grounding import LangChainSupportJudge
        support_judge = LangChainSupportJudge.openai(args.grounding_model)
    coverage_judge = None
    if args.required_information:
        from .coverage import LangChainCoverageJudge
        coverage_judge = LangChainCoverageJudge.openai(args.coverage_model)
    result = asyncio.run(evaluate_inputs(
        EvaluationStorage(directory.parent), directory.name, samples, RagasAdapter.openai(args.model, max_retries=args.max_retries),
        dataset_version=args.dataset_version, reference_sha256=args.reference_sha256,
        input_sha256=manifest.input_snapshot.sha256, evidence_sha256=manifest.evidence_snapshot.sha256,
        previous_evaluation_id=args.previous_evaluation_id, resume_reason=args.resume_reason,
        preparation_metadata=preparation,
        stage_package=stage_package,
        factual_judge=factual_judge, reference_dataset_directory=args.reference_dataset,
        support_judge=support_judge,
        coverage_judge=coverage_judge,
    ))
    print(result)


if __name__ == '__main__':
    main()
