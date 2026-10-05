"""baseline 저장의 원본 보존·실패·동시 실행을 검증한다. 실제 Agent/API는 호출하지 않는다."""

import hashlib
import json
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel

from evaluation.models import EvaluationManifest, RunManifest
from evaluation.storage import EvaluationStorage, serialize_payload


class Row(BaseModel):
    run_id: str
    index: int = 0
    text: str = "한국어\n두 번째 줄"
    details: dict = {}


START = datetime(2026, 10, 5, tzinfo=timezone.utc)
SHA = "a" * 64


def run_manifest(run_id="run_1", *, ended=False):
    return RunManifest(
        run_id=run_id, comparison_role="baseline", execution_path="agents",
        contract_version="0.1", schema_version="0.1", policy_version="v1", workflow_version="v1",
        commit_sha="abc123", dirty=False, as_of_date="2026-10-05", requested_formats=["md"],
        llm_enabled=False, live_research_enabled=False, started_at=START,
        ended_at=START + timedelta(seconds=1) if ended else None,
        generation_status="failed" if ended else "running",
    )


def evaluation_manifest(evaluation_id="eval_1", *, ended=False):
    return EvaluationManifest(
        evaluation_id=evaluation_id, run_id="run_1", mode="isolated", dataset_version="v1",
        reference_sha256=SHA, input_sha256=SHA, evidence_sha256=SHA,
        evaluator=dict(ragas_version="fixture", api_version="fixture", adapter_version="v1",
                       schema_version="v1", model=dict(provider="test", model="test"),
                       configuration_sha256=SHA,
                       metrics=[dict(metric_name="faithfulness", implementation="fixture",
                                     prompt=dict(prompt_id="p1", version="v1", sha256=SHA),
                                     trace_supported=False)]),
        started_at=START, ended_at=START + timedelta(seconds=1) if ended else None,
        quality_evaluation_status="failed" if ended else "running",
    )


def append_worker(root, worker):
    storage = EvaluationStorage(Path(root))
    for index in range(10):
        storage.append_jsonl(storage.root / "run_1", "events.jsonl", Row(run_id="run_1", index=worker * 10 + index))


class EvaluationStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = EvaluationStorage(Path(self.temporary.name) / "runs")
        self.run_dir = self.storage.create_run("run_1")

    def test_duplicate_run_and_evaluation_are_rejected(self):
        with self.assertRaises(FileExistsError):
            self.storage.create_run("run_1")
        directory = self.storage.create_evaluation("run_1", "eval_1")
        self.assertTrue(directory.is_dir())
        with self.assertRaises(FileExistsError):
            self.storage.create_evaluation("run_1", "eval_1")
        with self.assertRaises(FileNotFoundError):
            self.storage.create_evaluation("missing_run", "eval_1")
        self.assertFalse((self.storage.root / "missing_run").exists())

    def test_initial_json_is_never_overwritten(self):
        path = self.storage.write_json(self.run_dir, "evidence.json", Row(run_id="run_1"))
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.storage.write_json(self.run_dir, "evidence.json", Row(run_id="run_1", index=9))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(json.loads(original)["text"], "한국어\n두 번째 줄")

    def test_initial_publish_failure_leaves_no_partial_file(self):
        with patch("evaluation.storage.os.link", side_effect=OSError("injected publication failure")):
            with self.assertRaises(OSError):
                self.storage.write_json(self.run_dir, "evidence.json", Row(run_id="run_1"))
        self.assertFalse((self.run_dir / "evidence.json").exists())
        self.assertEqual(list(self.run_dir.glob(".write-*.tmp")), [])

    def test_jsonl_preserves_rows_and_line_boundaries(self):
        for index in range(3):
            self.storage.append_jsonl(self.run_dir, "events.jsonl", Row(run_id="run_1", index=index))
        lines = (self.run_dir / "events.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["index"] for line in lines], [0, 1, 2])

    def test_interrupted_and_invalid_tail_are_preserved(self):
        target = self.run_dir / "events.jsonl"
        for original in [b'{"index":', b'invalid\n', b'\n']:
            with self.subTest(original=original):
                target.write_bytes(original)
                with self.assertRaises(ValueError):
                    self.storage.append_jsonl(self.run_dir, "events.jsonl", Row(run_id="run_1"))
                self.assertEqual(target.read_bytes(), original)

    def test_large_jsonl_last_row(self):
        self.storage.append_jsonl(self.run_dir, "events.jsonl", Row(run_id="run_1", text="한" * 6000))
        self.storage.append_jsonl(self.run_dir, "events.jsonl", Row(run_id="run_1", index=1))
        self.assertEqual(len((self.run_dir / "events.jsonl").read_text().splitlines()), 2)

    def test_atomic_manifest_update_and_failure_preserve_original(self):
        target = self.storage.write_json(self.run_dir, "run.json", run_manifest())
        original = target.read_bytes()
        with patch("evaluation.storage.os.replace", side_effect=OSError("injected replacement failure")):
            with self.assertRaises(OSError):
                self.storage.update_manifest(self.run_dir, "run.json", run_manifest(ended=True))
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.run_dir.glob(".write-*.tmp")), [])
        self.storage.update_manifest(self.run_dir, "run.json", run_manifest(ended=True))
        self.assertEqual(RunManifest.model_validate_json(target.read_text()).generation_status, "failed")

    def test_wrong_ids_and_manifest_types_are_rejected(self):
        with self.assertRaises(ValueError):
            self.storage.write_json(self.run_dir, "run.json", run_manifest("run_2"))
        with self.assertRaises(ValueError):
            self.storage.write_json(self.run_dir, "run.json", Row(run_id="run_1"))
        with self.assertRaises(ValueError):
            self.storage.append_jsonl(self.run_dir, "events.jsonl", Row(run_id="run_2"))
        eval_dir = self.storage.create_evaluation("run_1", "eval_1")
        with self.assertRaises(ValueError):
            self.storage.write_json(eval_dir, "evaluation.json", evaluation_manifest("eval_2"))
        self.storage.write_json(eval_dir, "evaluation.json", evaluation_manifest())
        with self.assertRaises(ValueError):
            self.storage.update_manifest(eval_dir, "evaluation.json", evaluation_manifest("eval_2"))

    def test_frozen_run_rejects_writes_but_allows_new_evaluation(self):
        self.storage.write_json(self.run_dir, "run.json", run_manifest(ended=True))
        marker = self.storage.freeze(self.run_dir)
        self.assertTrue(marker.is_file())
        operations = [
            lambda: self.storage.write_json(self.run_dir, "evidence.json", Row(run_id="run_1")),
            lambda: self.storage.append_jsonl(self.run_dir, "events.jsonl", Row(run_id="run_1")),
            lambda: self.storage.update_manifest(self.run_dir, "run.json", run_manifest()),
            lambda: self.storage.write_text(self.run_dir, "report_original.md", "report"),
        ]
        for operation in operations:
            with self.assertRaises(RuntimeError):
                operation()
        eval_dir = self.storage.create_evaluation("run_1", "eval_1")
        self.storage.write_json(eval_dir, "evaluation.json", evaluation_manifest(ended=True))
        self.storage.freeze(eval_dir)
        with self.assertRaises(RuntimeError):
            self.storage.write_text(eval_dir, "notes.md", "new")
        self.assertTrue(self.storage.create_evaluation("run_1", "eval_2").is_dir())

    def test_running_or_missing_manifest_cannot_be_frozen(self):
        with self.assertRaises(FileNotFoundError):
            self.storage.freeze(self.run_dir)
        self.storage.write_json(self.run_dir, "run.json", run_manifest())
        with self.assertRaises(ValueError):
            self.storage.freeze(self.run_dir)
        self.assertFalse((self.run_dir / ".frozen").exists())

    def test_snapshots_and_report_hash_actual_stored_bytes(self):
        ref = self.storage.write_snapshot(self.run_dir, "input_snapshot.json", Row(run_id="run_1"))
        self.assertEqual(ref.sha256, hashlib.sha256(Path(ref.path).read_bytes()).hexdigest())
        report = "# 원본 보고서\n\n근거를 확인했습니다.\n"
        ref = self.storage.write_text(self.run_dir, "report_original.md", report)
        self.assertEqual(Path(ref.path).read_text(), report)
        self.assertEqual(ref.sha256, hashlib.sha256(report.encode()).hexdigest())

    def test_credentials_redacted_in_nested_fields_and_text(self):
        fake_secret = "test-private-value-1234"
        with patch.dict("os.environ", {"OPENAI_API_KEY": fake_secret}):
            row = Row(run_id="run_1", text=f"failed with {fake_secret}",
                      details={"nested": {"api_key": "hidden-value"}, "Authorization": "Bearer hidden-token"})
            text = serialize_payload(row)
            self.assertNotIn(fake_secret, text)
            self.assertNotIn("hidden-value", text)
            self.assertNotIn("hidden-token", text)
            ref = self.storage.write_text(self.run_dir, "error.txt", f"Bearer other-secret {fake_secret}")
            self.assertNotIn(fake_secret, Path(ref.path).read_text())

    def test_path_escape_and_symlinks_rejected(self):
        for name in ["../escape", "/escape", ".frozen"]:
            with self.assertRaises(ValueError):
                self.storage.write_json(self.run_dir, name, Row(run_id="run_1"))
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (self.run_dir / "evidence.json").symlink_to(outside / "evidence.json")
        with self.assertRaises(ValueError):
            self.storage.write_json(self.run_dir, "evidence.json", Row(run_id="run_1"))
        (self.run_dir / "evaluations").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.storage.create_evaluation("run_1", "eval_1")

    def test_multi_thread_writers_do_not_mix_rows(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(lambda worker: append_worker(str(self.storage.root), worker), range(3)))
        rows = [json.loads(line) for line in (self.run_dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(row["index"] for row in rows), list(range(30)))

    def test_multi_process_writers_do_not_mix_rows(self):
        context = multiprocessing.get_context("spawn")
        processes = [context.Process(target=append_worker, args=(str(self.storage.root), worker)) for worker in range(3)]
        for process in processes:
            process.start()
        try:
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join()
        rows = [json.loads(line) for line in (self.run_dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(row["index"] for row in rows), list(range(30)))


if __name__ == "__main__":
    unittest.main()
