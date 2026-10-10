#!/usr/bin/env python3
"""Resumable GPT Image 2.5 batch, exclusively for classified reconstructed pictures.

Requires full post-reconstruction vision triage and source SHA verification.
Reprocesses all legacy direct-edit PNGs from scratch in the NEW internal-composite
workspace; never treats an old atlas image edit as a new completed task.
"""
from __future__ import annotations
import argparse,json,sys,time,urllib.error
from collections import Counter
from pathlib import Path

import preprocess_reconstructed as stage
import internal_texture_image25 as atlas
import run_mltd_internal_image25 as edit
import provider_config

WORK=atlas.WORK
PRE=stage.PRE
PLAN=PRE/"batch-plan.json"
PROGRESS=PRE/"batch-progress.json"
FAILURES=PRE/"batch-errors.jsonl"
def save(obj,path=PROGRESS):
 atlas.write(path,obj)
def load_plan():
 catalog=json.loads(stage.CATALOG.read_text(encoding="utf-8"))
 report=json.loads((PRE/"preprocess-report.json").read_text(encoding="utf-8"))
 summary=json.loads(stage.SUMMARY.read_text(encoding="utf-8"))
 if summary["not_yet_classified_unique"] or summary["classified_sheets"]!=summary["total_sheets"]:
  raise ValueError("Incomplete POST-RECONSTRUCTION visual classification; do not start image generation.")
 if len(report["rows"])!=summary["prepared_unique_composites"]:
  raise ValueError("Classification inventory mismatch")
 indexed={item["task_id"]:item for item in catalog["source_bound_composites"]}
 tasks=[]
 for result in report["rows"]:
  task_id=result["task_id"]
  item=indexed[task_id]
  if result["source_sha256"]!=item["prepared_sha256"]:
   raise ValueError("Prepared image source changed: "+task_id)
  if result["status"]=="skip_no_japanese_high_confidence":
   continue
  if result["status"] not in {"needs_image_edit","manual_review"}:
   raise ValueError("Unsupported/unknown classification status: "+result["status"])
  prepared=WORK/item["original"]
  if not prepared.is_file() or atlas.sha(prepared)!=item["prepared_sha256"]:
   raise ValueError("Prepared image file SHA mismatch: "+task_id)
  tasks.append({"task_id":task_id,"texture_id":item["representative_id"],
                "source_sha256":item["prepared_sha256"],
                "original_sha256":item["original_sha256"],
                "members":item["ids"],"source":item["original"],
                "classification":result["classification"],"status":result["status"],
                "was_old_direct_edit":any(
                  (WORK/atlas.manifest()[member]["edited"]).is_file()
                  for member in item["ids"])})
 tasks.sort(key=lambda t:(not t["was_old_direct_edit"],t["task_id"]))
 evidence={"schema_version":1,"source":"post-reconstruction-triage-only",
           "classified_sheets":summary["classified_sheets"],
           "total_sheets":summary["total_sheets"],
           "auto_skipped_unique":summary["automatic_no_japanese_skip_unique"],
           "auto_skipped_source_objects":summary["automatic_no_japanese_skip_objects"],
           "duplicate_objects":summary["duplicate_original_objects"],
           "manual_layout_exceptions":summary["manual_review_missing_source"],
           "generation_tasks":len(tasks),
           "reprocessed_legacy_direct_edit_candidates":sum(t["was_old_direct_edit"] for t in tasks),
           "tasks":tasks}
 return evidence
