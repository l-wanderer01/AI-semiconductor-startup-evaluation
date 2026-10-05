"""Experiment scopes and strict search replay. Never fall through to the network."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path

from .storage import _redact_value

EXPERIMENT = ContextVar('paired_experiment', default=None)
SEARCH = ContextVar('paired_search_replay', default=None)
EXPERIMENT_RUNS = ContextVar('paired_experiment_runs', default=None)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode()


def evidence_content(directory):
    """Compare actual source snapshots without run-specific paths/collection times."""
    evidence = json.loads((Path(directory) / 'evidence.json').read_text())
    return sorted([canonical({k: row.get(k) for k in
        ('source_id', 'company_ids', 'title', 'url', 'published_date', 'as_of_date')} |
        {'content_sha256': row['snapshot']['sha256']}).decode() for row in evidence])


class SearchReplay:
    """Inject complete typed search responses, including empty results, by exact query/options."""
    def __init__(self, snapshot):
        self.snapshot = snapshot
        if snapshot.get('schema_version') != 'search-replay-v1':
            raise ValueError('unsupported search replay schema')
        self.entries = {}
        for row in snapshot['entries']:
            key = self.key(row['operation'], row['query'], row['args'], row['options'])
            if key in self.entries:
                raise ValueError('duplicate replay search key')
            self.entries[key] = row['results']
        self.used = set()

    @staticmethod
    def key(operation, query, args, options):
        return hashlib.sha256(canonical([operation, query, list(args), options])).hexdigest()

    def lookup(self, operation, query, args, options):
        key = self.key(operation, query, args, options)
        if key not in self.entries:
            raise ValueError('frozen search missing exact query/options: ' + query)
        self.used.add(key)
        return self.entries[key]

    @contextmanager
    def activate(self):
        token = SEARCH.set(self)
        try:
            yield self
        finally:
            SEARCH.reset(token)

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))


def export_search(directory):
    """Export the actual web search input and serialized output from a frozen run."""
    from .paired import read, rows
    directory = Path(directory)
    if not (directory / '.frozen').is_file():
        raise ValueError('search export requires frozen generation')
    from .validation import validate_run
    if validate_run(directory):
        raise ValueError('search export requires intact generation snapshots')
    entries = []
    for row in rows(directory / 'invocations.jsonl'):
        if row['operation_id'] != 'TavilySearchClient_search':
            continue
        if row['status'] != 'succeeded':
            raise ValueError('failed search cannot be converted to a successful replay')
        request = read(Path(row['input_snapshot']['path']))['data']
        response = read(Path(row['output_snapshot']['path']))['data']
        entries.append(dict(operation=row['operation_id'], query=request['query'],
                            args=request.get('args', []), options=request['options'], results=response))
    # Repeated queries must return identical results, otherwise replay needs a new fixture.
    unique = {}
    for row in entries:
        key = SearchReplay.key(row['operation'], row['query'], row['args'], row['options'])
        if key in unique and unique[key]['results'] != row['results']:
            raise ValueError('search changed within run; provide an explicit fixed fixture')
        unique[key] = row
    return _redact_value({'schema_version': 'search-replay-v1', 'entries': list(unique.values())})
