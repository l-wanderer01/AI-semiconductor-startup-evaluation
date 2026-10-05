"""동일한 새 evaluator로 baseline/after를 재평가한다. 생성 run은 보존한다."""
import argparse
import asyncio
import json
from pathlib import Path

from .ragas_adapter import RagasAdapter
from .reevaluation import comparison, reevaluate_pair


def main():
    parser = argparse.ArgumentParser(description='#27 baseline/after 평가기 버전 동기화')
    parser.add_argument('command', choices=['check', 'rerun'])
    parser.add_argument('baseline', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--model', default='gpt-4.1-mini')
    parser.add_argument('--max-retries', type=int, default=1)
    parser.add_argument('--reason')
    parser.add_argument('--reference-dataset', type=Path)
    parser.add_argument('--custom-factual', action='store_true')
    parser.add_argument('--custom-grounding', action='store_true')
    parser.add_argument('--required-information', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'check':
        result = comparison(args.baseline, args.after)
    else:
        if not args.reason:
            parser.error('rerun requires --reason')
        if args.max_retries < 0:
            parser.error('--max-retries must be non-negative')
        if args.custom_factual and not args.reference_dataset:
            parser.error('--custom-factual requires --reference-dataset')
        if args.required_information and not args.custom_factual:
            parser.error('--required-information requires --custom-factual')
        from .factual import LangChainFactualJudge
        from .grounding import LangChainSupportJudge
        from .coverage import LangChainCoverageJudge
        result = asyncio.run(reevaluate_pair(args.baseline, args.after,
            RagasAdapter.openai(args.model, max_retries=args.max_retries), reason=args.reason,
            reference_dataset_directory=args.reference_dataset,
            factual_judge=LangChainFactualJudge.openai(args.model) if args.custom_factual else None,
            support_judge=LangChainSupportJudge.openai(args.model) if args.custom_grounding else None,
            coverage_judge=LangChainCoverageJudge.openai(args.model) if args.required_information else None))
    if args.output:
        with args.output.open('x', encoding='utf-8') as file:
            json.dump(result, file, ensure_ascii=False, indent=2)
            file.write('\n')
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
