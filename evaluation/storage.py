"""평가 계약의 JSON/JSONL·원본·snapshot 저장 기반.

작성 기록(Codex): 모델 직렬화, 경로 검증, 동결 확인을 작성했다.
작성 기록(Codex): baseline 저장 구현을 위임받아 잠금·저장·갱신·동결을 완성했다.
Agent 호출이나 품질 평가는 수행하지 않는다. fcntl 잠금은 macOS/Linux용이다.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel

from .models import (
    EvaluationManifest,
    GenerationStatus,
    QualityEvaluationStatus,
    RunManifest,
    SnapshotRef,
)

Payload = BaseModel | list[BaseModel]


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in {
        "apikey", "openaiapi", "authorization", "password", "passwd", "secret",
        "clientsecret", "accesstoken", "refreshtoken", "credentials", "cookie",
        "setcookie", "privatekey",
    } or normalized.endswith(("apikey", "password", "clientsecret"))


def _redact_text(text: str) -> str:
    secrets = sorted(
        {value for key, value in os.environ.items() if _is_secret_key(key) and len(value) >= 8},
        key=len, reverse=True,
    )
    for value in secrets:
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", text)


def _redact_value(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _is_secret_key(key) else _redact_value(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def serialize_payload(payload: Payload, *, pretty: bool = True) -> str:
    """모델을 JSON으로 변환하고 인증 정보를 가린다. None은 null로 보존한다."""
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    elif isinstance(payload, list) and all(isinstance(item, BaseModel) for item in payload):
        data = [item.model_dump(mode="json") for item in payload]
    else:
        raise TypeError("저장 대상은 Pydantic 모델 또는 모델 목록이어야 합니다.")
    return json.dumps(_redact_value(data), ensure_ascii=False, allow_nan=False,
                      indent=2 if pretty else None,
                      separators=None if pretty else (",", ":"))


class EvaluationStorage:
    """생성 실행과 품질 평가의 저장·잠금·동결을 관리한다."""

    _locks_guard = threading.Lock()
    _thread_locks: dict[Path, threading.RLock] = {}

    def __init__(self, root: Path = Path("evaluation_runs")) -> None:
        self.root = root.resolve()

    @staticmethod
    def _validate_component(value: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValueError("ID와 파일명에는 영문·숫자·밑줄·점·하이픈만 사용할 수 있습니다.")

    def _target_path(self, directory: Path, filename: str) -> Path:
        self._validate_component(filename)
        directory = directory.absolute()
        if directory == self.root or not directory.is_relative_to(self.root):
            raise ValueError("저장 위치는 실행별 디렉터리 내부여야 합니다.")
        current = self.root
        for part in directory.relative_to(self.root).parts:
            current = current / part
            if part in {".", ".."} or current.is_symlink():
                raise ValueError("경로 이동과 심볼릭 링크는 허용하지 않습니다.")
        directory = directory.resolve()
        self._manifest_type(directory)
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        target = directory / filename
        if target.is_symlink():
            raise ValueError("심볼릭 링크를 저장 대상으로 사용할 수 없습니다.")
        return target

    @staticmethod
    def _assert_mutable(directory: Path) -> None:
        # 부모 run의 동결은 새 품질 평가 생성·기록을 금지하지 않는다.
        if (directory / ".frozen").exists():
            raise RuntimeError("종료된 실행·평가 디렉터리는 수정할 수 없습니다.")

    def _manifest_type(self, directory: Path):
        parts = directory.resolve().relative_to(self.root).parts
        if len(parts) == 1:
            return "run.json", RunManifest
        if len(parts) == 3 and parts[1] == "evaluations":
            return "evaluation.json", EvaluationManifest
        raise ValueError("저장 위치는 run 또는 evaluations/<evaluation_id>여야 합니다.")

    def _validate_manifest_identity(self, directory: Path, manifest: BaseModel) -> None:
        parts = directory.resolve().relative_to(self.root).parts
        _, model_type = self._manifest_type(directory)
        if not isinstance(manifest, model_type) or manifest.run_id != parts[0]:
            raise ValueError("manifest 유형 또는 run_id가 저장 위치와 일치하지 않습니다.")
        if isinstance(manifest, EvaluationManifest) and manifest.evaluation_id != parts[2]:
            raise ValueError("evaluation_id가 저장 위치와 일치하지 않습니다.")

    def _validate_record_identity(self, directory: Path, payload: Payload) -> None:
        parts = directory.relative_to(self.root).parts
        for record in payload if isinstance(payload, list) else [payload]:
            if not isinstance(record, BaseModel):
                raise TypeError("저장 대상은 Pydantic 모델이어야 합니다.")
            if hasattr(record, "run_id") and record.run_id != parts[0]:
                raise ValueError("레코드의 run_id가 저장 위치와 일치하지 않습니다.")
            expected_evaluation_id = parts[2] if len(parts) == 3 else None
            if hasattr(record, "evaluation_id") and record.evaluation_id != expected_evaluation_id:
                raise ValueError("레코드의 evaluation_id가 저장 위치와 일치하지 않습니다.")

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write(self, target: Path, data: bytes, *, replace: bool = False) -> None:
        """같은 디렉터리의 임시 파일을 fsync한 뒤 원자적으로 공개한다."""
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".write-",
                                             suffix=".tmp", delete=False) as file:
                temporary_path = Path(file.name)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            if replace:
                os.replace(temporary_path, target)
            else:
                # 기존 대상이 있으면 link가 실패하므로 최초 저장은 덮어쓰지 않는다.
                os.link(temporary_path, target)
            temporary_path.unlink(missing_ok=True)
            self._sync_directory(target.parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @contextmanager
    def _directory_lock(self, directory: Path) -> Iterator[None]:
        """스레드와 프로세스의 append·갱신·동결을 직렬화한다."""
        directory = self._target_path(directory, "lock_check").parent
        with self._locks_guard:
            thread_lock = self._thread_locks.setdefault(directory, threading.RLock())
        with thread_lock:
            descriptor = os.open(directory / ".writer.lock",
                                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            # 잠금 파일을 삭제하면 다른 inode를 잠그는 경쟁이 생길 수 있다.

    def create_run(self, run_id: str) -> Path:
        """새 run 디렉터리를 만든다. 기존 ID는 재사용하지 않는다."""
        self._validate_component(run_id)
        run_dir = self.root / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(exist_ok=False)
        self._sync_directory(self.root)
        return run_dir

    def create_evaluation(self, run_id: str, evaluation_id: str) -> Path:
        """기존 run에 새 평가를 만든다. 동결된 run에도 추가할 수 있다."""
        self._validate_component(run_id)
        self._validate_component(evaluation_id)
        run_dir = self.root / run_id
        evaluation_root = run_dir / "evaluations"
        evaluation_dir = evaluation_root / evaluation_id
        self._target_path(run_dir, "run.json")
        with self._directory_lock(run_dir):
            if evaluation_root.is_symlink():
                raise ValueError("평가 디렉터리는 심볼릭 링크일 수 없습니다.")
            evaluation_root.mkdir(exist_ok=True)
            evaluation_dir.mkdir(exist_ok=False)
            self._sync_directory(evaluation_root)
        return evaluation_dir

    def write_json(self, directory: Path, filename: str, payload: Payload) -> Path:
        """JSON 최초 저장. 같은 이름이 존재하면 덮어쓰지 않는다."""
        target = self._target_path(directory, filename)
        self._validate_record_identity(target.parent, payload)
        if filename in {"run.json", "evaluation.json"}:
            expected, model_type = self._manifest_type(target.parent)
            if filename != expected or not isinstance(payload, model_type):
                raise ValueError("manifest 파일명과 모델 유형이 일치하지 않습니다.")
            payload = model_type.model_validate(payload.model_dump(mode="json"))
            self._validate_manifest_identity(target.parent, payload)
        text = serialize_payload(payload)
        with self._directory_lock(target.parent):
            self._assert_mutable(target.parent)
            self._atomic_write(target, (text + "\n").encode("utf-8"))
        return target

    @staticmethod
    def _check_jsonl_tail(target: Path) -> None:
        """중단되거나 깨진 마지막 행을 덮거나 이어 붙이지 않는다."""
        if not target.exists() or target.stat().st_size == 0:
            return
        with target.open("rb") as file:
            file.seek(-1, os.SEEK_END)
            if file.read(1) != b"\n":
                raise ValueError("JSONL 마지막 행이 완료되지 않았습니다.")
            position = file.tell() - 1
            tail = b""
            while position > 0:
                count = min(position, 4096)
                position -= count
                file.seek(position)
                tail = file.read(count) + tail
                if b"\n" in tail:
                    tail = tail.rsplit(b"\n", 1)[1]
                    break
            json.loads(tail)

    def append_jsonl(self, directory: Path, filename: str, record: BaseModel) -> Path:
        """검증된 JSONL 끝에 레코드 한 행을 추가한다."""
        if not isinstance(record, BaseModel):
            raise TypeError("JSONL 레코드는 Pydantic 모델이어야 합니다.")
        if not filename.endswith(".jsonl"):
            raise ValueError("JSONL 파일명은 .jsonl로 끝나야 합니다.")
        target = self._target_path(directory, filename)
        self._validate_record_identity(target.parent, record)
        line = serialize_payload(record, pretty=False)
        with self._directory_lock(target.parent):
            self._assert_mutable(target.parent)
            self._check_jsonl_tail(target)
            with target.open("ab") as file:
                file.write((line + "\n").encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())
            self._sync_directory(target.parent)
        return target

    def update_manifest(self, directory: Path, filename: str, manifest: BaseModel) -> Path:
        """기존 manifest를 검증한 뒤 원자적으로 교체한다."""
        if filename not in {"run.json", "evaluation.json"}:
            raise ValueError("갱신 대상은 run.json 또는 evaluation.json이어야 합니다.")
        target = self._target_path(directory, filename)
        with self._directory_lock(target.parent):
            self._assert_mutable(target.parent)
            expected, model_type = self._manifest_type(target.parent)
            if filename != expected or not isinstance(manifest, model_type):
                raise ValueError("manifest 파일명과 모델 유형이 일치하지 않습니다.")
            manifest = model_type.model_validate(manifest.model_dump(mode="json"))
            previous = model_type.model_validate_json(target.read_text(encoding="utf-8"))
            self._validate_manifest_identity(target.parent, previous)
            self._validate_manifest_identity(target.parent, manifest)
            text = serialize_payload(manifest)
            self._atomic_write(target, (text + "\n").encode("utf-8"), replace=True)
        return target

    def freeze(self, directory: Path) -> Path:
        """종료 manifest를 확인하고 이후 저장을 막는 마커를 만든다."""
        directory = self._target_path(directory, "run.json").parent
        with self._directory_lock(directory):
            self._assert_mutable(directory)
            filename, model_type = self._manifest_type(directory)
            target = self._target_path(directory, filename)
            manifest = model_type.model_validate_json(target.read_text(encoding="utf-8"))
            self._validate_manifest_identity(directory, manifest)
            if isinstance(manifest, RunManifest):
                terminal = manifest.generation_status in {GenerationStatus.SUCCEEDED, GenerationStatus.FAILED}
            else:
                terminal = manifest.quality_evaluation_status in {
                    QualityEvaluationStatus.COMPLETED,
                    QualityEvaluationStatus.PARTIAL,
                    QualityEvaluationStatus.FAILED,
                }
            if not terminal:
                raise ValueError("진행 중인 실행·평가는 동결할 수 없습니다.")
            marker = directory / ".frozen"
            self._atomic_write(marker, (datetime.now(timezone.utc).isoformat() + "\n").encode("utf-8"))
        return marker

    def write_text(self, directory: Path, filename: str, text: str,
                   *, media_type: str = "text/markdown") -> SnapshotRef:
        """인증 정보를 가린 텍스트를 저장하고 실제 저장 바이트의 해시를 반환한다."""
        return self.write_bytes(directory, filename, _redact_text(text).encode("utf-8"),
                                media_type=media_type)

    def write_bytes(self, directory: Path, filename: str, data: bytes,
                    *, media_type: str) -> SnapshotRef:
        """바이너리를 최초 저장한다. 바이너리 내부의 인증 정보는 호출자가 제거한다."""
        if not isinstance(data, bytes):
            raise TypeError("바이너리 저장 대상은 bytes여야 합니다.")
        if filename in {"run.json", "evaluation.json"}:
            raise ValueError("manifest는 모델 검증을 거쳐 write_json으로 저장해야 합니다.")
        target = self._target_path(directory, filename)
        with self._directory_lock(target.parent):
            self._assert_mutable(target.parent)
            self._atomic_write(target, data)
        return SnapshotRef(path=str(target), sha256=hashlib.sha256(data).hexdigest(),
                           media_type=media_type)

    def write_snapshot(self, directory: Path, filename: str, payload: Payload) -> SnapshotRef:
        """JSON snapshot을 저장하고 저장된 바이트의 SHA-256을 반환한다."""
        target = self.write_json(directory, filename, payload)
        return SnapshotRef(path=str(target), sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                           media_type="application/json")


# 작성 기록(Codex): run/evaluation ID는 저장 시 검증한다. invocation·evidence·reference의
# 파일 간 참조 관계 검증은 실행 harness에서 수행한다.
# 인증 정보 가리기는 모든 비밀 형식을 탐지하지 않는다. 설정은 허용 목록으로 전달하고
# 환경 변수·인증 헤더 원본을 저장하지 않는다. 임의 바이너리에는 가리기를 적용하지 않는다.
