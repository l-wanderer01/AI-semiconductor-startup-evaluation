"""현재 두 경로의 실행 조건. 점수 공식의 독립 재계산은 #26에서 구현한다."""
from .models import RuleCheckResult, WorkflowManifest, WorkflowStep

AGENTS = ['discover_sources', 'analyze_market', 'extract_companies', 'collect_company_contexts',
          'investment_supervisor', 'rank_companies', 'apply_investment_policy']
DIMENSIONS = ['technology', 'market', 'business', 'team', 'risk', 'competition']
PIPELINE = ['list_candidates', 'market_research', 'analyze_companies', 'ranking']
COMPANY_STEPS = ['company_research', 'technical_eval', 'market_eval', 'team_eval', 'risk_eval',
                 'competition_eval', 'decision']
COMPANY_ROUTES = COMPANY_STEPS + ['technical_additional_research', 'market_additional_research']


def build_workflow(recorder):
    path = recorder.path
    policy = recorder.snapshot('policy_snapshot.json', {
        'version': recorder.manifest.policy_version, 'basis': 'baseline_behavior',
        'settings': recorder.settings,
        'score_scale': '6-30' if path == 'agents' else '0-100',
        'additional_research': 'unsupported' if path == 'agents' else 'notes_only_no_new_evidence',
        'no_new_evidence': 'record_failure_without_changing_baseline_route',
    })
    steps = []
    for name in AGENTS if path == 'agents' else PIPELINE:
        steps.append(WorkflowStep(step_id=name, agent_id=name, node_ids=[name], function_names=[name],
                                  per_company=False, required=True, enabled=True, supported=True))
    for name in ([f'evaluate_{d}' for d in DIMENSIONS] if path == 'agents' else COMPANY_STEPS):
        steps.append(WorkflowStep(step_id=name, agent_id=name, node_ids=[] if path == 'agents' else [name],
                                  function_names=[name], section_ids=[name], per_company=True,
                                  required=True, enabled=True, supported=True))
    reports = ['generate_investment_report', 'generate_hold_report'] if path == 'agents' else ['top_report', 'hold_report']
    for name in reports + ['polish_report_to_korean', 'export_markdown_to_pdf', 'save_outputs']:
        enabled = name in reports or name == 'save_outputs' or (path == 'investment_pipeline' and (
            name == 'export_markdown_to_pdf' or recorder.manifest.llm_enabled))
        steps.append(WorkflowStep(step_id=name, agent_id=name, function_names=[name], per_company=False,
                                  required=name == 'save_outputs', enabled=enabled, supported=enabled,
                                  unsupported_reason=None if enabled else 'not_in_this_execution_path'))
    for name in ['technical_additional_research', 'market_additional_research']:
        steps.append(WorkflowStep(step_id=name, agent_id=name, node_ids=[name] if path == 'investment_pipeline' else [],
                                  per_company=True, required=False, enabled=path == 'investment_pipeline',
                                  supported=False, unsupported_reason='baseline_has_no_new_search_or_evidence'))
    allowed = {'route_after_policy': ['top3', 'hold']} if path == 'agents' else {
        'investment_supervisor': COMPANY_ROUTES, 'company_route': COMPANY_ROUTES,
        'ranking': ['top3', 'hold'], 'branch_selector': ['top3', 'hold'],
    }
    return WorkflowManifest(version=recorder.manifest.workflow_version, execution_path=path,
                            policy_version=recorder.manifest.policy_version, check_basis='baseline_behavior',
                            candidate_company_ids=recorder.candidates, steps=steps, allowed_routes=allowed,
                            requested_formats=recorder.manifest.requested_formats, max_iterations=25,
                            termination_conditions=['required_coverage', 'allowed_route', 'graph_terminated',
                                                    'requested_artifacts_saved_and_parseable'],
                            no_new_evidence_policy='record_failure_without_changing_baseline_route',
                            policy_snapshot=policy)


