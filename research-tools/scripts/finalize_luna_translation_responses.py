#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Finalize file-based Luna adapter translation responses with deterministic QA."""
from __future__ import annotations
import argparse, collections, json, sys
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path: sys.path.insert(0,str(REPO))
if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8',errors='backslashreplace')

from scripts.translate_gtx_queue import mask_tokens, restore_tokens
from scripts.mltd_localize_gtx import read_jsonl, validate_translation
from scripts.mltd_translation_quality import load_glossary, evaluate_row


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--queue',type=Path,required=True)
    ap.add_argument('--response',type=Path,action='append',required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--summary',type=Path,required=True)
    ap.add_argument('--glossary',type=Path)
    args=ap.parse_args()

    queue=read_jsonl(args.queue)
    queue_ids={str(r.get('source_sha256','')) for r in queue}
    responses={}
    for p in args.response:
        value=json.loads(p.read_text(encoding='utf-8-sig'))
        rows=value.get('translations',[])
        if not isinstance(rows,list): raise ValueError(f'{p}: missing translations[]')
        for item in rows:
            sid=str(item.get('id','')); text=str(item.get('text',''))
            if not sid or not text: raise ValueError(f'{p}: invalid translation row')
            if sid in responses: raise ValueError(f'duplicate response id: {sid}')
            responses[sid]=text
    unknown=sorted(set(responses)-queue_ids)
    if unknown: raise ValueError(f'unknown response ids: {unknown[:3]}')

    glossary=load_glossary(args.glossary)
    out=[]; counts=collections.Counter()
    for q in queue:
        sid=str(q.get('source_sha256',''))
        if sid not in responses: continue
        source=str(q.get('source',''))
        _,tokens=mask_tokens(source)
        translated=restore_tokens(responses[sid],tokens)
        validate_translation(source,translated)
        row={'source_sha256':sid,'source':source,'translation':translated,'status':'machine_translated','provenance':'machine:codex-luna-adapter','model':'gpt-5.6-luna','occurrences':q.get('occurrences'),'examples':q.get('examples',[])}
        qa=evaluate_row(q,row,glossary)
        if qa['qa_verdict']!='PASS':
            row['status']='needs_review'; row['deterministic_qa_verdict']=qa['qa_verdict']; row['deterministic_qa_issues']=qa['issues']
        out.append(row); counts[qa['qa_verdict']]+=1
        for issue in qa['issues']: counts['issue:'+issue['code']]+=1

    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('w',encoding='utf-8',newline='\n') as f:
        for row in out: f.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
    summary={'schema_version':1,'queue_rows':len(queue),'response_files':[str(p) for p in args.response],'response_rows':len(responses),'finalized_rows':len(out),'missing_rows':len(queue)-len(out),'qa':dict(counts),'all_response_ids_known':not unknown}
    args.summary.parent.mkdir(parents=True,exist_ok=True)
    args.summary.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
