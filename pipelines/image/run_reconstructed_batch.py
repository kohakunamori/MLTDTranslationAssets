#!/usr/bin/env python3
"""Resume-safe single-texture GPT Image production after completed reconstructed-image triage.

Strictly consumes the SHA-verified 937 eligible unique reconstructed pictures.
Re-runs previously edited standalone atlas images under new recon-<sha> IDs; does
not reuse prior edits or overwrite the user-approved historical pilot. All
outputs are accepted by the automatic audit gate; no human sign-off is required.
"""
from __future__ import annotations
import argparse,hashlib,json,time,urllib.error
from pathlib import Path
import internal_texture_image25 as atlas
import run_mltd_internal_image25 as editor
import preprocess_mltd_image25 as vision
import provider_config

WORK=atlas.WORK
PRE=WORK/"reconstructed-preprocess"
REPORT=PRE/"preprocess-report.json"
SUMMARY=PRE/"summary.json"
QUEUE=PRE/"image-edit-queue.jsonl"
PROGRESS=PRE/"batch-progress.json"
MODERATION_RECORD="moderation-blocked.json"
def moderation_reason(http_code:int, detail:str)->dict|None:
 """Only an explicit structured upstream moderation_blocked is skippable.

 Other HTTP 400/401/403 errors remain fatal; do not treat ordinary transport
 or authorization failures as content filtering, or retry a blocked request.
 """
 if http_code not in {400,403}:return None
 try:
  body=json.loads(detail)
  error=body.get("error",{})
  if not isinstance(error,dict) or error.get("code")!="moderation_blocked":
   return None
  moderation=error.get("moderation_details") or {}
  return {"upstream_error_code":"moderation_blocked",
          "http_code":http_code,"moderation_stage":str(moderation.get("moderation_stage","unknown"))[:64],
          "categories":[str(c)[:64] for c in moderation.get("categories",[])][:10]}
 except (ValueError,TypeError,AttributeError):
  return None
def moderation_path(task_id:str)->Path:
 return atlas.OUT/atlas.taskname(task_id)/MODERATION_RECORD
def blocked_record(job:dict)->dict|None:
 path=moderation_path(job["task_id"])
 if not path.is_file():return None
 doc=json.loads(path.read_text(encoding="utf-8"))
 if (doc.get("status")!="moderation_blocked_manual_review"
     or doc.get("task_id")!=job["task_id"]
     or doc.get("source_sha256")!=job["source_sha256"]
     or doc.get("composite_sha256")!=job["composite_sha256"]):
  raise ValueError("Source-bound moderation record changed: "+job["task_id"])
 return doc
def record_moderation(job:dict,http_code:int,detail:str)->dict:
 reason=moderation_reason(http_code,detail)
 if reason is None:raise ValueError("Cannot mark a non-moderation HTTP error as blocked")
 doc={"schema_version":1,"status":"moderation_blocked_manual_review",
      "task_id":job["task_id"],"texture_id":job["texture_id"],
      "source_sha256":job["source_sha256"],
      "composite_sha256":job["composite_sha256"],"source_ids":job["source_ids"],
      "note":"The image service declined this input. Keep for human review; do not retry or change the input to bypass moderation.",
      **reason}
 path=moderation_path(job["task_id"])
 if path.is_file():
  prev=blocked_record(job)
  return prev
 vision.atomic(path,doc)
 return doc
