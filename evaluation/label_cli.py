"""보관된 custom/native 추출 결과를 명시적 label과 대조한다. 새로운 LLM 호출 없음."""
import argparse
import json
from pathlib import Path

from .extraction_labels import compare_labels


def compare_package(labels, package, native_records=()):
    units, native = [], []
    for audit in package['extractions']:
        matched_cases = [c for c in labels['cases'] if c['text'] in audit['unit']['text']]
        if not matched_cases:
            continue
        expected = {'facts': [s for c in matched_cases for s in c['facts']], 'kinds': [k for c in matched_cases for k in c['kinds']]}
        atoms = [a['candidate'] for a in package['atoms'] if a['unit_id']==audit['unit']['unit_id']]
        units.append({'unit_id': audit['unit']['unit_id'], 'case_ids': [c['id'] for c in matched_cases],
                      'extraction_status': audit['status'], **compare_labels(expected, atoms)})
    # native claim 집합은 metric별/attempt별로 분리한다. reference에서 추출한 claim은 제외한다.
    for sample in package['samples']:
        locations = {u['unit_id'] for u in package['atoms'] if u['sample_id']==sample['sample_id']}
        texts = [a['unit']['text'] for a in package['extractions'] if a['unit']['unit_id'] in locations]
        matched_cases = [c for c in labels['cases'] if any(c['text'] in t for t in texts)]
        expected = {'facts': [s for c in matched_cases for s,k in zip(c['facts'],c['kinds'],strict=True) if k=='fact'],
                    'kinds': ['fact' for c in matched_cases for k in c['kinds'] if k=='fact']}
        relevant = [r for r in native_records if r['sample_id']==sample['sample_id'] and r['source']=='response']
        groups = {(r['metric_name'],r['attempt'],r['callback_run_id']) for r in relevant}
        for metric, attempt, callback in groups:
            actual = [{'statement':r['statement'],'kind':'fact'} for r in relevant if (r['metric_name'],r['attempt'],r['callback_run_id'])==(metric,attempt,callback)]
            native.append({'sample_id':sample['sample_id'],'metric_name':metric,'attempt':attempt,'callback_run_id':callback,
                           **compare_labels(expected,actual)})
    return {'label_version':labels['label_version'], 'label_author':labels['author'], 'human_reviewed':labels['human_reviewed'],
            'custom_units':units,'native_sets':native,'unmatched_label_case_ids':[c['id'] for c in labels['cases'] if not any(c['id'] in u['case_ids'] for u in units)],
            'reason':'원문/종류 exact-match 누락·과분해 진단. 실제 내용 정확도 또는 의미 동등성 판정이 아님.'}


def main():
    parser=argparse.ArgumentParser(description='명시적 label과 custom/RAGAS 공개 claim 대조')
    parser.add_argument('--labels',type=Path,required=True)
    parser.add_argument('--package',type=Path,required=True,help='#22 package.json')
    parser.add_argument('--ragas-mappings',type=Path,help='평가 디렉토리 ragas_claim_mappings.jsonl')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    records=[]
    if args.ragas_mappings:
        records=[json.loads(l)['data'] for l in args.ragas_mappings.read_text().splitlines() if l.strip()]
    result=compare_package(json.loads(args.labels.read_text()),json.loads(args.package.read_text()),records)
    with args.output.open('x',encoding='utf-8') as file:
        json.dump(result,file,ensure_ascii=False,indent=2)
    print(args.output)


if __name__=='__main__':
    main()
