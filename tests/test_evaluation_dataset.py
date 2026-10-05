"""#21 검토 게이트·원문 무결성·범위·실제 run 계보 회귀 검증. 외부 API 없음."""
import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from evaluation.dataset import (canonical_bytes, dataset_hash, load_dataset, prepare_run_samples,
                                publish_dataset, reference_payload, reference_text, sha256, validate_dataset)
from evaluation.dataset_models import RunCaseBinding
from evaluation.models import SourceLocation
from evaluation.recording import RunRecorder, company_scope
from evaluation.seed_dataset import build_seed


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def publish(self, synthetic=True, parent=None):
        dataset, files = build_seed(synthetic=synthetic)
        return publish_dataset(dataset, files, parent or self.root)

    def mutate(self, directory, change, *, refresh_reference=False):
        dataset = load_dataset(directory)
        change(dataset)
        if refresh_reference:
            for case in dataset.manifest.cases:
                raw = canonical_bytes(reference_payload(dataset, case.case_id)) + b'\n'
                (directory / case.reference_snapshot.path).write_bytes(raw)
                case.reference_snapshot.sha256 = sha256(raw)
                dataset.inventory[case.reference_snapshot.path] = sha256(raw)
        dataset.manifest.sha256 = dataset_hash(dataset)
        (directory / 'dataset.json').write_text(dataset.model_dump_json(), encoding='utf-8')

    def test_checked_in_seeds_validate_and_real_data_is_not_ready(self):
        parent = Path(__file__).resolve().parents[1] / 'data/evaluation'
        for dataset_id, version in [('semiconductor-baseline', '0.1.0-draft'), ('semiconductor-fixtures', '1.0.0')]:
            directory = parent / dataset_id / version
            self.assertEqual(validate_dataset(directory), [])
        with self.assertRaisesRegex(ValueError, 'draft'):
            reference_text(parent / 'semiconductor-baseline/0.1.0-draft', 'groq_2024_round')

    def test_publish_hash_does_not_depend_on_checkout_path_and_no_overwrite(self):
        one = self.publish(parent=self.root / 'one')
        two = self.publish(parent=self.root / 'two')
        self.assertEqual(load_dataset(one).manifest.sha256, load_dataset(two).manifest.sha256)
        with self.assertRaises(FileExistsError):
            self.publish(parent=self.root / 'one')
        self.assertEqual(validate_dataset(one, require_ready=True, allow_synthetic=True), [])

    def test_synthetic_data_cannot_be_used_as_real_baseline(self):
        directory = self.publish()
        with self.assertRaisesRegex(ValueError, 'synthetic fixtures'):
            reference_text(directory, 'fixture_poc')
        self.assertIn('PoC', reference_text(directory, 'fixture_poc', allow_synthetic=True))

    def test_pending_facts_are_not_reference_text(self):
        directory = self.publish(synthetic=False)
        dataset = load_dataset(directory)
        payload = reference_payload(dataset, 'groq_2024_round')
        self.assertEqual(payload['text'], '')
        self.assertEqual(payload['facts'], [])
        self.assertEqual(len(payload['excluded_reference_ids']), 2)
        self.assertEqual(dataset.manifest.reviewers, [])

    def test_original_quote_must_match_even_if_all_hashes_are_recomputed(self):
        directory = self.publish()
        self.mutate(directory, lambda d: setattr(d.facts[0].source_locations[0], 'quote', '다른 원문'), refresh_reference=True)
        self.assertTrue(any('quote/offset mismatch' in e for e in validate_dataset(directory)))

    def test_source_tampering_and_metadata_tampering_are_detected(self):
        directory = self.publish()
        dataset = load_dataset(directory)
        path = directory / dataset.sources[0].snapshot.path
        path.write_text('changed', encoding='utf-8')
        self.assertTrue(any('hash mismatch' in e for e in validate_dataset(directory)))
        directory2 = self.publish(parent=self.root / 'other')
        value = json.loads((directory2 / 'dataset.json').read_text())
        value['case_reviews'][0]['scope_description'] = 'changed'
        (directory2 / 'dataset.json').write_text(json.dumps(value), encoding='utf-8')
        self.assertIn('dataset hash mismatch', validate_dataset(directory2))

    def test_cross_company_section_date_and_future_source_are_rejected(self):
        changes = [lambda d: setattr(d.facts[0], 'company_id', 'Other'),
                   lambda d: setattr(d.facts[0], 'section_id', 'risk'),
                   lambda d: setattr(d.facts[0], 'as_of_date', __import__('datetime').date(2024, 1, 1)),
                   lambda d: setattr(d.sources[0], 'published_date', __import__('datetime').date(2099, 1, 1))]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                directory = self.publish(parent=self.root / str(index))
                self.mutate(directory, change, refresh_reference=True)
                self.assertTrue(validate_dataset(directory))

    def test_reference_must_be_derived_from_verified_facts_not_a_report(self):
        directory = self.publish()
        dataset = load_dataset(directory)
        case = dataset.manifest.cases[0]
        path = directory / case.reference_snapshot.path
        value = json.loads(path.read_text())
        value['text'] = '이전 baseline 보고서에서 복사한 텍스트'
        raw = canonical_bytes(value)
        path.write_bytes(raw)
        case.reference_snapshot.sha256 = sha256(raw)
        dataset.inventory[case.reference_snapshot.path] = sha256(raw)
        dataset.manifest.sha256 = dataset_hash(dataset)
        (directory / 'dataset.json').write_text(dataset.model_dump_json())
        self.assertTrue(any('differs from verified facts' in e for e in validate_dataset(directory)))

    def test_baseline_cannot_claim_fixture_review_as_human_review(self):
        directory = self.publish(synthetic=False)
        def fake_review(dataset):
            dataset.status = 'released'
            for fact in dataset.facts:
                fact.review_status = 'verified'
                fact.reviewer = 'fixture-author'
                fact.reviewed_at = dataset.manifest.created_at
                fact.review_kind = 'synthetic_fixture'
            for review in dataset.case_reviews:
                review.completeness = 'complete'
                review.reviewer = 'fixture-author'
                review.reviewed_at = dataset.manifest.created_at
                review.review_kind = 'synthetic_fixture'
            dataset.manifest.reviewers = ['fixture-author']
        self.mutate(directory, fake_review, refresh_reference=True)
        self.assertTrue(any('human review' in e for e in validate_dataset(directory)))

    def test_incomplete_reference_scope_blocks_scoring(self):
        directory = self.publish()
        self.mutate(directory, lambda d: setattr(d.case_reviews[0], 'completeness', 'incomplete'))
        self.assertTrue(any('not approved' in e for e in validate_dataset(directory)))

    def test_dataset_snapshot_traversal_and_symlink_are_rejected(self):
        for index, kind in enumerate(['traversal', 'symlink']):
            directory = self.publish(parent=self.root / str(index))
            dataset = load_dataset(directory)
            path = directory / dataset.sources[0].snapshot.path
            if kind == 'symlink':
                other = self.root / 'outside.txt'
                other.write_bytes(path.read_bytes())
                path.unlink()
                path.symlink_to(other)
            else:
                self.mutate(directory, lambda d: setattr(d.sources[0].snapshot, 'path', '../outside.txt'))
            self.assertTrue(validate_dataset(directory))

    def frozen_run(self, directory, *, retrieval=True):
        dataset = load_dataset(directory, require_ready=True, allow_synthetic=True)
        case = dataset.manifest.cases[0]
        source = dataset.sources[0]
        text = (directory / source.snapshot.path).read_text()
        recorder = RunRecorder(root=self.root / 'runs', execution_path='agents', inputs={'question': case.user_input}, settings={},
                               requested_formats=['md'], repository=Path(__file__).resolve().parents[1])
        recorder.manifest.as_of_date = case.as_of_date
        with recorder.activate(), company_scope(case.company_id):
            recorder.add_evidence(text, source=source.source_id, company=case.company_id)
            retrieval_id = None
            if retrieval:
                from langchain_core.documents import Document
                with recorder.span('fixture_search', {'query': case.user_input}, kind='retrieval') as state:
                    recorder.retrieval(case.user_input, [Document(page_content=text, metadata={'source': source.source_id})], state['record'])
                    state['output'] = text
                    retrieval_id = state['record'].invocation_id
            with recorder.span('business', {'question': case.user_input}, kind='llm') as state:
                recorder.delivered('자료:\n' + text, state['record'])
                state['output'] = 'FixtureChip은 6억 4천만 미국 달러를 조달했다.'
                generation_id = state['record'].invocation_id
            output_ref = recorder.records[-1].output_snapshot
            raw = Path(output_ref.path).read_text()
            response = state['output']
            start = raw.index(response)
            response_location = SourceLocation(snapshot=output_ref, start_offset=start, end_offset=start + len(response), quote=response)
        recorder.finish(exc=ValueError('fixture: 보고서 산출물 없음'))
        binding = RunCaseBinding(case_id=case.case_id, company_id=case.company_id, section_id=case.section_id,
                                 as_of_date=case.as_of_date, generation_invocation_id=generation_id,
                                 response_location=response_location, retrieval_invocation_id=retrieval_id)
        return recorder.directory, binding

    def test_actual_run_samples_preserve_query_rank_source_order_and_response(self):
        directory = self.publish()
        run, binding = self.frozen_run(directory)
        before = {p: sha256(p.read_bytes()) for p in run.rglob('*') if p.is_file()}
        samples, metadata = prepare_run_samples(directory, run, [binding], allow_synthetic=True)
        self.assertEqual(len(samples), 5)
        self.assertEqual(samples[0].response, binding.response_location.quote)
        self.assertEqual(metadata['source_lineage'][0]['match_method'], 'exact_content_sha256')
        record = metadata['retrieval_records'][0]
        retrieval_sample = next(s for s in samples if s.metric_name == 'context_precision')
        self.assertEqual(retrieval_sample.retrieved_contexts, [c['text'] for c in record['candidates']])
        self.assertEqual(record['candidates'][0]['rank'], 1)
        self.assertEqual(record['query'], samples[0].user_input)
        self.assertEqual(before, {p: sha256(p.read_bytes()) for p in run.rglob('*') if p.is_file()})

    def test_absent_retrieval_is_na_and_not_invented_from_dataset_evidence(self):
        directory = self.publish()
        run, binding = self.frozen_run(directory, retrieval=False)
        samples, metadata = prepare_run_samples(directory, run, [binding], allow_synthetic=True)
        self.assertEqual(len(samples), 3)
        self.assertEqual(len(metadata['applicability']), 2)
        self.assertTrue(all(r['evaluation_status'] == 'not_applicable' for r in metadata['applicability']))
        self.assertEqual(metadata['retrieval_records'], [])

    def test_bindings_reject_different_scope_and_fabricated_response(self):
        directory = self.publish()
        run, binding = self.frozen_run(directory)
        for fields in [{'company_id': 'Other'}, {'section_id': 'risk'}, {'response_location': binding.response_location.model_copy(update={'quote': '가짜 응답'})}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                prepare_run_samples(directory, run, [binding.model_copy(update=fields)], allow_synthetic=True)

    def test_publish_cli_regenerates_reference_after_explicit_review_without_changing_original(self):
        directory = self.publish(synthetic=False)
        before = sha256((directory / 'dataset.json').read_bytes())
        import shutil
        working = self.root / 'review-work'
        shutil.copytree(directory, working)
        value = json.loads((working / 'dataset.json').read_text())
        # 사람 검토 완료 상태를 흉내내는 단위 테스트 데이터다. 체크인 원문에는 적용하지 않는다.
        for fact in value['facts']:
            fact.update(review_status='verified', review_kind='human', reviewer='test-only-reviewer', reviewed_at=value['manifest']['created_at'])
        for review in value['case_reviews']:
            review.update(completeness='complete', review_kind='human', reviewer='test-only-reviewer', reviewed_at=value['manifest']['created_at'])
        (working / 'dataset.json').write_text(json.dumps(value), encoding='utf-8')
        result = subprocess.run([sys.executable, '-B', '-m', 'evaluation.dataset_cli', 'publish', str(working),
                                 '--parent', str(self.root / 'published'), '--version', '1.0.0'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        released = self.root / 'published/semiconductor-baseline/1.0.0'
        self.assertIn('USD 640,000,000', reference_text(released, 'groq_2024_round'))
        self.assertEqual(load_dataset(released).manifest.reviewers, ['test-only-reviewer'])
        self.assertEqual(sha256((directory / 'dataset.json').read_bytes()), before)

    def test_unreviewed_cli_release_fails_without_leaving_a_version(self):
        directory = self.publish(synthetic=False)
        parent = self.root / 'published'
        result = subprocess.run([sys.executable, '-B', '-m', 'evaluation.dataset_cli', 'publish', str(directory),
                                 '--parent', str(parent), '--version', '1.0.0'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unverified', result.stderr)
        self.assertFalse((parent / 'semiconductor-baseline/1.0.0').exists())

    def test_critical_fact_and_na_reason_are_required(self):
        dataset, _ = build_seed(synthetic=True)
        from evaluation.dataset_models import DatasetFact, DatasetRequiredItem
        value = dataset.facts[0].model_dump(mode='json')
        value['critical'] = False
        with self.assertRaises(ValueError):
            DatasetFact.model_validate(value)
        value = dataset.required_items[-1].model_dump(mode='json')
        self.assertEqual(value['applicability'], 'not_applicable')
        value['not_applicable_reason'] = None
        with self.assertRaises(ValueError):
            DatasetRequiredItem.model_validate(value)


if __name__ == '__main__':
    unittest.main()
