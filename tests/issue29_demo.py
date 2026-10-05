"""Reproducible offline #29 demonstration, explicitly synthetic (not model accuracy)."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from evaluation.experiment import EXPERIMENT
from evaluation.factual_models import FactComparison, JudgmentProposal
from evaluation.models import ModelSettings
from evaluation.paired import paired_report, preserve, write_report
from evaluation.paired_cli import evaluate_pair, save_new
from evaluation.ragas_adapter import RagasAdapter
from evaluation.stage_samples import prepare_stage_package, write_stage_package
from tests.test_evaluation_claims import ClaimsTests, LABELS
from tests.test_evaluation_factual import FixedJudge
from tests.test_evaluation_grounding import FixtureJudge
from tests.test_evaluation_paired import RequiredJudge
from tests.test_evaluation_ragas_adapter import RagasAdapterTests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = (args.output or Path('evaluation_runs') / ('issue29-demo-' + uuid4().hex)).resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixture = ClaimsTests()
    fixture.setUp()
    fixture.root = root
    try:
        dataset, case = fixture.reviewed_dataset()
        runs, packages = [], []
        for role in ('baseline', 'refactored'):
            token = EXPERIMENT.set({'role': role, 'data_mode': 'frozen', 'synthetic': True})
            try:
                rec = fixture.recorder(settings={'llm_enabled': False, 'live_research_enabled': False, 'model':'fixture'})
                rec.candidates = ['FixtureChip']
                rec.manifest.as_of_date = case.as_of_date
                with rec.activate():
                    target = fixture.call(rec, 'evaluate_business', LABELS['cases'][9]['text'])
                    report = '# Synthetic report\n### FixtureChip\n#### Business\n' + LABELS['cases'][9]['text'] + '\n'
                    source = fixture.call(rec, 'generate_hold_report', report, company=None, upstream=LABELS['cases'][9]['text'])
                    path = root / f'{role}.md'
                    path.write_text(report)
                    with rec.span('save_outputs', {}) as span:
                        rec.artifact(path, 'report_original.md', 'md')
                        span['output'] = {'saved': True}
                rec.generation_complete()
                rec.finish(result={'companies': ['FixtureChip'], 'policy_decision': 'hold', 'final_report': report})
            finally:
                EXPERIMENT.reset(token)
            if role == 'baseline':
                receipt = preserve(rec.directory, note='synthetic paired demonstration, not verified investment quality')
                save_new(root/'baseline-receipt.json', receipt)
            package = prepare_stage_package(rec.directory, fixture.extractor, dataset_directory=dataset,
                case_bindings={r.invocation_id+'|FixtureChip|business': case.case_id for r in (target, source)}, allow_synthetic=True)
            runs.append(rec.directory)
            packages.append(write_stage_package(package, root/f'{role}-package'))
        fact = package['dataset']['facts'][0]
        judge = FixedJudge(JudgmentProposal(comparisons=[FactComparison(fact_id=fact['fact_id'], relation='supports',
            same_entity=True, same_attribute=True, same_event=True, same_time=True, same_conditions=True,
            claim_quantity_quote='2곳', reference_quantity_quote='2곳', reason='synthetic equal count')], reason='fixture'))
        RagasAdapterTests.setUpClass()
        adapter = RagasAdapter(llm=RagasAdapterTests.llm, model=ModelSettings(provider='fixture', model='deterministic'))
        attempt = asyncio.run(evaluate_pair(runs, packages, dataset, adapter, output=root/'paired-attempt.json',
            receipt=receipt, factual_judge=judge, support_judge=FixtureJudge(), coverage_judge=RequiredJudge(None), allow_synthetic=True))
        report = paired_report(*attempt['evaluations'], data_mode='frozen', baseline_receipt=receipt)
        write_report(report, root/'comparison')
        summary = {'synthetic':True, 'human_reviewed':False, 'paid_api_calls':0,
            'ragas_version':'0.4.3', 'quality':report['quality'],
            'samples': [{'key':r['key'], 'pair_status':r['pair_status'],
                'baseline':r['baseline']['values'], 'after':r['after']['values'],
                'difference':r['difference']} for r in report['samples']],
            'workflow': {s:{k:report['workflow'][s][k] for k in ('attempted_runs','successful_runs','success_rate','generation_status','workflow_status')}
                         for s in ('baseline','after')},
            'artifacts':str(root/'comparison')}
        save_new(root/'summary.json',summary)
        print(json.dumps({'output':str(root), 'summary':summary},ensure_ascii=False,indent=2))
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
