"""#26 independent examples, corruptions, offline graph and storage integration."""
import copy
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.rules import check_rules, load_spec, summarize

FIXTURES = json.loads((Path(__file__).parent / 'fixtures/rule_cases_v1.json').read_text())


def profile(name='A', stage='Series A', **signals):
    return {'name': name, 'stage': stage, **{k + '_signal': signals.get(k, 3)
            for k in ('technology', 'team', 'market', 'traction', 'competition', 'risk')}}


def baseline_row(p, total=60, market_contribution=21, grade='Watchlist'):
    # Explicit baseline observation builder, never calls the evaluator/product scorer.
    stage_weights = {'Seed': (15, 18, 18, 6, 3), 'Series A': (15, 21, 15, 6, 3),
                     'Series B': (12, 27, 12, 6, 3), 'Series C+': (9, 24, 9, 9, 9)}
    values = stage_weights[p['stage']]
    row = {'company_name': p['name'], 'stage': p['stage'], 'final_score': total, 'recommendation': grade}
    for field, score, contribution in zip(('technical_evaluation', 'market_evaluation', 'team_evaluation', 'competition_evaluation', 'risk_analysis'),
                                         (3, 3, 3, 3, 3), values):
        row[field] = {'score': score, 'weighted_score': contribution}
    row['market_evaluation']['weighted_score'] = market_contribution
    return row


def result(rows, passed=None, top=None, branch=None):
    passed = passed or []
    top = top if top is not None else passed[:3]
    branch = branch or ('top3' if top else 'hold')
    table = '| Company | Stage | Final Score | Recommendation |\n|---|---|---|---|\n'
    table += '\n'.join(f"| {r['company_name']} | {r['stage']} | {r['final_score']} | {r['recommendation']} |" for r in rows)
    if top:
        table += '\n\n| Rank | Company | Stage | DD Score | Recommendation |\n|---|---|---|---|---|\n'
        by_name = {r['company_name']: r for r in rows}
        table += '\n'.join(f"| {i} | {n} | {by_name[n]['stage']} | {by_name[n]['final_score']} | {by_name[n]['recommendation']} |" for i, n in enumerate(top, 1))
    return {'decisions': rows, 'branch': branch,
            'ranking': {'branch': branch, 'passed_companies': passed, 'top_companies': top,
                        'score_threshold': 65, 'high_priority_threshold': 80,
                        'watchlist_companies': [r['company_name'] for r in rows if r['recommendation'] == 'Watchlist']},
            'report_markdown': table}


