#!/usr/bin/env python3
"""Reconstruct and localize regions INSIDE ONE MLTD Texture2D, then restore it.

One input texture -> regions -> one complete composition -> GPT Image (separate
runner) -> aspect-checked full-image resize -> inverse region transforms ->
paste into the original texture. No cross-texture pairing or Unity mutation.
"""
from __future__ import annotations
import argparse,hashlib,json,re
from pathlib import Path
from PIL import Image,ImageChops,ImageDraw

ROOT=Path(__file__).resolve().parents[2]
WORK=ROOT/"work/image-localization-25"
OUT=WORK/"internal-composites"
SAFE=re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
TRANSFORMS={
 "identity":None,"rotate90":Image.Transpose.ROTATE_270,
 "rotate180":Image.Transpose.ROTATE_180,"rotate270":Image.Transpose.ROTATE_90,
 "rotate_ccw90":Image.Transpose.ROTATE_90,"rotate_cw90":Image.Transpose.ROTATE_270,
 "flip_h":Image.Transpose.FLIP_LEFT_RIGHT,"flip_v":Image.Transpose.FLIP_TOP_BOTTOM}
INVERSE={"identity":"identity","rotate90":"rotate270","rotate270":"rotate90",
 "rotate_ccw90":"rotate_cw90","rotate_cw90":"rotate_ccw90",
 "rotate180":"rotate180","flip_h":"flip_h","flip_v":"flip_v"}

def sha(p:Path)->str:
 h=hashlib.sha256()
 with p.open("rb") as f:
  for block in iter(lambda:f.read(1024*1024),b""):h.update(block)
 return h.hexdigest()
def write(p:Path,obj:dict)->None:
 p.parent.mkdir(parents=True,exist_ok=True)
 tmp=p.with_suffix(p.suffix+".writing")
 tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
 tmp.replace(p)
def taskname(task_id:str)->str:
 if not isinstance(task_id,str) or not SAFE.fullmatch(task_id):
  raise ValueError("Invalid internal-composite task ID")
 return task_id
def manifest()->dict:
 p=WORK/"manifest.jsonl"
 return {r["id"]:r for line in p.read_text(encoding="utf-8").splitlines()
         if line.strip() and (r:=json.loads(line))}
def transform(im:Image.Image,method:str)->Image.Image:
 if method not in TRANSFORMS:raise ValueError("Invalid region transform: "+str(method))
 code=TRANSFORMS[method]
 return im.copy() if code is None else im.transpose(code)
def roi_rect(region:dict,size:tuple[int,int])->tuple[int,int,int,int]:
 rect=region.get("rect")
 if not isinstance(rect,list) or len(rect)!=4 or any(not isinstance(v,int) or isinstance(v,bool) for v in rect):
  raise ValueError("ROI rect must be [x,y,width,height] integers")
 x,y,w,h=rect
 if x<0 or y<0 or w<=0 or h<=0 or x+w>size[0] or y+h>size[1]:
  raise ValueError("ROI outside source texture")
 return (x,y,x+w,y+h)
def compose_regions(source, regions, direction="horizontal"):
    """Pure, reversible same-texture composition shared with incremental CI."""
    if direction not in {"vertical", "horizontal"}:
        raise ValueError("Invalid compose direction")
    if not isinstance(regions, list) or len(regions) < 2:
        raise ValueError("At least two same-texture regions required")
    ordered = sorted(regions, key=lambda r: r.get("order", -1))
    if [r.get("order") for r in ordered] != list(range(len(ordered))):
        raise ValueError("Region order must be 0..N-1 with no duplicates")
    rectangles, parts = [], []
    for item in ordered:
        rect = roi_rect(item, source.size)
        for old in rectangles:
            if not (rect[2] <= old[0] or rect[0] >= old[2]
                    or rect[3] <= old[1] or rect[1] >= old[3]):
                raise ValueError("Source region overlap")
        rectangles.append(rect)
        parts.append(transform(source.crop(rect), item.get("transform", "identity")))
    if direction == "horizontal":
        width, height = sum(p.width for p in parts), max(p.height for p in parts)
    else:
        width, height = max(p.width for p in parts), sum(p.height for p in parts)
    if width * height > 16_777_216:
        raise ValueError("Composite exceeds bounded image task size")
    canvas = Image.new(source.mode, (width, height), source.getpixel((source.width-1, source.height-1)))
    position, mapping = 0, []
    for item, part in zip(ordered, parts):
        x, y = (position, 0) if direction == "horizontal" else (0, position)
        canvas.paste(part, (x, y))
        mapping.append({"order": item["order"], "rect": item["rect"],
                        "transform": item.get("transform", "identity"),
                        "canvas_box": [x, y, x+part.width, y+part.height]})
        position += part.width if direction == "horizontal" else part.height
    return canvas, mapping


