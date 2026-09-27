#!/usr/bin/env python3
"""Source-bound, post-reconstruction Japanese-text triage for ONE-texture atlases.

All 2-Sprite source Texture2Ds are reconstructed using the frozen Unity metadata
BEFORE classification. No raw-atlas no-Japanese labels are reused. Exact raw
duplicates are represented once but retained as independent source locators.
This script never calls image_generation or alters official Unity bundles.
"""
from __future__ import annotations
import argparse,json,hashlib
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import discover_sprite_layout as discover
import preprocess_mltd_image25 as vision
import internal_texture_image25 as internal

WORK=internal.WORK
PRE=WORK/"reconstructed-preprocess"
CATALOG=PRE/"catalog.json"
SUMMARY=PRE/"summary.json"
def prepare_index():
 rows=list(internal.manifest().values())
 by_raw=defaultdict(list)
 for row in rows:by_raw[row["original_sha256"]].append(row)
 entries=[];missing=[]
 for idx,(raw_sha,members) in enumerate(sorted(by_raw.items())):
  r=members[0]
  tid="recon-"+raw_sha[:24]
  try:
   prepared=discover.discover(r["id"],tid)
   entries.append({"task_id":tid,"representative_id":r["id"],"original_sha256":raw_sha,
                   "prepared_sha256":prepared["prepared_sha256"],
                   "original":prepared["prepared_image"],"size":prepared["prepared_size"],
                   "ids":[x["id"] for x in members],"representative_original":r["original"],
                   "bundle":r["bundle"],"model_generated":False})
  except (ValueError,KeyError,TypeError) as exc:
   missing.append({"id":r["id"],"member_ids":[x["id"] for x in members],
                   "original_sha256":raw_sha,
                   "reason":type(exc).__name__+": "+str(exc)[:300]})
  if (idx+1)%150==0:print("PREPARED",idx+1,len(by_raw),len(entries),len(missing),flush=True)
 vision.atomic(CATALOG,{"schema_version":1,"all_original_objects":len(rows),
                       "distinct_raw_originals":len(by_raw),
                       "exact_duplicate_objects":len(rows)-len(by_raw),
                       "source_bound_composites":entries,"manual_review":missing})
 return entries,missing,len(rows),len(by_raw)
def inventory():
 data=json.loads(CATALOG.read_text(encoding="utf-8"))
 rows=[]
 for e in data["source_bound_composites"]:
  source=WORK/e["original"]
  if not source.is_file() or vision.sha(source)!=e["prepared_sha256"]:
   raise ValueError("Prepared composite missing/modified: "+e["task_id"])
  rows.append({"source_sha256":e["prepared_sha256"],
               "representative_id":e["task_id"],"original":e["original"],
               "size":e["size"],"ids":e["ids"],"task_id":e["task_id"],
               "raw_sha256":e["original_sha256"]})
 return data,rows
def classify_pending(groups:list[dict],workers:int,limit:int,timeout:int):
 pending=[g for g in groups if not (PRE/"classifications"/f"sheet-{g['sheet']:04d}.json").exists()]
 if limit>0:pending=pending[:limit]
 def run(group):
  fname=PRE/"classifications"/f"sheet-{group['sheet']:04d}.json"
  try:
   data=vision.classify(group,timeout)
   vision.atomic(fname,data)
   return group["sheet"],"ok",dict(Counter(x["label"] for x in data["labels"]))
  except Exception as exc:
   vision.atomic(PRE/"failures"/f"sheet-{group['sheet']:04d}.json",
                 {"sheet":group["sheet"],"error":type(exc).__name__+": "+str(exc)[:1200]})
   return group["sheet"],"error",str(exc)[:150]
 if not pending:return
 with ThreadPoolExecutor(max_workers=workers) as pool:
  futures={pool.submit(run,g):g for g in pending}
  for fut in as_completed(futures):
   i,status,out=fut.result()
   print("CLASSIFY",i,status,out,flush=True)
def report(catalog:dict,uniques:list[dict],groups:list[dict]):
 base=vision.collect(uniques,groups)
 missing=catalog["manual_review"]
 counts=Counter(base["counts"])
 # A high-confidence skip is valid only after source-bound reconstruction.
 report={
  "schema_version":1,"total_original_textures":catalog["all_original_objects"],
  "duplicate_original_objects":catalog["exact_duplicate_objects"],
  "unique_raw_originals":catalog["distinct_raw_originals"],
  "prepared_unique_composites":len(uniques),
  "unable_to_reconstruct_unique":len(missing),
  "unable_to_reconstruct_objects":sum(len(m["member_ids"]) for m in missing),
  "classified_sheets":base["classified_sheets"],"total_sheets":base["total_sheets"],
  "automatic_no_japanese_skip_unique":counts["skip_no_japanese_high_confidence"],
  "automatic_no_japanese_skip_objects":sum(len(r["ids"]) for r in base["rows"] if r["status"]=="skip_no_japanese_high_confidence"),
  "needs_edit_unique":counts["needs_image_edit"],
  "needs_edit_objects":sum(len(r["ids"]) for r in base["rows"] if r["status"]=="needs_image_edit"),
  "uncertain_unique":counts["manual_review"],"not_yet_classified_unique":counts["pending_visual_classification"],
  "manual_review_missing_source":missing,"complete":base["classified_sheets"]==base["total_sheets"]
      and counts["pending_visual_classification"]==0 and len(missing)==0,
  "note":"Old raw-atlas labels and old GPT Image outputs are not reused. No Japanese skip only after full internal reconstruction."
 }
 vision.atomic(SUMMARY,report)
 return report
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--prepare",action="store_true")
 ap.add_argument("--classify",action="store_true")
 ap.add_argument("--workers",type=int,default=3)
 ap.add_argument("--max-sheets",type=int,default=0)
 ap.add_argument("--timeout",type=int,default=None,
                 help="Override vision.timeout_seconds in config.json")
 args=ap.parse_args()
 if not 1<=args.workers<=6:raise ValueError("workers must be 1..6")
 vision.PRE=PRE
 if args.prepare or not CATALOG.is_file():prepare_index()
 catalog,rows=inventory()
 groups=vision.create_sheets(rows)
 vision.atomic(PRE/"sheets-manifest.json",groups)
 print("INDEX",len(rows),"unique reconstructions",len(catalog["manual_review"]),
       "manual layouts",len(groups),"contact sheets",flush=True)
 if args.classify:classify_pending(groups,args.workers,args.max_sheets,args.timeout)
 print("REPORT",json.dumps(report(catalog,rows,groups),ensure_ascii=False),flush=True)
if __name__=="__main__":main()
