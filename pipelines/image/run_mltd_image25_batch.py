#!/usr/bin/env python3
"""Resume-safe GPT Image 2.5 edit queue over the user's local CLI ProxyAPI.

Each source-bound Texture2D is an independent image task. Outputs never alter
originals or game archives. Human approval is a separate step.
"""
from __future__ import annotations
import argparse,base64,hashlib,json,shutil,time,urllib.error,urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from threading import Event,Lock
from PIL import Image
import provider_config
ROOT=Path(__file__).resolve().parents[2]
WORK=ROOT/"work/image-localization-25"
MANIFEST=WORK/"manifest.jsonl"
BASE="http://127.0.0.1:15721/v1/responses"
TOOL_MODEL="gpt-image-2.5-sunburst"
CONTROLLER="gpt-5.6-luna"
PROMPT=(
  "Edit the attached original MLTD game UI texture/atlas into Simplified Chinese. "
  "Replace ALL visible Japanese player-facing words and sentences with fluent Mainland Chinese, "
  "accurate to the original Japanese meaning. Keep character and event names consistent when readable; "
  "do not invent lore or translate English brand names. "
  "Preserve the original illustration, character art, numbers, icons, palette, layout, typography hierarchy, "
  "sprite boundaries and transparency. Do not add, crop or rearrange UI elements. "
  "Translate only text; keep all non-text content as close to pixel-identical as possible. "
  "For multiple panel cells in an atlas, keep each caption in its original cell, do not merge panels. "
  "Produce the translated EDITED IMAGE using the image_generation tool, not a written explanation."
)
COMPOSITE_PROMPT=(
  "Edit the ATTACHED COMPLETE reconstructed MLTD UI picture into Simplified Chinese. "
  "The attached image has already been assembled from multiple Sprite regions within one original texture; "
  "treat it as ONE continuous image, not an atlas or two independent panels. "
  "Replace all visible Japanese player-facing text with accurate, natural Mainland Chinese. "
  "Keep readable proper names, numbers and English branding stable; do not invent new text or graphics. "
  "Preserve character art, illustration, all non-text elements, their relative positions, and the exact picture "
  "composition and aspect ratio. Do not add borders or crop the picture. "
  "Produce an edited IMAGE using image_generation, not a text explanation."
)
def jsonwrite(path:Path,obj:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".writing")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(path)
def metric_path(row:dict)->Path:
    return WORK/"metrics"/Path(row["edited"]).with_suffix(".json")
def request_edit(row:dict,quality:str|None=None,timeout:int|None=None)->tuple[bytes,dict]:
    source=WORK/row["original"]
    if not source.is_file():raise ValueError("missing original")
    raw=source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=row["original_sha256"]:
        raise ValueError("original hash has changed")
    config=provider_config.load_config()
    prompt=(COMPOSITE_PROMPT if row.get("composite_mode") else PROMPT)+" "+row.get("edit_prompt","")
    # Native /v1/images/edits uses the configured Sunburst OAuth backend
    # directly; legacy_responses is opt-in for older proxy deployments.
    return provider_config.image_edit(config,raw,prompt,quality,timeout)

def process(row:dict,quality:str,timeout:int,stop:Event,lock:Lock)->dict:
    dest=WORK/row["edited"];mp=metric_path(row)
    if dest.is_file() and dest.stat().st_size>0:
        return {"id":row["id"],"status":"already_generated"}
    if stop.is_set():return {"id":row["id"],"status":"deferred"}
    t0=time.perf_counter()
    attempt=0
    while attempt<4 and not stop.is_set():
        attempt+=1
        try:
            raw,provider=request_edit(row,quality,timeout)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Provider output is not PNG")
            dest.parent.mkdir(parents=True,exist_ok=True)
            tmp=dest.with_suffix(".png.writing")
            tmp.write_bytes(raw)
            with Image.open(tmp) as im:
                im.verify()
            tmp.replace(dest)
            with Image.open(dest) as im:
                size=list(im.size);mode=im.mode
            meta={"id":row["id"],"status":"generated_unreviewed",
                  "original":row["original"],"edited":row["edited"],"generated_size":size,
                  "original_size":row["original_size"],"generated_mode":mode,"original_mode":row["original_mode"],
                  "size_changed":size!=row["original_size"],
                  "alpha_changed":("A" in mode)!=("A" in row["original_mode"]),
                  "elapsed_seconds":round(time.perf_counter()-t0,3),"attempts":attempt,**provider}
            jsonwrite(mp,meta)
            return {"id":row["id"],"status":"generated_unreviewed","size_changed":meta["size_changed"],"time":meta["elapsed_seconds"]}
        except urllib.error.HTTPError as e:
            detail=e.read(1400).decode("utf-8","replace")
            if e.code in (401,403,404):
                stop.set()
                err=f"fatal HTTP {e.code}: {detail[:400]}"
                break
            if e.code==429 or 500<=e.code<600:
                err=f"HTTP {e.code}: {detail[:400]}"
                time.sleep(min(20*2**(attempt-1),100))
                continue
            err=f"HTTP {e.code}: {detail[:400]}"
            break
        except Exception as e:
            err=type(e).__name__+": "+str(e)[:400]
            if isinstance(e,(TimeoutError,ConnectionError)) and attempt<4:
                time.sleep(15*attempt);continue
            break
    else:err="stopped after repeated provider failures"
    meta={"id":row["id"],"status":"failed_retryable" if not stop.is_set() else "blocked_provider",
          "attempts":attempt,"error":err,"elapsed_seconds":round(time.perf_counter()-t0,3),
          "requested_image_model":TOOL_MODEL,"original":row["original"],"edited":row["edited"]}
    jsonwrite(mp,meta)
    return {"id":row["id"],"status":meta["status"],"error":err}