def restore_regions(original, candidate, mapping):
    """Invert an exact composition; preserve alpha and every non-ROI pixel."""
    target = original.copy()
    for region in mapping:
        box = region["canvas_box"]
        if not (0 <= box[0] < box[2] <= candidate.width
                and 0 <= box[1] < box[3] <= candidate.height):
            raise ValueError("Composite region outside candidate")
        part = transform(candidate.crop(box), INVERSE[region["transform"]])
        rect = roi_rect(region, original.size)
        if part.size != (rect[2]-rect[0], rect[3]-rect[1]):
            raise ValueError("Reverse-region geometry mismatch")
        patch = part.convert(original.mode)
        if "A" in original.getbands():
            patch.putalpha(original.crop(rect).getchannel("A"))
        target.paste(patch, rect[:2])
    return target


def prepare(task:dict,force:bool=False)->dict:
 tid=taskname(task["task_id"])
 row=manifest().get(task.get("texture_id"))
 if row is None:raise ValueError("Missing Texture2D source id")
 original=WORK/row["original"]
 if not original.is_file() or sha(original)!=row["original_sha256"]:
  raise ValueError("Original Texture2D SHA changed")
 regions=task.get("regions")
 if not isinstance(regions,list) or len(regions)<2:raise ValueError("At least two same-texture regions required")
 direction=task.get("compose_direction","vertical")
 if direction not in {"vertical","horizontal"}:raise ValueError("Invalid compose direction")
 sorted_regions=sorted(regions,key=lambda r:r.get("order",-1))
 if [r.get("order") for r in sorted_regions]!=list(range(len(regions))):
  raise ValueError("Region order must be 0..N-1 with no duplicates")
 folder=OUT/tid;metadata=folder/"task.json"
 if metadata.exists() and not force:
  old=json.loads(metadata.read_text(encoding="utf-8"))
  if old["texture_id"]!=row["id"] or old["source_sha256"]!=row["original_sha256"]:
   raise ValueError("Existing task ID already bound to another source")
  if old.get("regions")!=sorted_regions:raise ValueError("Existing task layout differs")
  prepared=WORK/old["prepared_image"]
  if prepared.is_file() and sha(prepared)==old["prepared_sha256"]:return old
 with Image.open(original) as source:
  source.load();size=source.size
  if source.mode not in {"RGB","RGBA"}:
   raise ValueError("Unsupported source texture mode; preserve native RGB/RGBA only")
  canvas,bound=compose_regions(source,sorted_regions,direction)
  width,height=canvas.size
  folder.mkdir(parents=True,exist_ok=True)
  composite=folder/"source-composite.png";tmp=composite.with_suffix(".png.writing")
  canvas.save(tmp,format="PNG");tmp.replace(composite)
  output={"schema_version":1,"task_id":tid,"texture_id":row["id"],"bundle":row["bundle"],
   "texture_path_id":row["texture_path_id"],"original":row["original"],
   "source_sha256":row["original_sha256"],"original_size":list(size),
   "original_mode":source.mode,"regions":sorted_regions,"compose_direction":direction,
   "region_map":bound,"prepared_image":composite.relative_to(WORK).as_posix(),
   "prepared_sha256":sha(composite),"prepared_size":[width,height],
   "status":"prepared_unreviewed"}
 write(metadata,output)
 return output