def prepare_queue()->list[dict]:
 if not REPORT.is_file() or not SUMMARY.is_file():
  raise ValueError("Reconstruct and classify complete pictures before image generation")
 summary=json.loads(SUMMARY.read_text(encoding="utf-8"))
 report=json.loads(REPORT.read_text(encoding="utf-8"))
 if (summary["classified_sheets"]!=summary["total_sheets"]
     or summary["not_yet_classified_unique"]
     or report["classified_sheets"]!=report["total_sheets"]):
  raise ValueError("Reconstructed visual classification is incomplete")
 if (summary["total_original_textures"]!=1405 or summary["prepared_unique_composites"]!=1108
     or summary["automatic_no_japanese_skip_unique"]!=159
     or summary["needs_edit_unique"]!=937
     or summary["uncertain_unique"]!=12
     or summary["unable_to_reconstruct_unique"]!=2):
  raise ValueError("Frozen batch baseline changed: re-review skip counts before execution")
 allrows={r["id"]:r for r in atlas.manifest().values()}
 eligible=[]
 for item in report["rows"]:
  if item["status"]!="needs_image_edit":continue
  tid=item["task_id"]
  meta=atlas.OUT/tid/"task.json"
  if not meta.is_file():raise ValueError("Prepared task missing: "+tid)
  doc=json.loads(meta.read_text(encoding="utf-8"))
  if (doc["task_id"]!=tid or doc["texture_id"] not in allrows or
      doc["prepared_sha256"]!=item["source_sha256"] or
      doc["source_sha256"]!=item["raw_sha256"] or
      sorted(item["ids"])!=sorted(r["id"] for r in allrows.values() if r["original_sha256"]==item["raw_sha256"])):
   raise ValueError("Source-bound task identity mismatch: "+tid)
  if not (WORK/doc["prepared_image"]).is_file() or vision.sha(WORK/doc["prepared_image"])!=doc["prepared_sha256"]:
   raise ValueError("Source-bound composite changed: "+tid)
  eligible.append({"task_id":tid,"texture_id":doc["texture_id"],"source_sha256":doc["source_sha256"],
                   "composite_sha256":doc["prepared_sha256"],"source_ids":item["ids"]})
 if len(eligible)!=937 or sum(len(x["source_ids"]) for x in eligible)!=1228:
  raise ValueError("Eligible edit count/duplicate expansion changed")
 # Legacy raw texture image results are neither inputs nor completion markers.
 body="".join(json.dumps(x,ensure_ascii=False,sort_keys=True)+"\n" for x in eligible)
 if QUEUE.is_file() and QUEUE.read_text(encoding="utf-8")!=body:
  raise ValueError("Existing production queue changed; do not overwrite live or historical work")
 if not QUEUE.is_file():
  QUEUE.write_text(body,encoding="utf-8")
 return eligible
def progress_update(state:dict)->None:
 state["completed_total"]=state["already_generated"]+state["generated_this_run"]
 state["timestamp_local"]=time.strftime("%Y-%m-%d %H:%M:%S")
 vision.atomic(PROGRESS,state)
 print("PROGRESS",json.dumps({k:state[k] for k in ("status","total","already_generated","generated_this_run",
           "moderation_blocked_total","geometry_review","failed","last_task")},ensure_ascii=False),flush=True)
