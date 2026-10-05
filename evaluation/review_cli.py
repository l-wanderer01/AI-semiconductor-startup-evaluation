"""사람 검토 계획·이력·신뢰도 보고서 CLI. LLM 호출 없음."""
import argparse
import json
from pathlib import Path

from .review import ReviewStore, inventory, reliability_report, review_plan


def main():
    parser = argparse.ArgumentParser(description='#27 한국어 평가기 사람 검토')
    parser.add_argument('command', choices=['plan', 'record', 'report'])
    parser.add_argument('evaluation_directory', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--decision', type=Path, help='ReviewDecision JSON; 실제 사람만 human으로 기록')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--seed', default='ko-review-v1')
    parser.add_argument('--per-stratum', type=int, default=2)
    args = parser.parse_args()
    store = ReviewStore(args.evaluation_directory.resolve())
    if args.command == 'record':
        if not args.decision:
            parser.error('record requires --decision')
        print(store.append(json.loads(args.decision.read_text())))
        return
    targets = inventory(store.directory)
    if args.command == 'plan':
        result = review_plan(targets, seed=args.seed, per_stratum=args.per_stratum)
    else:
        plan = json.loads(args.plan.read_text()) if args.plan else None
        result = reliability_report(targets, store.read(), plan=plan)
    if args.output:
        with args.output.open('x', encoding='utf-8') as file:
            json.dump(result, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write('\n')
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
