"""python -m evaluation.claim_cli: 단계별 sample/원자 주장 추출 후처리."""
import argparse
import json
from pathlib import Path

from .claim_extraction import LangChainClaimExtractor
from .stage_samples import prepare_stage_package, write_stage_package


def main():
    parser = argparse.ArgumentParser(description='#22 custom claim 추출 및 단계 sample 준비')
    parser.add_argument('run_directory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--case-bindings', type=Path, help='invocation_id|company_id|section_id → case_id JSON 객체')
    parser.add_argument('--model', default='gpt-4.1-mini')
    parser.add_argument('--mode', choices=['isolated', 'end_to_end'], default='end_to_end')
    parser.add_argument('--isolated-contract', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('--output은 존재하지 않는 새 디렉토리여야 합니다.')
    try:
        package = prepare_stage_package(args.run_directory, LangChainClaimExtractor.openai(args.model),
            dataset_directory=args.dataset, mode=args.mode,
            isolated_contract=json.loads(args.isolated_contract.read_text()) if args.isolated_contract else None,
            case_bindings=json.loads(args.case_bindings.read_text()) if args.case_bindings else None)
        write_stage_package(package, args.output)
        print(json.dumps({'output': str(args.output), 'sample_count': len(package['samples']), 'unique_claim_count': len(package['claims']),
                          'occurrence_count': sum(len(c['occurrences']) for c in package['claims']),
                          'extraction_error_count': sum(a['status'] == 'error' for a in package['extractions'])}, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
