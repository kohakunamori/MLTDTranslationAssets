#!/usr/bin/env python3
"""Edit a reconstructed single-texture picture, then restore its source atlas.

The custom CLI ProxyAPI produces a raw GPT Image 2.5 PNG at its chosen size.
The postprocessor checks aspect ratio, resizes the *complete* composition once,
undoes each ROI transform and pastes regions back into the original atlas.
All generated PNGs remain unreviewed. This never writes a Unity bundle.
"""
from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
from PIL import Image
import internal_texture_image25 as atlas
import run_mltd_image25_batch as provider

WORK=atlas.WORK
def edit(task_id:str,quality:str|None=None,timeout:int|None=None)->dict:
 tid=atlas.taskname(task_id)
 folder=atlas.OUT/tid
 task=json.loads((folder/"task.json").read_text(encoding="utf-8"))
 composite=WORK/task["prepared_image"]
 if not composite.is_file() or atlas.sha(composite)!=task["prepared_sha256"]:
  raise ValueError("Prepared composite source changed")
 raw=folder/"edited-composite-model.png"
 if not raw.is_file():
  row={"id":"internal:"+tid,"composite_mode":True,"original":task["prepared_image"],
       "original_sha256":task["prepared_sha256"],
       "original_size":task["prepared_size"],
       "edit_prompt":"This is a fully reconstructed picture from multiple subregions of ONE source Texture2D. "
        "Only translate player-visible Japanese into Simplified Chinese. Preserve its exact overall "
        "composition, relative panel positions, seamless joins, and horizontal/vertical aspect ratio. "
        "DO NOT output a rewritten atlas, separate tiles, or a decorated image; output this one complete "
        "picture with its layout unchanged. After generation the image will be normalized to its "
        "original pixel dimensions and split back at exact source coordinates."}
  payload,meta=provider.request_edit(row,quality,timeout)
  if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
   raise ValueError("Provider returned non-PNG")
  temp=raw.with_suffix(".png.writing")
  temp.write_bytes(payload)
  with Image.open(temp) as check:check.verify()
  temp.replace(raw)
  atlas.write(folder/"provider-output.json",{"task_id":tid,**meta,
       "raw_image":str(raw.relative_to(WORK).as_posix()),
       "raw_sha256":atlas.sha(raw),"status":"generated_unreviewed"})
 try:
  output=atlas.restore(tid,raw,force=True)
 except ValueError as ex:
  # The raw GPT Image output and source mapping are intentionally preserved
  # for retry/manual review; no fake source-sized texture is produced.
  atlas.write(folder/"postprocess-status.json",{"task_id":tid,
    "status":"needs_geometry_review","error":str(ex),
    "raw_model":str(raw.relative_to(WORK).as_posix())})
  return {"task_id":tid,"status":"needs_geometry_review","error":str(ex),
          "raw_model":str(raw.relative_to(WORK).as_posix())}
 atlas.write(folder/"postprocess-status.json",{"task_id":tid,
   "status":"generated_unreviewed","model_output_size":output["model_output_size"],
   "source_size":output["original_size"],
   "restored_image":output["restored_image"]})
 return {"task_id":tid,"status":"generated_unreviewed",
         "raw_model":str(raw.relative_to(WORK).as_posix()),
         "restored_image":output["restored_image"]}
def main()->int:
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--task-id",required=True)
 ap.add_argument("--quality",choices=["low","medium","high","xhigh","max","auto"],default=None)
 ap.add_argument("--timeout",type=int,default=None)
 args=ap.parse_args()
 result=edit(args.task_id,args.quality,args.timeout)
 print(json.dumps(result,ensure_ascii=False,indent=2))
 return 0 if result["status"]=="generated_unreviewed" else 2
if __name__=="__main__":raise SystemExit(main())