def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--workers",type=int,default=1)
    ap.add_argument("--max-items",type=int,default=0,help="0 means all preprocessed unique Japanese-text candidates")
    ap.add_argument("--quality",default="high",choices=["medium","high","xhigh","max"])
    ap.add_argument("--timeout",type=int,default=240)
    args=ap.parse_args()
    if not 1<=args.workers<=2:raise ValueError("Use at most 2 workers while main translation runs")
    rows=[json.loads(line) for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:raise ValueError("Empty workset")
    # A previous run sent every extracted Texture2D to GPT Image, including
    # illustration-only artwork. New production must use the complete,
    # visually classified and source-bound preprocess report by default.
    report_path=WORK/"preprocess"/"preprocess-report.json"
    if not report_path.is_file():
        raise ValueError("Preprocess first: python scripts/preprocess_mltd_image25.py --classify")
    report=json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("classified_sheets")!=report.get("total_sheets") or report.get("counts",{}).get("pending_visual_classification",0):
        raise ValueError("Preprocess incomplete: image batch must not process unclassified textures")
    by_id={r["id"]:r for r in rows}
    selected=[]
    composite_count=0
    for candidate in report.get("rows",[]):
        if candidate["status"]!="needs_image_edit":continue
        if candidate.get("composite_pair_id"):
            composite_count+=1
            continue  # run_mltd_image25_stitched.py handles whole-image composites
        id_=candidate["representative_id"]
        if id_ not in by_id or by_id[id_]["original_sha256"]!=candidate["source_sha256"]:
            raise ValueError("Preprocessed source identity no longer matches: "+id_)
        selected.append(by_id[id_])
    pending=[r for r in selected if not (WORK/r["edited"]).is_file()]
    if args.max_items>0:pending=pending[:args.max_items]
    stop=Event();lock=Lock()
    summary={"inventory_tasks":len(rows),"eligible_unique_image_tasks":len(selected),
             "composite_jobs_separate":composite_count,
             "already_generated_in_selected":len(selected)-sum(not (WORK/r["edited"]).is_file() for r in selected),
             "submitted_this_run":len(pending),"generated_this_run":0,"failed_this_run":0,
             "deferred_this_run":0,"model":TOOL_MODEL,"status":"running"}
    jsonwrite(WORK/"batch-progress.json",summary)
    print("BEGIN",json.dumps(summary,ensure_ascii=False),flush=True)
    # Workers are bounded to avoid disrupting the user's live translation pool.
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        jobs=[ex.submit(process,r,args.quality,args.timeout,stop,lock) for r in pending]
        for i,future in enumerate(as_completed(jobs),1):
            item=future.result()
            status=item["status"]
            if status=="generated_unreviewed":summary["generated_this_run"]+=1
            elif status in ("failed_retryable","blocked_provider"):summary["failed_this_run"]+=1
            else:summary["deferred_this_run"]+=1
            if stop.is_set():summary["status"]="blocked_provider"
            jsonwrite(WORK/"batch-progress.json",summary)
            print(f"[{i}/{len(pending)}]",json.dumps(item,ensure_ascii=False),flush=True)
    if not stop.is_set():summary["status"]="finished_attempts"
    jsonwrite(WORK/"batch-progress.json",summary)
    print("FINAL",json.dumps(summary,ensure_ascii=False),flush=True)
    return 0 if not stop.is_set() and summary["failed_this_run"]==0 else 2
if __name__=="__main__":raise SystemExit(main())