def check_workflow(recorder, workflow, result):
    checks = []
    def add(name, expected, actual, *, company=None, invocation=None):
        checks.append(RuleCheckResult(run_id=recorder.run_id, check_id=name,
                                     execution_path=recorder.path, policy_version=workflow.policy_version,
                                     evaluation_status='completed', verdict='pass' if expected == actual else 'fail',
                                     expected=expected, actual=actual, company_id=company, invocation_id=invocation,
                                     reason=None if expected == actual else 'workflow expectation not met'))
    add('candidate_pool_nonempty', True, bool(recorder.candidates))
    add('candidate_ids_unique', len(recorder.candidates), len(set(recorder.candidates)))
    successful = [r for r in recorder.records if r.status == 'succeeded']
    for step in workflow.steps:
        if not step.required:
            continue
        if step.per_company:
            for company in recorder.candidates:
                if recorder.path == 'agents':
                    dimension = step.step_id.removeprefix('evaluate_') + '_evaluations'
                    data = result.model_dump() if hasattr(result, 'model_dump') else (result or {})
                    found = company in data.get(dimension, {})
                    add(f'coverage_{step.step_id}_{company}', True, found, company=company)
                    if found:
                        add(f'company_context_{step.step_id}_{company}', company,
                            data[dimension][company].get('company_name'), company=company)
                else:
                    records = [r for r in successful if r.operation_id == step.step_id and r.company_id == company]
                    add(f'coverage_{step.step_id}_{company}', True, bool(records), company=company)
        else:
            add('coverage_' + step.step_id, True, any(r.operation_id == step.step_id for r in successful))
    def company_names(value):
        if isinstance(value, dict):
            own = [value['company_name']] if 'company_name' in value else []
            return own + [name for child in value.values() for name in company_names(child)]
        if isinstance(value, list):
            return [name for child in value for name in company_names(child)]
        return []
    for output in recorder.outputs:
        if output.company_id:
            for index, name in enumerate(company_names(output.response)):
                add(f'output_company_{output.invocation_id}_{index}', output.company_id, name,
                    company=output.company_id, invocation=output.invocation_id)
    for record in recorder.records:
        if record.route is not None:
            add('route_' + record.invocation_id, True,
                record.route in workflow.allowed_routes.get(record.operation_id, []),
                company=record.company_id, invocation=record.invocation_id)
        if record.status == 'failed':
            add('call_' + record.invocation_id, 'succeeded', record.status,
                company=record.company_id, invocation=record.invocation_id)
        if record.company_id and record.company_id not in recorder.candidates:
            add('unknown_company_' + record.invocation_id, True, False, invocation=record.invocation_id)
    for company in recorder.candidates:
        count = sum(r.operation_id == 'investment_supervisor' and r.company_id == company for r in recorder.records)
        add(f'iteration_limit_{company}', True, count <= workflow.max_iterations, company=company)
    for item in recorder.research:
        add('research_evidence_' + item.invocation_id, 'new_evidence', item.outcome,
            company=item.company_id, invocation=item.invocation_id)
        reevaluation = next((r for r in successful if r.company_id == item.company_id
                             and r.operation_id == ('technical_eval' if item.section_id == 'technology' else 'market_eval')
                             and r.started_at >= next(x.ended_at for x in recorder.records
                                                      if x.invocation_id == item.invocation_id)), None)
        item.reevaluation_invocation_id = reevaluation.invocation_id if reevaluation else None
        add('research_recheck_' + item.invocation_id, True, reevaluation is not None,
            company=item.company_id, invocation=item.invocation_id)
    add('all_required_artifacts_saved', True, all(a.status == 'saved' for a in recorder.artifacts if a.required))
    for format in workflow.requested_formats:
        add('artifact_' + format, True, any(a.format == format and a.status == 'saved' for a in recorder.artifacts))
    data = result.model_dump() if hasattr(result, 'model_dump') else (result or {})
    branch = data.get('policy_decision') if recorder.path == 'agents' else data.get('branch')
    add('report_branch', True, branch in {'top3', 'hold'})
    selected_report = ({'top3': 'generate_investment_report', 'hold': 'generate_hold_report'}
                       if recorder.path == 'agents' else {'top3': 'top_report', 'hold': 'hold_report'}).get(branch)
    add('selected_report_node_executed', True, any(r.operation_id == selected_report for r in successful))
    report = data.get('final_report') if recorder.path == 'agents' else data.get('report_markdown')
    add('final_report_nonempty', True, bool((report or '').strip()))
    from .storage import _redact_text
    report_file = recorder.directory / 'report_original.md'
    add('final_report_matches_saved_artifact', _redact_text(report or ''),
        report_file.read_text() if report_file.is_file() else None)
    if recorder.path == 'investment_pipeline':
        company_names = [d.get('company_name') for d in data.get('decisions', [])]
        add('decision_company_coverage', sorted(recorder.candidates), sorted(company_names))
    return checks