def restore(tid:str,model_image:Path,force:bool=False,aspect_tolerance:float=0.01)->dict:
 tid=taskname(tid);folder=OUT/tid;meta_path=folder/"task.json"
 info=json.loads(meta_path.read_text(encoding="utf-8"))
 source=WORK/info["original"];prepared=WORK/info["prepared_image"]
 if not source.is_file() or sha(source)!=info["source_sha256"]:raise ValueError("Source modified")
 if not prepared.is_file() or sha(prepared)!=info["prepared_sha256"]:raise ValueError("Prepared composite modified")
 if not model_image.is_file():raise ValueError("Model image missing")
 result=folder/"restored-texture.png"
 if result.exists() and not force:raise ValueError("Restored version already exists; use --force")
 expect=tuple(info["prepared_size"])
 with Image.open(model_image) as generated,Image.open(source) as original:
  generated.load();original.load()
  if generated.width<1 or generated.height<1:raise ValueError("Empty model image")
  # A 512x512 composite may be returned by GPT Image as 1254x1254.
  # Validate geometry using RATIO, not absolute pixel dimensions. No
  # guessing crop or stretch when the aspect ratio changed materially.
  ratio=(generated.width/generated.height)/(expect[0]/expect[1])
  if abs(ratio-1)>aspect_tolerance:
   write(folder/"geometry-warning.json",{
    "task_id":tid,"status":"needs_geometry_review","original_composite_size":list(expect),
    "model_output_size":list(generated.size),"aspect_ratio_error":abs(ratio-1),
    "model_sha256":sha(model_image),"model_path":str(model_image)})
   raise ValueError("Model changed composite aspect ratio; cannot safely restore")
  # Resize the WHOLE composite to its ORIGINAL exact canvas BEFORE splitting;
  # resizing each region independently would introduce seams/offset errors.
  resized=generated.convert("RGBA" if "A" in original.getbands() else "RGB").resize(
   expect,Image.Resampling.LANCZOS)
  target=restore_regions(original,resized,info["region_map"])
  if target.size!=original.size:raise ValueError("Result dimensions differ from original")
  if "A" in original.getbands():
   if ImageChops.difference(original.getchannel("A"),target.getchannel("A")).getbbox():
    raise ValueError("Source alpha was modified")
  # Non-ROI pixels must be exactly equal to the original (not merely similar).
  unchanged=Image.new("L",original.size,255)
  draw=ImageDraw.Draw(unchanged)
  for region in info["region_map"]:
   x,y,x2,y2=roi_rect(region,original.size)
   draw.rectangle((x,y,x2-1,y2-1),fill=0)
  # RGBA getbbox() can ignore an RGB-only difference when the diff alpha
  # channel is zero. Check every channel individually under the outside mask.
  for left,right in zip(original.convert("RGBA").split(),target.convert("RGBA").split()):
   changed=ImageChops.difference(left,right)
   exterior=Image.new("L",original.size,0)
   exterior.paste(changed,(0,0),unchanged)
   if exterior.getbbox():raise ValueError("Pixel changes outside ROI")
  result.parent.mkdir(parents=True,exist_ok=True)
  temp=result.with_suffix(".png.writing")
  target.save(temp,format="PNG")
  with Image.open(temp) as verified:verified.verify()
  temp.replace(result)
  info["model_image"]=str(model_image);info["model_sha256"]=sha(model_image)
  info["model_output_size"]=list(generated.size)
  info["restored_image"]=result.relative_to(WORK).as_posix()
  info["restored_sha256"]=sha(result)
  info["status"]="generated_unreviewed"
 write(meta_path,info)
 return info
def main()->int:
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--task-id",required=True)
 ap.add_argument("--texture-id")
 ap.add_argument("--regions-json",type=Path)
 ap.add_argument("--model-output",type=Path)
 ap.add_argument("--force",action="store_true")
 a=ap.parse_args()
 if a.model_output:
  doc=restore(a.task_id,a.model_output,a.force)
 elif a.texture_id and a.regions_json:
  spec=json.loads(a.regions_json.read_text(encoding="utf-8-sig"))
  doc=prepare({"task_id":a.task_id,"texture_id":a.texture_id,
                "regions":spec["regions"],
                "compose_direction":spec.get("compose_direction","vertical")},a.force)
 else:raise SystemExit("Prepare using --texture-id --regions-json or restore using --model-output")
 print(json.dumps({k:doc.get(k) for k in ("task_id","texture_id","prepared_size","original_size","status",
    "prepared_image","restored_image","model_output_size")},ensure_ascii=False,indent=2))
 return 0
if __name__=="__main__":raise SystemExit(main())
