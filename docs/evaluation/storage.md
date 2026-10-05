# 평가 저장 기반

작성 기록: Codex가 baseline 평가 기반 구현을 위임받아 작성했다.
대상: `evaluation/storage.py`. 실제 Agent 로직과 평가 API 호출은 포함하지 않는다.

## 생성 실행 수명주기

1. 고유 run_id를 만들어 `create_run`으로 새 디렉터리를 생성한다.
2. `write_json`으로 최초 run.json을 저장한다.
3. `append_jsonl`로 이벤트·중간 결과를 추가한다.
4. `write_snapshot`으로 입력·근거·중간 출력 모델과 저장된 바이트의 SHA-256을 연결한다.
5. `write_text`로 원본 Markdown, `write_bytes`로 PDF 등 산출물을 보존한다.
6. 호출자가 실제 파일 형식·필수 단계·분기 조건을 검사하고 manifest에 결과를 반영한다.
7. `update_manifest`로 종료 상태를 저장한 뒤 `freeze`로 원본 실행을 동결한다.

존재하는 run_id와 파일은 재사용하거나 덮어쓰지 않는다. 최초 저장도 임시 파일을 완성한
뒤 공개하므로 쓰기 도중 실패한 JSON 파일을 정상 산출물로 남기지 않는다.
갱신은 run.json/evaluation.json에만 허용하며 실패 시 원본을 보존하고 오류를 호출자에게 전달한다.

## 후처리 평가 수명주기

1. 기존 run에 `create_evaluation`으로 고유 evaluation_id 디렉터리를 추가한다.
2. evaluation.json 및 sample·판정·지표를 해당 디렉터리에 기록한다.
3. 평가 종료 상태를 갱신한 후 그 평가 디렉터리를 동결한다.
4. 재개·재평가에는 새 evaluation_id를 부여하고 이전 평가와 연결한다.

동결된 생성 실행에도 새 평가 디렉터리는 추가할 수 있다. 원본 run.json의 상태는 소급 변경하지 않는다.

## 동시 실행과 중단

- 동일 디렉터리의 쓰기·갱신·동결은 스레드 잠금과 fcntl 프로세스 잠금으로 직렬화한다.
- 잠금 파일은 삭제하지 않는다. 이 구현은 macOS/Linux에서 사용한다.
- JSONL 마지막 행이 불완전하거나 유효한 JSON이 아니면 내용을 보존하고 추가 쓰기를 거부한다.
- 종료 이벤트·manifest가 없는 실행은 성공으로 해석하지 않는다.
- 강제 종료로 남은 임시 파일과 불완전 로그의 복구 판단은 수집 harness에서 다룬다.
- 동결은 이 writer를 통한 수정을 막는 규칙이며 운영체제의 읽기 전용 권한 설정은 아니다.

## 데이터와 인증정보

모델의 None은 null로 유지하고 한국어·UTC 시각·Enum·계산 필드를 JSON으로 저장한다.
writer는 직접 기록의 run_id/evaluation_id가 디렉터리와 일치하는지 검사한다.
여러 파일의 invocation/evidence/reference ID 관계는 후속 수집·평가 harness가 검사한다.

구조화 필드의 API 키·인증 헤더 이름, 환경에 설정된 알려진 비밀값, Bearer/OpenAI 토큰 형태는
JSON과 텍스트 저장 시 제거한다. 호출자는 설정 허용 목록을 사용하고 raw 환경·인증 헤더를 수집하지 않는다.
임의 비밀값이나 PDF 등 바이너리 내부까지 완전히 탐지하는 기능은 아니다.
SHA-256은 인증정보 제거 후 실제 저장한 바이트를 기준으로 한다.

## 검증

프로젝트 루트에서 실행한다. 평가 환경 준비 절차는 [이슈 #20 구현 기록](issue20.md)을 참고한다:

```sh
.venv-evaluation/bin/python -B -m unittest discover -s tests -p 'test_evaluation_*.py' -v
```

중복 ID·덮어쓰기·atomic 교체 실패·불완전 JSONL·동결·상호 ID·스레드/프로세스
동시 기록·원본 텍스트 및 해시·인증정보 제거를 검증한다. 실제 API를 호출하지 않는다.
