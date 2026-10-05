"""python -m evaluation.dataset_cli: #21 데이터 검증/검토 후 발행/입력 준비."""
import argparse
import json
from pathlib import Path

from .dataset import canonical_bytes, load_dataset, prepare_run_samples, publish_dataset, reference_text, safe_path, validate_dataset
from .dataset_models import RunCaseBinding


def main():
    parser = argparse.ArgumentParser(description='고정 평가 데이터셋 관리 (외부 API 호출 없음)')
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('validate')
    validate.add_argument('directory', type=Path)
    validate.add_argument('--ready', action='store_true')
    validate.add_argument('--allow-synthetic', action='store_true')
    reference = commands.add_parser('reference')
    reference.add_argument('directory', type=Path)
    reference.add_argument('case_id')
    reference.add_argument('--allow-synthetic', action='store_true')
    publish = commands.add_parser('publish')
    publish.add_argument('directory', type=Path, help='내용과 스냅샷을 수정한 별도 검토 작업 디렉토리')
    publish.add_argument('--parent', type=Path, required=True)
    publish.add_argument('--version', required=True, help='기존 버전과 다른 새 버전')
    publish.add_argument('--status', choices=['draft', 'released'], default='released')
    prepare = commands.add_parser('prepare')
    prepare.add_argument('directory', type=Path)
    prepare.add_argument('run_directory', type=Path)
    prepare.add_argument('--bindings', type=Path, required=True, help='RunCaseBinding JSON 배열')
    prepare.add_argument('--output', type=Path, required=True, help='새 출력 디렉토리')
    prepare.add_argument('--allow-synthetic', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'validate':
            errors = validate_dataset(args.directory, require_ready=args.ready, allow_synthetic=args.allow_synthetic)
            print(json.dumps({'valid': not errors, 'errors': errors}, ensure_ascii=False, indent=2))
            raise SystemExit(1 if errors else 0)
        if args.command == 'reference':
            print(reference_text(args.directory, args.case_id, allow_synthetic=args.allow_synthetic))
        elif args.command == 'publish':
            # 사람이 수정한 작업본은 이전 해시가 유효하지 않다. 발행 단계에서 재계산/전체 검증한다.
            from .dataset_models import FixedDataset
            from .dataset import reference_payload, sha256
            from .models import SnapshotRef
            dataset = FixedDataset.model_validate_json((args.directory / 'dataset.json').read_text(encoding='utf-8'))
            from datetime import datetime, timezone
            if args.version == dataset.manifest.version:
                raise ValueError('발행에는 작업본과 다른 새 버전이 필요합니다.')
            dataset.manifest.version = args.version
            dataset.manifest.created_at = datetime.now(timezone.utc)
            dataset.status = args.status
            for item in dataset.required_items:
                item.version = args.version
            files = {name: safe_path(args.directory, name).read_bytes() for name in dataset.inventory}
            # reference는 verified facts만으로 다시 만든다. 임의 보고서 텍스트를 복사하지 않는다.
            for case in dataset.manifest.cases:
                content = canonical_bytes(reference_payload(dataset, case.case_id)) + b'\n'
                files[case.reference_snapshot.path] = content
                case.reference_snapshot = SnapshotRef(path=case.reference_snapshot.path, sha256=sha256(content))
            dataset.manifest.reviewers = sorted({f.reviewer for f in dataset.facts if f.review_status != 'pending'} |
                                                {r.reviewer for r in dataset.case_reviews if r.completeness != 'pending'})
            print(publish_dataset(dataset, files, args.parent))
        else:
            bindings = [RunCaseBinding.model_validate(row) for row in json.loads(args.bindings.read_text(encoding='utf-8'))]
            samples, metadata = prepare_run_samples(args.directory, args.run_directory, bindings, allow_synthetic=args.allow_synthetic)
            args.output.mkdir(parents=True, exist_ok=False)
            (args.output / 'samples.jsonl').write_text(''.join(s.model_dump_json() + '\n' for s in samples), encoding='utf-8')
            (args.output / 'preparation.json').write_bytes(canonical_bytes(metadata) + b'\n')
            (args.output / 'bindings.json').write_bytes(canonical_bytes([b.model_dump(mode='json') for b in bindings]) + b'\n')
            print(args.output)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
