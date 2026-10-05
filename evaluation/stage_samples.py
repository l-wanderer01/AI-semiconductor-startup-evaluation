"""동결된 run → custom claims 및 Agent/기업/영역/회차별 sample. 후처리 전용."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .claim_extraction import (EXTRACTOR_VERSION, PROMPT, TRANSFORMATION_VERSION, candidate_location,
                               checked_text, json_character_offsets, json_units, markdown_units, normalize_statement, stable_id, validate_candidate)
from .claim_models import AtomAudit, CoverageEntry, ExtractionAudit, ExtractedAtoms, IsolatedContract, StageSample
from .dataset import canonical_bytes, load_dataset, reference_payload, sha256
from .models import ClaimRecord, RagasSampleInput, RunManifest, SnapshotRef
from .recording import json_value, now
from .validation import validate_run

ROLE_MAPPING_VERSION = 'baseline-role-map-v1'
ROLES = {
    'analyze_market': ('market_research', 'market'), 'market_research': ('market_research', 'market'),
    '_search_market': ('market_research', 'market'), '_search_company': ('company_research', 'research'),
    'collect_company_contexts': ('company_research', 'research'), 'company_research': ('company_research', 'research'),
    'technical_eval': ('technology', 'technology'), 'evaluate_technology': ('technology', 'technology'),
    'market_eval': ('market_traction', 'market'), 'evaluate_market': ('market', 'market'),
    'evaluate_business': ('business', 'business'), 'team_eval': ('team', 'team'), 'evaluate_team': ('team', 'team'),
    'competition_eval': ('competition', 'competition'), 'evaluate_competition': ('competition', 'competition'),
    'risk_eval': ('risk', 'risk'), 'evaluate_risk': ('risk', 'risk'),
    'decision': ('decision_rationale', 'ranking'), 'rank_companies': ('decision_rationale', 'ranking'),
    'top_report': ('report_writer', 'report'), 'hold_report': ('report_writer', 'report'),
    'generate_investment_report': ('report_writer', 'report'), 'generate_hold_report': ('report_writer', 'report'),
    'polish_report_to_korean': ('language_polish', 'report'),
    'technical_additional_research': ('additional_research', 'technology'), 'market_additional_research': ('additional_research', 'market'),
}
NON_FACT_ROLES = {'investment_supervisor', 'company_route', 'branch_selector', 'route_after_policy',
                  'ranking', 'apply_investment_policy', 'save_outputs', 'export_markdown_to_pdf', 'list_candidates', 'extract_companies'}


def logical_role(invocation, by_id):
    current, seen = invocation, set()
    while current and current['invocation_id'] not in seen:
        seen.add(current['invocation_id'])
        name = current['operation_id'].split('.')[0]
        if name in NON_FACT_ROLES:
            return None, 'decision'
        if name in ROLES:
            return ROLES[name]
        current = by_id.get(current.get('parent_invocation_id'))
    return None, 'unknown'


def read_rows(directory, name):
    path = directory / name
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []


def prepare_stage_package(run_directory, extractor, *, dataset_directory=None, mode='end_to_end',
                          isolated_contract=None, allow_synthetic=False, case_bindings=None):
    """LLM 실패/잘못된 분해를 error로 기록한다. 오류 출력이나 비사실만 있는 출력을 0점으로 바꾸지 않는다.

    case_bindings는 invocation/company/section → case_id의 명시적 검토된 연결이다.
    전체 단계 출력에 좁은 #21 reference를 자동 적용하지 않는다.
    """
    run_directory = Path(run_directory).resolve()
    errors = validate_run(run_directory)
    if errors or not (run_directory / '.frozen').is_file():
        raise ValueError('\n'.join(errors) or 'generation run must be frozen')
    manifest = RunManifest.model_validate_json((run_directory / 'run.json').read_text())
    dataset = load_dataset(Path(dataset_directory), require_ready=True, allow_synthetic=allow_synthetic) if dataset_directory else None
    invocations = read_rows(run_directory, 'invocations.jsonl')
    by_id = {r['invocation_id']: r for r in invocations}
    contexts = read_rows(run_directory, 'delivered_contexts.jsonl')
    evidence = json.loads((run_directory / 'evidence.json').read_text())
    retrievals = read_rows(run_directory, 'retrievals.jsonl')
    extraction_id = stable_id('extraction', manifest.run_id, now().isoformat())
    if mode not in {'isolated', 'end_to_end'}:
        raise ValueError('invalid evaluation mode')
    if mode == 'isolated':
        if isolated_contract is None or dataset is None:
            raise ValueError('isolated mode requires fixed input/reference/evaluator contract')
        isolated_contract = IsolatedContract.model_validate(isolated_contract)
        settings = json.loads((run_directory / 'settings_snapshot.json').read_text())['data']
        if settings.get('evaluation_mode') != 'isolated':
            raise ValueError('cannot relabel an end-to-end run as isolated')
        if (isolated_contract.input_sha256, isolated_contract.evidence_sha256, isolated_contract.reference_sha256,
            isolated_contract.dataset_version, isolated_contract.role_mapping_version) != (
            manifest.input_snapshot.sha256, manifest.evidence_snapshot.sha256, dataset.manifest.sha256,
            dataset.manifest.version, ROLE_MAPPING_VERSION):
            raise ValueError('isolated contract fixed input/reference/version mismatch')
        targets = [by_id.get(i) for i in isolated_contract.target_invocation_ids]
        if any(r is None for r in targets):
            raise ValueError('isolated target invocation missing')
        target_roles = {logical_role(r, by_id)[0] for r in targets}
        if None in target_roles or len(target_roles) != 1:
            raise ValueError('isolated targets must have one supported role')
        if any(logical_role(r, by_id)[0] not in {None, *target_roles} for r in invocations):
            raise ValueError('isolated run contains other semantic agent roles')
        if settings.get('evaluator_configuration_sha256') != isolated_contract.evaluator_configuration_sha256:
            raise ValueError('isolated evaluator configuration mismatch')
    elif isolated_contract is not None:
        raise ValueError('isolated contract not allowed for end_to_end')

    samples, claims, atom_audits, audits, coverage = [], [], [], [], []
    atom_by_claim = {}
    bindings = case_bindings or {}
    used_bindings = set()

    def process(record, ref, role, section, stage='intermediate'):
        invocation_id = record['invocation_id']
        unit_errors = []
        if ref is None or record['status'] != 'succeeded':
            status = 'error' if record['status'] == 'failed' else 'missing'
            sid = stable_id('sample', manifest.run_id, invocation_id, record.get('company_id'), section, record['attempt'], stage)
            samples.append(StageSample(sample_id=sid, run_id=manifest.run_id, evaluation_id=extraction_id, case_id='unbound',
                company_id=record.get('company_id'), section_id=section, agent_id=record.get('agent_id') or role,
                node_id=record.get('node_id'), operation_id=record['operation_id'], invocation_id=invocation_id,
                attempt=record['attempt'], stage=stage, mode=mode, as_of_date=manifest.as_of_date,
                input_snapshot=record.get('input_snapshot'), logical_role=role, role_mapping_version=ROLE_MAPPING_VERSION,
                transformation_version=TRANSFORMATION_VERSION, evaluation_status=status,
                reason='실패 호출 또는 출력 스냅샷 누락', reference_reason='출력 없어 사실 평가 불가'))
            return
        ref = SnapshotRef.model_validate(ref)
        raw = checked_text(ref, run_directory)
        delivered = [c for c in contexts if c['invocation_id'] == invocation_id]
        input_ref = record.get('input_snapshot')
        request = checked_text(SnapshotRef.model_validate(input_ref), run_directory) if input_ref else None
        # JSON 문자열 속 Markdown도 원본 토큰과 디코딩된 내용 양쪽을 보존한다.
        try:
            parsed = json.loads(raw)
            if parsed is None or (isinstance(parsed, dict) and parsed.get('data', 'present') in (None, '')):
                return process(record, None, role, section, stage)
            if isinstance(parsed, dict) and isinstance(parsed.get('data'), str) and section == 'report':
                value = parsed['data']
                token = next(u for u in json_units(raw, ref, section=section, company=record.get('company_id'), companies=manifest.candidate_company_ids) if u.json_pointer == '/data')
                units = list(markdown_units(value, ref, manifest.candidate_company_ids))
                fragment = token.location.quote[1:-1]
                positions = json_character_offsets(fragment)
                for unit in units:
                    start, end = positions[unit.location.start_offset], positions[unit.location.end_offset]
                    from .models import SourceLocation
                    unit.location = SourceLocation(snapshot=ref, start_offset=token.location.start_offset + 1 + start,
                        end_offset=token.location.start_offset + 1 + end, quote=fragment[start:end])
                    unit.format = 'json_string'
                    unit.json_fragment = True
                    unit.json_pointer = '/data'
            else:
                units = list(json_units(raw, ref, section=section, company=record.get('company_id'), companies=manifest.candidate_company_ids))
        except json.JSONDecodeError:
            units = list(markdown_units(raw, ref, manifest.candidate_company_ids, section=section, company=record.get('company_id')))
        groups = {}
        citation_urls = dict(re.findall(r'\[(\d+)\]:\s*(https?://\S+)', raw))
        for unit in units:
            audit = ExtractionAudit(unit=unit, extractor_version=extractor.version,
                                    prompt_sha256=sha256(extractor.prompt.encode()), status='not_applicable', excluded_reason=unit.exclusion_reason)
            audits.append(audit)
            if unit.exclusion_reason:
                continue
            key = (unit.company_id, unit.section_id)
            group = groups.setdefault(key, {'units': [], 'atoms': [], 'errors': []})
            group['units'].append(unit)
            try:
                result, raw_extraction = extractor.extract(unit, manifest.candidate_company_ids)
                result = ExtractedAtoms.model_validate(result)
                audit.raw_output = json_value(raw_extraction)
                # 잘못된 분해 하나라도 있으면 단위 전체를 error로 기록한다.
                for atom in result.atoms:
                    validate_candidate(unit, atom, manifest.candidate_company_ids)
                audit.status, audit.excluded_reason = 'completed', result.no_claim_reason
                for atom in result.atoms:
                    target = groups.setdefault((atom.company_id, atom.section_id), {'units': [], 'atoms': [], 'errors': []})
                    if unit not in target['units']:
                        target['units'].append(unit)
                    target['atoms'].append((unit, atom))
            except Exception as exc:
                audit.status, audit.error = 'error', str(exc)
                audit.raw_output = json_value(getattr(exc, 'raw_output', audit.raw_output))
                group['errors'].append(str(exc))
                unit_errors.append(str(exc))
        if not groups:
            groups[(record.get('company_id'), section)] = {'units': [], 'atoms': [], 'errors': []}
        # 다른 영역으로 분리되어 사실이 없는 그룹도 N/A로 기록한다.
        for (company, area), group in groups.items():
            sid = stable_id('sample', manifest.run_id, invocation_id, company, area, record['attempt'], stage)
            selected_claims, by_statement, excluded = [], {}, 0
            for unit, atom in group['atoms']:
                location = candidate_location(unit, atom)
                markers = list(dict.fromkeys(atom.citation_markers + re.findall(r'\[\d+\]|\[SOURCE:[^\]]+\]|https?://[^\s)]+', unit.text)))
                resolved_markers = {m: citation_urls.get(m.strip('[]'), m) for m in markers}
                citation_sources = [e['source_id'] for e in evidence if any(m in {e.get('url'), e['source_id'], '[SOURCE:' + e['source_id'] + ']'} for m in resolved_markers.values())]
                unresolved = [m for m in markers if not any(resolved_markers[m] in {e.get('url'), e['source_id'], '[SOURCE:' + e['source_id'] + ']'} for e in evidence)]
                atom_audit = AtomAudit(unit_id=unit.unit_id, sample_id=sid, candidate=atom, location=location,
                                      citation_source_ids=citation_sources, unresolved_citations=unresolved)
                atom_audits.append(atom_audit)
                if atom.kind != 'fact':
                    excluded += 1
                    continue
                normalized = normalize_statement(atom.statement)
                if normalized in by_statement:
                    claim = by_statement[normalized]
                    if location not in claim.occurrences:
                        claim.occurrences.append(location)
                    claim.critical = claim.critical or atom.critical
                else:
                    cid = stable_id('claim', sid, normalized, extractor.version)
                    claim = ClaimRecord(claim_id=cid, run_id=manifest.run_id, evaluation_id=extraction_id, sample_id=sid,
                        invocation_id=invocation_id, company_id=company, section_id=area, atomic_statement=atom.statement,
                        claim_kind='fact', critical=atom.critical, occurrences=[location], evidence_ids=record.get('evidence_ids', []),
                        extractor_version=extractor.version)
                    by_statement[normalized] = claim
                    selected_claims.append(claim)
                    claims.append(claim)
                    atom_by_claim[cid] = atom
                atom_audit.claim_id = claim.claim_id
            response = '\n'.join(c.atomic_statement for c in selected_claims)
            binding_key = '|'.join([invocation_id, company or '', area])
            case_id = bindings.get(binding_key)
            reference, reference_ids, bound_case, reference_reason = None, [], None, '명시적 case 범위 검토/연결이 없음'
            if case_id:
                used_bindings.add(binding_key)
                if dataset is None:
                    raise ValueError('case binding requires reviewed dataset')
                bound_case = next((c for c in dataset.manifest.cases if c.case_id == case_id), None)
                if bound_case is None or (bound_case.company_id, bound_case.section_id, bound_case.as_of_date) != (company, area, manifest.as_of_date):
                    raise ValueError('reference binding company/section/as_of scope mismatch')
                reference = reference_payload(dataset, case_id)['text']
                reference_ids, reference_reason = bound_case.reference_ids, None
                for claim in selected_claims:
                    claim.reference_ids = reference_ids
            metrics, unsupported = [], {}
            for metric in ['factual_precision', 'factual_recall', 'faithfulness', 'context_recall', 'context_precision']:
                if metric.startswith('factual_') and reference is None:
                    unsupported[metric] = reference_reason
                elif metric == 'faithfulness' and not delivered and not (request and record['invocation_type'] in {'node','function'}):
                    unsupported[metric] = '직접 전달 문맥 없음; 원문 evidence로 대체하지 않음'
                elif metric.startswith('context_') and not any(c['retrieval_invocation_ids'] for c in delivered):
                    unsupported[metric] = '연결된 실제 검색 없음'
                elif metric.startswith('context_') and reference is None:
                    unsupported[metric] = reference_reason
                else:
                    metrics.append(metric)
            error = bool(unit_errors)
            status = 'error' if error else ('pending' if selected_claims else 'not_applicable')
            reason = '추출 오류: ' + '; '.join(unit_errors) if error else (None if selected_claims else '사실 주장 없음; 의견·점수·결정은 rubric/규칙 검사 대상')
            locations = list({(u.location.snapshot.path, u.location.start_offset, u.location.end_offset): u.location for u in group['units']}.values())
            samples.append(StageSample(sample_id=sid, run_id=manifest.run_id, evaluation_id=extraction_id, case_id=case_id or 'unbound',
                company_id=company, section_id=area, agent_id=record.get('agent_id') or role, node_id=record.get('node_id'),
                operation_id=record['operation_id'], invocation_id=invocation_id, attempt=record['attempt'], stage=stage, mode=mode,
                as_of_date=manifest.as_of_date, user_input=request, response=None if error else response, reference=reference,
                input_snapshot=input_ref, input_sha256=SnapshotRef.model_validate(input_ref).sha256 if input_ref else None,
                response_sha256=sha256(response.encode()) if not error else None, source_sha256=ref.sha256,
                reference_sha256=dataset.manifest.sha256 if bound_case else None, reference_ids=reference_ids,
                evidence_ids=record.get('evidence_ids', []), delivered_context_ids=[c['context_id'] for c in delivered],
                retrieval_invocation_ids=list(dict.fromkeys(i for c in delivered for i in c['retrieval_invocation_ids'])),
                supported_metrics=metrics if status == 'pending' else [], unsupported_metric_reasons=unsupported,
                evaluation_status=status, reason=reason, logical_role=role, role_mapping_version=ROLE_MAPPING_VERSION,
                transformation_version=TRANSFORMATION_VERSION, source_locations=locations,
                raw_response='\n'.join(u.text for u in group['units']), claim_ids=[c.claim_id for c in selected_claims],
                excluded_atom_count=excluded, rubric_targets=['opinion_rubric', 'score_policy', 'recommendation_policy'] if excluded else []))

    for record in sorted(invocations, key=lambda r: r['started_at']):
        role, section = logical_role(record, by_id)
        if role is None or record['invocation_type'] == 'retrieval':
            coverage.append(CoverageEntry(logical_role=record['operation_id'], company_id=record.get('company_id'),
                invocation_ids=[record['invocation_id']], sample_ids=[], status='not_applicable',
                reason='routing/계산/저장 또는 지원되지 않는 역할: workflow/규칙 검사 대상'))
            continue
        if mode == 'isolated' and record['invocation_id'] not in isolated_contract.target_invocation_ids:
            continue
        process(record, record.get('output_snapshot'), role, section)
    # 최종 납품 Markdown만 final로 지정한다. 보강 전 원본/보강 출력은 위에서 intermediate로 보존된다.
    artifact = next((a for a in manifest.artifacts if a.format == 'md' and a.status == 'saved' and Path(a.path).name == 'report_original.md'), None)
    report_calls = [r for r in invocations if logical_role(r, by_id)[0] in {'language_polish', 'report_writer'} and r['status'] == 'succeeded']
    if mode == 'end_to_end':
        if artifact and report_calls:
            body = checked_text(SnapshotRef(path=artifact.path, sha256=artifact.sha256), run_directory)
            matching_calls = []
            for call in report_calls:
                if not call.get('output_snapshot'):
                    continue
                output = json.loads(checked_text(SnapshotRef.model_validate(call['output_snapshot']), run_directory))
                value = output.get('data') if isinstance(output, dict) else output
                emitted = value if isinstance(value, str) else next((value[k] for k in ['final_report', 'report_markdown', 'content'] if isinstance(value, dict) and isinstance(value.get(k), str)), None)
                if emitted == body:
                    matching_calls.append(call)
            if matching_calls:
                source_call = sorted(matching_calls, key=lambda r: r['ended_at'])[-1]
                process(source_call, {'path': artifact.path, 'sha256': artifact.sha256, 'media_type': 'text/markdown'}, 'delivered_report', 'report', 'final')
            else:
                coverage.append(CoverageEntry(logical_role='delivered_report', invocation_ids=[], sample_ids=[], status='error',
                    reason='납품 Markdown이 기록된 작성/언어 보강 출력과 일치하지 않음'))
        else:
            coverage.append(CoverageEntry(logical_role='delivered_report', invocation_ids=[], sample_ids=[], status='missing',
                reason='납품 Markdown 또는 연결 가능한 보고서 작성/언어 보강 호출 누락'))
    if used_bindings != set(bindings):
        raise ValueError('unused/unknown case binding keys')
    expected_roles = ['market_research', 'company_research', 'technology', 'market', 'business', 'team', 'competition', 'risk', 'decision_rationale', 'report_writer', 'language_polish', 'delivered_report', 'additional_research']
    for role in expected_roles:
        relevant = [s for s in samples if s.logical_role == role or (role == 'market' and s.logical_role == 'market_traction')]
        if relevant:
            for company in {s.company_id for s in relevant}:
                group = [s for s in relevant if s.company_id == company]
                coverage.append(CoverageEntry(logical_role=role, company_id=company, invocation_ids=list(dict.fromkeys(s.invocation_id for s in group)),
                    sample_ids=[s.sample_id for s in group], status='error' if any(s.evaluation_status == 'error' for s in group) else ('missing' if any(s.evaluation_status == 'missing' for s in group) else 'covered'),
                    reason='실제 호출별 원문 및 변환 결과 보존'))
        elif not any(c.logical_role == role for c in coverage):
            unsupported = (manifest.execution_path == 'investment_pipeline' and role == 'business') or (manifest.execution_path == 'agents' and role in {'additional_research', 'language_polish'}) or (mode == 'isolated')
            coverage.append(CoverageEntry(logical_role=role, invocation_ids=[], sample_ids=[], status='unsupported' if unsupported else 'missing',
                reason='현재 경로/단독 실행에서 지원하지 않는 단계' if unsupported else '해당 단계 출력이 기록되지 않음'))
        if mode == 'end_to_end' and role in {'company_research', 'technology', 'market', 'business', 'team', 'competition', 'risk', 'decision_rationale'}:
            for company in manifest.candidate_company_ids:
                if not any(c.logical_role == role and c.company_id == company for c in coverage):
                    unsupported = manifest.execution_path == 'investment_pipeline' and role == 'business'
                    coverage.append(CoverageEntry(logical_role=role, company_id=company, invocation_ids=[], sample_ids=[],
                        status='unsupported' if unsupported else 'missing', reason='현재 경로의 미지원 영역' if unsupported else '기업별 필수 단계 출력 누락'))
    from .claim_lineage import connect_claims
    score_values = {}
    def collect_scores(value, path=''):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'score', 'score_raw', 'rating', 'final_score'} and isinstance(item, (int, float)) and not isinstance(item, bool):
                    yield path + '/' + key, item
                else:
                    yield from collect_scores(item, path + '/' + key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from collect_scores(item, path + '/' + str(index))
    for invocation in invocations:
        if invocation.get('output_snapshot'):
            try:
                value = json.loads(checked_text(SnapshotRef.model_validate(invocation['output_snapshot']), run_directory))
                score_values[invocation['invocation_id']] = dict(collect_scores(value))
            except json.JSONDecodeError:
                pass
    links, research = connect_claims(samples, claims, atom_by_claim, invocations, contexts,
                                    json.loads((run_directory / 'additional_research.json').read_text()), score_values)
    return {'schema_version': '0.3.0', 'extraction_id': extraction_id, 'run_id': manifest.run_id, 'mode': mode,
            'input_sha256': manifest.input_snapshot.sha256, 'evidence_sha256': manifest.evidence_snapshot.sha256,
            'dataset': dataset.model_dump(mode='json') if dataset else None,
            'isolated_contract': isolated_contract.model_dump(mode='json') if isolated_contract else None,
            'case_bindings': bindings, 'extractor': {'version': extractor.version, 'prompt': extractor.prompt, 'prompt_sha256': sha256(extractor.prompt.encode()),
                'schema': ExtractedAtoms.model_json_schema(), 'model_settings': extractor.model_settings},
            'transformation_version': TRANSFORMATION_VERSION, 'role_mapping_version': ROLE_MAPPING_VERSION,
            'samples': [s.model_dump(mode='json') for s in samples], 'claims': [c.model_dump(mode='json') for c in claims],
            'atoms': [a.model_dump(mode='json') for a in atom_audits], 'extractions': [a.model_dump(mode='json') for a in audits],
            'coverage': [c.model_dump(mode='json') for c in coverage], 'claim_links': [c.model_dump(mode='json') for c in links],
            'research_transitions': research,
            'delivered_contexts': contexts, 'retrievals': retrievals}


def ragas_inputs(package):
    """사실용 response와 전체 원문을 구분하며 실제 전달/검색 문맥만 사용한다."""
    contexts = {c['context_id']: c for c in package['delivered_contexts']}
    retrievals = {r['invocation_id']: r for r in package['retrievals']}
    result = []
    for value in package['samples']:
        sample = StageSample.model_validate(value)
        if sample.evaluation_status != 'pending':
            continue
        for metric in sample.supported_metrics:
            base = dict(sample_id=sample.sample_id, evaluation_id=sample.evaluation_id, metric_name=metric,
                        user_input=sample.user_input, response=sample.response, reference=sample.reference,
                        reference_ids=sample.reference_ids, evidence_ids=sample.evidence_ids,
                        origin_invocation_id=sample.invocation_id, context_kind='none')
            if metric == 'faithfulness':
                base.update(reference=None, reference_ids=[])
                if sample.delivered_context_ids:
                    base.update(context_kind='delivered', context_ids=sample.delivered_context_ids,
                                retrieved_contexts=[contexts[c]['text'] for c in sample.delivered_context_ids])
                else:
                    base.update(context_kind='received_input', context_ids=['received_' + sample.invocation_id], retrieved_contexts=[sample.user_input])
            elif metric.value.startswith('context_'):
                for retrieval_id in sample.retrieval_invocation_ids:
                    retrieval = retrievals[retrieval_id]
                    candidates = retrieval['candidates']
                    result.append(RagasSampleInput(**{**base, 'sample_id': stable_id('retrieval_sample', sample.sample_id, retrieval_id),
                        'context_kind': 'retrieved', 'origin_invocation_id': retrieval_id, 'user_input': retrieval['query'],
                        'context_ids': [c['context_id'] for c in candidates], 'retrieved_contexts': [c['text'] for c in candidates],
                        'evidence_ids': [c['evidence_id'] for c in candidates]}))
                continue
            result.append(RagasSampleInput(**base))
    return result


def write_stage_package(package, output):
    """새 디렉토리에만 저장한다. manifest 해시로 파일과 변환 규칙을 고정한다."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    files = {}
    for name in ['samples', 'claims', 'atoms', 'extractions', 'coverage', 'claim_links']:
        content = b''.join(canonical_bytes(row) + b'\n' for row in package[name])
        filename = name + '.jsonl'
        (output / filename).write_bytes(content)
        files[filename] = sha256(content)
    content = canonical_bytes(package) + b'\n'
    (output / 'package.json').write_bytes(content)
    files['package.json'] = sha256(content)
    (output / 'manifest.json').write_bytes(canonical_bytes({'schema_version': '0.3.0', 'files': files}) + b'\n')
    return output