class RuleTests(unittest.TestCase):
    def check(self, data, profiles, target=False, weights=None, settings=None):
        return check_rules('run', 'investment_pipeline', data, profiles=profiles,
            settings=settings, observed_weights=weights if weights is not None else load_spec()['weights'],
            policy_version='investment_pipeline-target-v1' if target else None)

    def test_explicit_stage_market_traction_discrepancies(self):
        for case in FIXTURES['unequal_market_traction']:
            with self.subTest(stage=case['stage']):
                p = profile(stage=case['stage'], market=5, traction=1)
                data = result([baseline_row(p, case['baseline_total'], case['baseline_market'])])
                baseline = self.check(data, {'A': p})
                self.assertFalse([c for c in baseline if c.status in ('fail', 'error')])
                target = self.check(data, {'A': p}, target=True)
                total = next(c for c in target if c.check_id.endswith(':score_formula'))
                contribution = next(c for c in target if c.check_id.endswith(':contribution_market'))
                self.assertEqual(total.expected, case['target_total'])
                self.assertEqual(contribution.expected, case['target_market'])
                self.assertEqual(contribution.status, 'fail')

    def test_bad_arithmetic_risk_weights_and_report_fail_independently(self):
        p = profile()
        data = result([baseline_row(p)])
        data['decisions'][0]['risk_analysis']['score'] = 5
        data['decisions'][0]['technical_evaluation']['weighted_score'] = 99
        data['decisions'][0]['final_score'] = 65
        data['report_markdown'] = data['report_markdown'].replace('| 60 |', '| 65 |')
        weights = copy.deepcopy(load_spec()['weights'])
        weights['Series A']['team'] = .5
        checks = self.check(data, {'A': p}, weights=weights)
        failures = {c.check_id.rsplit(':', 1)[1] for c in checks if c.status == 'fail'}
        self.assertTrue({'risk_direction', 'stage_weights', 'weight_sum', 'contribution_technology', 'score_formula'} <= failures)
        self.assertTrue(any(c.check_id.endswith(':report_score_1') and c.status == 'fail' for c in checks))

    def test_top3_order_stable_ties_and_hold(self):
        names = ['Z', 'A', 'C', 'B']
        profiles = {n: profile(n) for n in names}
        rows = [baseline_row(p) for p in profiles.values()]
        data = result(rows)
        self.assertFalse([c for c in self.check(data, profiles) if c.status in ('fail', 'error')])
        # Identical 60s at a configured threshold 60: input order wins a tie.
        data = result(rows, passed=names)
        data['ranking']['score_threshold'] = 60
        data['ranking']['top_companies'] = ['A', 'Z', 'C']
        checks = self.check(data, profiles, settings={'selective_dd_threshold': 60})
        self.assertTrue(any(c.check_id.endswith(':ranking_top_companies') and c.status == 'fail' for c in checks))

    def test_all_grade_boundaries_and_65_inclusion(self):
        # Explicit weights for Seed in units of one point per signal: 5/6/6/2/1.
        # Find signal vectors; boundary expectations are versioned explicit fixtures.
        for case in FIXTURES['boundaries']:
            vector = next(v for v in itertools.product(range(1, 6), repeat=5)
                          if 5*v[0] + 6*v[1] + 6*v[2] + 2*v[3] + v[4] == case['score'])
            tech, market, team, competition, risk = vector
            p = profile(stage='Seed', technology=tech, market=market, traction=market,
                        team=team, competition=competition, risk=6-risk)
            row = baseline_row(p, case['score'], 6*market, case['grade'])
            for field, score, contribution in zip(('technical_evaluation','market_evaluation','team_evaluation','competition_evaluation','risk_analysis'),
                                                  vector, (5*tech,6*market,6*team,2*competition,risk)):
                row[field] = {'score': score, 'weighted_score': contribution}
            data = result([row], passed=['A'] if case['selected'] else [])
            for target in (False, True):
                self.assertFalse([c for c in self.check(data, {'A': p}, target=target) if c.status in ('fail', 'error')])
            if case['score'] == 65:
                data['ranking'].update(score_threshold=70, high_priority_threshold=90)
                checks = self.check(data, {'A':p}, target=True,
                    settings={'selective_dd_threshold':70, 'high_priority_threshold':90})
                self.assertTrue(any(c.check_id.endswith(':grade') and c.expected == 'Watchlist' and c.status == 'fail' for c in checks))
        data['branch'] = 'hold'  # At 80 this must fail even with a faithful report.
        self.assertTrue(any(c.check_id.endswith(':recommendation_route') and c.status == 'fail'
                            for c in self.check(data, {'A': p})))

    def test_configurable_target_grades_and_unknown_policy(self):
        p = profile()
        data = result([baseline_row(p)])
        data['ranking'].update(score_threshold=70, high_priority_threshold=90)
        checks = self.check(data, {'A': p}, target=True,
                            settings={'selective_dd_threshold': 70, 'high_priority_threshold': 90})
        self.assertFalse([c for c in checks if c.status in ('fail', 'error')])
        checks = check_rules('r', 'agents', {}, policy_version='investment_pipeline-target-v1')
        self.assertEqual(checks[0].status, 'error')

    def test_errors_excluded_na_and_no_scale_combination(self):
        checks = self.check(result([baseline_row(profile())]), {})
        summary = summarize(checks)
        self.assertIsNone(summary['investment_pipeline-baseline-v1']['K'])
        row = {'company_name':'A', **{k+'_score':3 for k in ('technology','market','business','team','risk','competition')}, 'total_score':18}
        data = {'companies':['A'], 'evaluations':[row], 'selected_companies':[], 'hold_companies':[row], 'policy_decision':'hold', 'final_report':'충실한 문맥 요약'}
        checks = check_rules('r', 'agents', data)
        self.assertFalse([c for c in checks if c.status in ('fail', 'error')])
        self.assertEqual(summarize(checks)['agents-baseline-v1']['K'], 1)
        self.assertTrue(any(c.status == 'not_applicable' for c in checks))
        row['technology_score'] = 0
        self.assertTrue(any(c.status == 'fail' for c in check_rules('r', 'agents', data)))

    def test_min_mid_max_all_stages(self):
        for stage in ('Seed', 'Series A', 'Series B', 'Series C+'):
            for value, total, grade in ((1, 20, 'No DD'), (3, 60, 'Watchlist'), (5, 100, 'High Priority DD')):
                p = profile(stage=stage, technology=value, team=value, market=value,
                            traction=value, competition=value, risk=6-value)
                row = baseline_row(p, total, grade=grade)
                # Scale explicit 3-point contribution examples to 1 or 5.
                for item in ('technical_evaluation','market_evaluation','team_evaluation','competition_evaluation','risk_analysis'):
                    row[item]['score'] = value
                    row[item]['weighted_score'] *= value / 3
                base_market = {'Seed':18, 'Series A':21, 'Series B':27, 'Series C+':24}[stage]
                row['market_evaluation']['weighted_score'] = base_market * value / 3
                data = result([row], passed=['A'] if total >= 65 else [])
                self.assertFalse([c for c in self.check(data, {'A':p}) if c.status in ('fail','error')])

    def test_agents_threshold_boundary(self):
        for score, first in ((19,4),(20,5)):
            row = {'company_name':'A', **{k+'_score':3 for k in ('technology','market','business','team','risk','competition')}, 'total_score':score}
            row['technology_score'] = first
            selected = [row] if score == 20 else []
            data = {'companies':['A'], 'evaluations':[row], 'selected_companies':selected,
                    'hold_companies':[] if selected else [row], 'policy_decision':'top3' if selected else 'hold',
                    'final_report':'fixture'}
            self.assertFalse([c for c in check_rules('r','agents',data) if c.status in ('fail','error')])

    def test_real_graph_records_replay_and_integrity(self):
        from evaluation.recording import RunRecorder
        from evaluation.rule_cli import replay
        from investment_pipeline import graph, services
        from investment_pipeline.config import settings
        from investment_pipeline.models import CompanyProfile
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp, patch.object(settings, 'enable_live_research', False), \
             patch.object(settings, 'enable_llm_enrichment', False), patch.object(services, 'get_knowledge_base', return_value=None):
            rec = RunRecorder(root=Path(temp), execution_path='investment_pipeline', inputs={}, settings={},
                              requested_formats=['md'], repository=repo)
            p = CompanyProfile(name='Fixture', stage='Series A', market_signal=5, traction_signal=1)
            rec.candidates = [p.name]
            with rec.activate():
                other = CompanyProfile(name='Other', stage='Series A', market_signal=5, traction_signal=1)
                rec.candidates.append(other.name)
                output = graph.run_pipeline('fixture', [p, other])
                path = Path(temp) / 'report.md'
                path.write_text(output.report_markdown)
                rec.artifact(path, 'report_original.md', 'md')
            rec.finish(result=output)
            summary = json.loads((rec.directory / 'rule_conformance.json').read_text())['data']
            self.assertEqual(summary['policies']['investment_pipeline-baseline-v1']['K'], 1)
            self.assertLess(summary['policies']['investment_pipeline-target-v1']['K'], 1)
            self.assertTrue((rec.directory / '.frozen').exists())
            replayed = replay(rec.directory)
            self.assertEqual(replayed['rule_conformance']['policies'], summary['policies'])
            checks = json.loads((rec.directory / 'checks.json').read_text())
            self.assertTrue(any(c['check_id'].startswith('rule:') and c['invocation_id'] for c in checks))
            (rec.directory / 'decisions.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                replay(rec.directory)


if __name__ == '__main__':
    unittest.main()