def progress_state(evidence,quality):
 old= json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.is_file() else {}
 if old and old["inventory"]!=len(evidence["tasks"]):
  raise ValueError("Prior progress inventory differs; reconcile before restarting")
 return {"schema_version":1,"status":"running","inventory":len(evidence["tasks"]),
         "model":provider_config.load_config()["image_provider"]["model"],"quality":quality,
         "auto_skipped_unique":evidence["auto_skipped_unique"],
         "duplicate_objects":evidence["duplicate_objects"],
         "manual_layout_exceptions":len(evidence["manual_layout_exceptions"]),
         "reprocess_old_direct_edit_count":evidence["reprocessed_legacy_direct_edit_candidates"],
         "finished_this_run":0,"failed_this_run":0,"deferred_this_run":0,
         "last_task":old.get("last_task",""),
         "notes":"Only post-reconstruction JP/uncertain tasks. No legacy direct edits reused."}
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--workers",type=int,default=1)
 ap.add_argument("--quality",choices=["medium","high"],default="high")
 ap.add_argument("--timeout",type=int,default=300)
 ap.add_argument("--max-items",type=int,default=0)
 args=ap.parse_args()
 if args.workers!=1:raise ValueError("Safety limit: one GPT Image worker for first batch")
 evidence=load_plan()
 if PLAN.is_file():
  prior=json.loads(PLAN.read_text(encoding="utf-8"))
  if [(v["task_id"],v["source_sha256"]) for v in prior["tasks"]]!=[
      (v["task_id"],v["source_sha256"]) for v in evidence["tasks"]]:
   raise ValueError("Batch plan has changed since last run")
 else:save(evidence,PLAN)
 pending=[t for t in evidence["tasks"] if not (
  atlas.OUT/t["task_id"]/"restored-texture.png").is_file()]
 if args.max_items>0:pending=pending[:args.max_items]
 status=progress_state(evidence,args.quality)
 status.update({"queued_this_run":len(pending),"already_processed":len(evidence["tasks"])-
        sum(not (atlas.OUT[t["task_id"]]/"restored-texture.png").is_file() for t in evidence["tasks"])})
 save(status)
 print("START",json.dumps({k:v for k,v in status.items() if k not in ("notes",)},ensure_ascii=False),flush=True)
 for idx,t in enumerate(pending,1):
  folder=atlas.OUT/t["task_id"]
  info=json.loads((folder/"task.json").read_text(encoding="utf-8"))
  if info["prepared_sha256"]!=t["source_sha256"] or info["source_sha256"]!=t["original_sha256"]:
   raise ValueError("Source identity changed: "+t["task_id"])
  try:
   for attempt in range(1,5):
    try:
     outcome=edit.edit(t["task_id"],args.quality,args.timeout)
     break
    except urllib.error.HTTPError as exc:
     if exc.code not in (408,429,500,502,503,504) or attempt==4:
      raise
     delay=min(45,5*2**(attempt-1))
     print("RETRY_PROVIDER",t["task_id"],"HTTP",exc.code,"attempt",attempt,
           "delay_seconds",delay,flush=True)
     time.sleep(delay)
   state=outcome["status"]
   if state=="generated_unreviewed":
    status["finished_this_run"]+=1
    # After a successful re-edit, show the NEW source-bound texture to the
    # user, not an older directly edited atlas with the same Texture2D ID.
    choice=WORK/"internal-composites"/"review-selection.json"
    selected=json.loads(choice.read_text(encoding="utf-8")) if choice.is_file() else {}
    if t["texture_id"] in selected and selected[t["texture_id"]]!=t["task_id"]:
     selected[t["texture_id"]]=t["task_id"]
     save(selected,choice)
   else:status["deferred_this_run"]+=1
  except urllib.error.HTTPError as exc:
   # An upstream 502 is a provider failure, not a reason to corrupt assets or
   # claim a generation. Stop instead of consuming a whole batch in errors.
   body=exc.read(350).decode("utf-8","replace")
   with FAILURES.open("a",encoding="utf-8") as f:
    f.write(json.dumps({"task_id":t["task_id"],"http_code":exc.code,
                        "error":body},ensure_ascii=False)+"\n")
   status["failed_this_run"]+=1
   status["status"]="paused_provider_error";status["last_task"]=t["task_id"]
   save(status);print("STOP_PROVIDER",exc.code,t["task_id"],flush=True)
   return 2
  except Exception as exc:
   with FAILURES.open("a",encoding="utf-8") as f:
    f.write(json.dumps({"task_id":t["task_id"],"error":type(exc).__name__+": "+str(exc)[:500]},ensure_ascii=False)+"\n")
   status["failed_this_run"]+=1
   status["status"]="paused_internal_error";status["last_task"]=t["task_id"]
   save(status);print("STOP_INTERNAL",type(exc).__name__,str(exc)[:250],flush=True)
   return 2
  status["last_task"]=t["task_id"]
  save(status)
  print("PROGRESS",idx,len(pending),t["task_id"],state,flush=True)
 status["status"]="completed"
 save(status)
 return 0
if __name__=="__main__":raise SystemExit(main())