def load_stage_package(directory, run_directory):
    """보관 파일과 변환 내용을 원문에 재대조한다. 원본이 바뀌거나 response가 편집되면 거부한다."""
    directory, run_directory = Path(directory), Path(run_directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text())
    from .dataset import safe_path
    for name, digest in manifest['files'].items():
        if sha256(safe_path(directory, name).read_bytes()) != digest:
            raise ValueError('stage package file hash mismatch: ' + name)
    package = json.loads((directory / 'package.json').read_text())
    if package['schema_version'] != '0.3.0' or package['transformation_version'] != TRANSFORMATION_VERSION or package['role_mapping_version'] != ROLE_MAPPING_VERSION:
        raise ValueError('unsupported stage transformation/role version')
    run = RunManifest.model_validate_json((run_directory / 'run.json').read_text())
    if (package['run_id'], package['input_sha256'], package['evidence_sha256']) != (run.run_id, run.input_snapshot.sha256, run.evidence_snapshot.sha256):
        raise ValueError('stage package generation scope mismatch')
    problems = validate_run(run_directory)
    if problems or not (run_directory / '.frozen').exists():
        raise ValueError('\n'.join(problems) or 'run not frozen')
    for name in ['samples', 'claims', 'atoms', 'extractions', 'coverage', 'claim_links']:
        if read_rows(directory, name + '.jsonl') != package[name]:
            raise ValueError('stage archive rows differ from package: ' + name)
    claims = {c['claim_id']: ClaimRecord.model_validate(c) for c in package['claims']}
    if len(claims) != len(package['claims']):
        raise ValueError('duplicate custom claim ID')
    units = {a['unit']['unit_id']: a['unit'] for a in package['extractions']}
    dataset = None
    if package['dataset'] is not None:
        from .dataset_models import FixedDataset
        from .dataset import dataset_hash
        dataset = FixedDataset.model_validate(package['dataset'])
        if dataset.manifest.sha256 != dataset_hash(dataset) or dataset.status != 'released':
            raise ValueError('stage dataset hash/status mismatch')
        case_reference_ids={ref for case in dataset.manifest.cases for ref in case.reference_ids}
        selected_facts=[f for f in dataset.facts if f.reference_id in case_reference_ids]
        if len(selected_facts) != len(case_reference_ids) or any(f.review_status != 'verified' for f in selected_facts) or any(r.completeness != 'complete' for r in dataset.case_reviews):
            raise ValueError('stage dataset review incomplete')
        if dataset.purpose == 'baseline' and (any(f.review_kind != 'human' for f in selected_facts) or any(r.review_kind != 'human' for r in dataset.case_reviews)):
            raise ValueError('baseline stage reference requires human review')
    from .claim_models import TextUnit
    for atom_row in package['atoms']:
        audit = AtomAudit.model_validate(atom_row)
        unit = TextUnit.model_validate(units[audit.unit_id])
        expected_location = validate_candidate(unit, audit.candidate, run.candidate_company_ids)
        if expected_location != audit.location:
            raise ValueError('atom original position mismatch')
        source = checked_text(audit.location.snapshot, run_directory)
        location = audit.location
        if source[location.start_offset:location.end_offset] != location.quote:
            raise ValueError('claim original quote mismatch')
        if unit.format == 'json_string':
            decoded = json.loads('"' + unit.location.quote + '"') if unit.json_fragment else json.loads(unit.location.quote)
            if unit.text != decoded:
                raise ValueError('decoded unit differs from original JSON token')
        elif unit.text != unit.location.quote:
            raise ValueError('markdown unit differs from original text')
        if audit.claim_id is not None:
            claim = claims.get(audit.claim_id)
            if claim is None or audit.candidate.kind != 'fact' or audit.sample_id != claim.sample_id:
                raise ValueError('custom claim does not match fact atom')
            if (normalize_statement(audit.candidate.statement),audit.candidate.company_id,audit.candidate.section_id) != (normalize_statement(claim.atomic_statement),claim.company_id,claim.section_id):
                raise ValueError('custom claim statement/scope differs from extraction audit')
            if audit.location not in claim.occurrences:
                raise ValueError('custom claim occurrence differs from atom original position')
    for claim in claims.values():
        if claim.run_id != run.run_id or claim.evaluation_id != package['extraction_id']:
            raise ValueError('custom claim run/evaluation scope mismatch')
        locations = [AtomAudit.model_validate(a).location for a in package['atoms'] if a['claim_id']==claim.claim_id]
        if set(p.model_dump_json() for p in locations) != set(p.model_dump_json() for p in claim.occurrences):
            raise ValueError('custom claim occurrence set differs from audited atoms')
    by_id={i['invocation_id']:i for i in read_rows(run_directory,'invocations.jsonl')}
    for row in package['samples']:
        sample = StageSample.model_validate(row)
        if sample.run_id != run.run_id or sample.evaluation_id != package['extraction_id'] or sample.mode != package['mode']:
            raise ValueError('sample scope mismatch')
        invocation=by_id.get(sample.invocation_id)
        if invocation is None or sample.attempt != invocation['attempt']:
            raise ValueError('sample invocation/attempt mismatch')
        if sample.input_snapshot != (SnapshotRef.model_validate(invocation['input_snapshot']) if invocation.get('input_snapshot') else None):
            raise ValueError('sample actual input snapshot differs from invocation')
        if sample.user_input is not None and sample.user_input != checked_text(sample.input_snapshot,run_directory):
            raise ValueError('sample request differs from actual input')
        permitted=[invocation.get('output_snapshot')] if sample.stage=='intermediate' else [a.model_dump(mode='json') for a in run.artifacts if a.status=='saved' and a.format=='md' and Path(a.path).name=='report_original.md']
        for location in sample.source_locations:
            if not any(r and r['path']==location.snapshot.path and r['sha256']==location.snapshot.sha256 for r in permitted):
                raise ValueError('sample source is not invocation output or delivered artifact')
        owned = [claims[c] for c in sample.claim_ids]
        if any(c.sample_id != sample.sample_id or c.invocation_id != sample.invocation_id for c in owned):
            raise ValueError('claim/sample lineage mismatch')
        expected = '\n'.join(c.atomic_statement for c in owned)
        if sample.evaluation_status == 'pending' and (sample.response != expected or sample.response_sha256 != sha256(expected.encode())):
            raise ValueError('facts response differs from custom claims')
        if sample.reference is not None:
            if dataset is None:
                raise ValueError('reference without dataset')
            case = next((c for c in dataset.manifest.cases if c.case_id == sample.case_id), None)
            if case is None or (case.company_id, case.section_id, case.as_of_date) != (sample.company_id, sample.section_id, sample.as_of_date):
                raise ValueError('stage reference scope mismatch')
            if sample.reference != reference_payload(dataset, case.case_id)['text'] or sample.reference_ids != case.reference_ids or sample.reference_sha256 != dataset.manifest.sha256:
                raise ValueError('stage reference differs from reviewed facts')
        for location in sample.source_locations:
            source = checked_text(location.snapshot, run_directory)
            if source[location.start_offset:location.end_offset] != location.quote:
                raise ValueError('sample original position mismatch')
    return package
