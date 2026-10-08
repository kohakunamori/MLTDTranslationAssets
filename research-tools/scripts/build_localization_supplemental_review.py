#!/usr/bin/env python3
"""Build one deduplicated review inventory for unconfirmed JP UI text surfaces.

This is discovery/review only. Rows are intentionally excluded from confirmed
localization coverage until runtime visibility or an exact materialization path
is proven.
"""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from collections import Counter
from typing import Any, Iterable

HIGH = {"user_visible_candidate"}
REVIEW = HIGH | {"short_ambiguous", "short_ui_ambiguous", "other_review"}

def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            if line.strip():
                row=json.loads(line)
                if isinstance(row,dict): yield row

def source_ids(path: Path) -> set[str]:
    out=set()
    for row in read_jsonl(path) or ():
        src=str(row.get("source",""))
        sid=str(row.get("source_sha256","")).strip() or hashlib.sha256(src.encode()).hexdigest()
        if sid: out.add(sid)
    return out

def load_triage(path: Path, surface: str) -> list[dict[str, Any]]:
    doc=json.loads(path.read_text(encoding="utf-8-sig"))
    out=[]
    for row in doc.get("rows",[]):
        if not isinstance(row,dict) or row.get("classification") not in REVIEW: continue
        value=dict(row)
        value["review_surface"]=surface
        out.append(value)
    return out

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace",type=Path,default=Path("build/localization-90200"))
    ap.add_argument("--output",type=Path)
    ap.add_argument("--queue",type=Path)
    args=ap.parse_args()
    w=args.workspace
    out_json=args.output or w/"localization-supplemental-review.json"
    out_queue=args.queue or w/"localization-supplemental-review-queue.jsonl"
    confirmed=source_ids(w/"translation-memory.jsonl") | source_ids(w/"machine-translation-nongtx-queue.jsonl")
    inputs=[
        ("apk_baked_ui", w/"apk-baked-ui-triage.json"),
        ("remote_monobehaviour", w/"remote-monobehaviour-jp-text-triage.json"),
    ]
    merged:dict[str,dict[str,Any]]={}
    raw=Counter()
    excluded=Counter()
    for surface,path in inputs:
        for row in load_triage(path,surface):
            raw[surface]+=1
            src=str(row.get("source",""))
            sid=str(row.get("source_sha256","")).strip() or hashlib.sha256(src.encode()).hexdigest()
            if sid in confirmed:
                excluded[surface]+=1
                continue
            entry=merged.setdefault(sid,{
                "source_sha256":sid,"source":src,"status":"review_required",
                "surfaces":[],"classifications":[],"evidence":[],
            })
            entry["surfaces"].append(surface)
            entry["classifications"].append(str(row.get("classification","")))
            evidence={k:v for k,v in row.items() if k not in {"source","source_sha256","review_surface"}}
            evidence["surface"]=surface
            entry["evidence"].append(evidence)
    rows=[]
    for row in merged.values():
        row["surfaces"]=sorted(set(row["surfaces"]))
        row["classifications"]=sorted(set(row["classifications"]))
        row["review_tier"]="high_user_visible" if any(x in HIGH for x in row["classifications"]) else "ambiguous_default"
        rows.append(row)
    rows.sort(key=lambda r:(0 if r["review_tier"]=="high_user_visible" else 1,r["source_sha256"]))
    counts=Counter(r["review_tier"] for r in rows)
    surface_membership=Counter()
    overlap=0
    for r in rows:
        if len(r["surfaces"])>1: overlap+=1
        for s in r["surfaces"]: surface_membership[s]+=1
    report={
        "schema_version":1,"kind":"mltd-localization-supplemental-review",
        "confirmed_source_ids_excluded":len(confirmed),
        "input_review_rows":dict(raw),"excluded_already_confirmed":dict(excluded),
        "unique_review_candidates":len(rows),"review_tier_counts":dict(counts),
        "surface_membership":dict(surface_membership),"cross_surface_unique":overlap,
        "policy":{
            "high_user_visible":"priority runtime/materialization verification; do not count as translated/confirmed yet",
            "ambiguous_default":"verify dynamic overwrite/runtime visibility before promotion",
        },
        "queue":str(out_queue),"rows":rows,
    }
    out_json.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    with out_queue.open("w",encoding="utf-8",newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r,ensure_ascii=False,separators=(",",":"))+"\n")
    print(json.dumps({k:report[k] for k in ("unique_review_candidates","review_tier_counts","surface_membership","cross_surface_unique","excluded_already_confirmed")},ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__": raise SystemExit(main())
