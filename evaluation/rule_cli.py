"""Read-only deterministic replay of a frozen generation run."""
import json
from pathlib import Path
from types import SimpleNamespace

from .models import InvocationRecord, RunManifest
from .rules import check_recorded_run
from .validation import validate_run


def replay(directory):
    directory = Path(directory).resolve()
    if not (directory / '.frozen').is_file():
        raise ValueError('Rule replay requires a frozen generation run')
    errors = validate_run(directory)
    if errors:
        raise ValueError('Generation source integrity failed: ' + '; '.join(errors))
    manifest = RunManifest.model_validate_json((directory / 'run.json').read_text())
    records = [InvocationRecord.model_validate_json(line)
               for line in (directory / 'invocations.jsonl').read_text().splitlines() if line.strip()]
    result = json.loads((directory / 'decisions.json').read_text())['data']
    recorder = SimpleNamespace(directory=directory, records=records,
                               path=manifest.execution_path, run_id=manifest.run_id)
    checks, summary = check_recorded_run(recorder, result)
    return {'checks': [c.model_dump(mode='json') for c in checks], 'rule_conformance': summary}