def main()->int:
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--max-items",type=int,default=0,help="0 means all eligible reconstructed pictures")
 ap.add_argument("--max-seconds",type=int,default=0,
                 help="Stop cleanly after this many seconds so the next run resumes (0 = no limit). "
                      "The image service allows one request per minute, so one batch outlives one CI job.")
 ap.add_argument("--quality",choices=["low","medium","high","xhigh","max","auto"],default=None,
                 help="Override image_provider.quality in config.json")
 ap.add_argument("--timeout",type=int,default=None,
                 help="Override image_provider.timeout_seconds in config.json")
 ap.add_argument("--retry-http",type=int,default=None,
                 help="Override retry.http in config.json")
 args=ap.parse_args()
 config=provider_config.load_config()
 if args.quality is None:args.quality=config["image_provider"]["quality"]
 if args.timeout is None:args.timeout=config["image_provider"]["timeout_seconds"]
 if args.retry_http is None:args.retry_http=config["retry"]["http"]
 if args.retry_http<0 or args.retry_http>2:raise ValueError("HTTP retries must be 0..2")
 tasks=prepare_queue()
 report=json.loads(SUMMARY.read_text(encoding="utf-8"))
 total=len(tasks)
 done=[t for t in tasks if (atlas.OUT/t["task_id"]/"restored-texture.png").is_file()
       and json.loads((atlas.OUT/t["task_id"]/"task.json").read_text(encoding="utf-8")).get("status")=="generated_unreviewed"]
 blocked=[t for t in tasks if blocked_record(t)]
 done_ids={t["task_id"] for t in done}
 blocked_ids={t["task_id"] for t in blocked}
 if done_ids & blocked_ids:
  raise ValueError("A task is both completed and moderation-blocked")
 todo=[t for t in tasks if t["task_id"] not in done_ids|blocked_ids]
 if args.max_items>0:todo=todo[:args.max_items]
 state={"status":"running","total":total,"skip_no_japanese_unique":report["automatic_no_japanese_skip_unique"],
   "skip_no_japanese_originals":report["automatic_no_japanese_skip_objects"],"needs_edit_originals":1228,
   "uncertain_unique":12,"unmapped_originals":2,"already_generated":len(done),
   "selected_this_run":len(todo),"generated_this_run":0,"geometry_review":0,
   "failed":0,"moderation_blocked_total":len(blocked),"moderation_blocked_this_run":0,
   "last_task":None,"source_queue_sha256":hashlib.sha256(QUEUE.read_bytes()).hexdigest(),
   "model":config["image_provider"]["model"],
   "image_provider_mode":config["image_provider"]["mode"],
   "user_review_required":False,
   "review_mode":"automatic_no_human_signoff",
   "min_request_interval_seconds":config["image_provider"].get("min_request_interval_seconds",60)}
 progress_update(state)
 started=time.monotonic()
 for index,job in enumerate(todo,1):
  if args.max_seconds>0 and time.monotonic()-started>=args.max_seconds:
   state["status"]="time_budget_exhausted"
   state["stopped_after_seconds"]=int(time.monotonic()-started)
   progress_update(state)
   print("TIME_BUDGET",state["stopped_after_seconds"],"handled_this_run",index-1,flush=True)
   return 0
  tid=job["task_id"];state["last_task"]=tid
  folder=atlas.OUT/tid
  result=None
  for attempt in range(1,args.retry_http+2):
   try:
    result=editor.edit(tid,args.quality,args.timeout)
    break
   except urllib.error.HTTPError as exc:
    detail=exc.read(8192).decode("utf-8","replace")
    reason=moderation_reason(exc.code,detail)
    if reason:
     record_moderation(job,exc.code,detail)
     state["moderation_blocked_total"]+=1
     state["moderation_blocked_this_run"]+=1
     state["last_moderation_blocked_task"]=tid
     print("MODERATION_BLOCKED_MANUAL_REVIEW",tid,reason["moderation_stage"],
           ",".join(reason["categories"]),flush=True)
     result={"status":"moderation_blocked_manual_review"}
     break
    state["last_http_error"]={"code":exc.code,"task_id":tid,"detail":detail[:500]}
    # Retry only transient transport/server failures, never moderation-blocked requests.
    if exc.code in {408,500,502,503,504} and attempt<=args.retry_http:
     print("RETRY_HTTP",tid,exc.code,attempt,flush=True)
     time.sleep(12*attempt)
     continue
    state["status"]="stopped_proxy_http_error"
    progress_update(state)
    print("PROXY_FAIL",tid,exc.code,detail[:450],flush=True)
    return 2
   except (ValueError,RuntimeError,TimeoutError,OSError) as exc:
    state["failed"]+=1
    vision.atomic(folder/"production-error.json",{
       "task_id":tid,"status":"needs_manual_review","error":type(exc).__name__+": "+str(exc)[:600]})
    print("TASK_ERROR",tid,type(exc).__name__,str(exc)[:150],flush=True)
    result={"status":"needs_manual_review"}
    break
  if result and result["status"]=="generated_unreviewed":
   state["generated_this_run"]+=1
   # Show fresh source-bound image instead of a previously edited whole atlas.
   choice=WORK/"internal-composites"/"review-selection.json"
   previous=json.loads(choice.read_text(encoding="utf-8")) if choice.is_file() else {}
   previous[job["texture_id"]]=tid
   vision.atomic(choice,previous)
  elif result and result["status"]=="needs_geometry_review":state["geometry_review"]+=1
  progress_update(state)
  if result and result["status"]=="generated_unreviewed":
   # index.html is a static offline snapshot. Rebuild it after each successful
   # new output so the browser's Refresh button/F5 sees current results.
   try:
    import build_mltd_image25_review_gallery as gallery
    gallery.main([])
   except Exception as exc:
    vision.atomic(PRE/"gallery-refresh-error.json",{
      "task_id":tid,"error":type(exc).__name__+": "+str(exc)[:600],
      "note":"Image output is retained; gallery refresh failed independently."})
    print("GALLERY_REFRESH_ERROR",tid,type(exc).__name__,str(exc)[:150],flush=True)
 state["status"]="completed_run" if args.max_items==0 else "completed_bounded_run"
 progress_update(state)
 return 0
if __name__=="__main__":raise SystemExit(main())
