#!/usr/bin/env python3
"""Pre-filter original MLTD textures before expensive GPT Image editing.

- Collapse exact SHA-256 duplicate pixels, preserving every source locator.
- Compile contact sheets and classify Japanese visible copy using the existing
  local CLI ProxyAPI *text/vision* model, not GPT Image.
- High-confidence no-Japanese candidates are excluded from image editing; any
  uncertain or malformed classification stays in a manual-review queue.
- This command does NOT start image edits or change archived Unity assets.
"""
from __future__ import annotations
import argparse,base64,hashlib,json,re,time,urllib.error,urllib.request
from collections import Counter,defaultdict
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont,ImageOps
import provider_config

ROOT=Path(__file__).resolve().parents[2]
WORK=ROOT/"work/image-localization-25"
PRE=WORK/"preprocess"
API="http://127.0.0.1:15721/v1/responses"
VISION="gpt-5.6-luna"
LABELS={"jp_text","no_jp_text","uncertain"}
RULES=(
    "You are classifying a numbered 4x4 contact sheet of original MLTD game texture atlases. "
    "The goal is to avoid unnecessary IMAGE EDITING, not to translate. Each tile shows ONE texture, "
    "numbered 00-15 in a black label above the original pixels. Classify EACH numbered tile. "
    "jp_text = any clearly visible Japanese player-facing copy, including kana, Japanese kanji "
    "phrases, vertical text, tiny labels, event logos containing Japanese, or translated-needed captions. "
    "no_jp_text = DEFINITELY no Japanese text: art/photos, icons, blank panels, English-only words, or "
    "decorative geometric backgrounds. "
    "uncertain = too small, partially obscured, stylized text, Chinese-vs-Japanese ambiguous, "
    "unreadable typography, or possible Japanese embedded in visual art. "
    "Be CONSERVATIVE: if unsure, choose uncertain, never no_jp_text. "
    "Respond as JSON object only with key 'tiles' containing objects "
    "{'index':0,'label':'jp_text|no_jp_text|uncertain','confidence':'high|medium|low','evidence':'short reason'}. "
    "Return one record for every numbered tile, no missing/extra indices."
)
def atomic(path:Path,obj:object)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".writing")
    temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    temp.replace(path)
def sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()
def font(n:int):
    for name in ["C:/Windows/Fonts/consola.ttf","C:/Windows/Fonts/arial.ttf"]:
        try:return ImageFont.truetype(name,n)
        except OSError:pass
    return ImageFont.load_default()
def inventory()->list[dict]:
    rows=[json.loads(s) for s in (WORK/"manifest.jsonl").read_text(encoding="utf-8").splitlines() if s.strip()]
    by_id={r["id"]:r for r in rows}
    by_hash=defaultdict(list)
    for row in rows:
        original=WORK/row["original"]
        if not original.is_file() or sha(original)!=row["original_sha256"]:
            raise ValueError("Source missing or changed: "+row["id"])
        by_hash[row["original_sha256"]].append(row)

    # Only explicitly prepared source-bound pair.json files may override the
    # independent texture plan. Neighbouring info_00/info_01 files are NOT
    # automatically assumed to be the two halves of one continuous picture.
    composite_items=[]
    paired_ids=set()
    for pairfile in sorted((WORK/"composites").glob("*/pair.json")):
        info=json.loads(pairfile.read_text(encoding="utf-8"))
        if info.get("status") not in {"prepared_unreviewed","generated_unreviewed"}:continue
        pieces=info.get("pieces",[])
        if len(pieces)!=2:raise ValueError("Bad paired layout: "+str(pairfile))
        members=[]
        for part in pieces:
            id_=part["id"]
            if id_ not in by_id or id_ in paired_ids:
                raise ValueError("Missing or multiply-paired Texture2D: "+id_)
            original=by_id[id_]
            if original["original_sha256"]!=part["original_sha256"]:
                raise ValueError("Paired source hash changed: "+id_)
            members.append(id_);paired_ids.add(id_)
        stitched=WORK/info["source_composite"]
        if not stitched.is_file() or sha(stitched)!=info["source_composite_sha256"]:
            raise ValueError("Paired composite missing or modified: "+str(pairfile))
        composite_items.append({
            "source_sha256":info["source_composite_sha256"],
            "representative_id":"composite:"+info["pair_id"],
            "original":info["source_composite"],"size":info["size"],
            "ids":members,"edited_members":members if info["status"]=="generated_unreviewed" else [],
            "composite_pair_id":info["pair_id"],
            "composite_metadata":pairfile.relative_to(WORK).as_posix()})
    uniq=[]
    for h,sources in sorted(by_hash.items(),key=lambda pair:min(r["id"] for r in pair[1])):
        remaining=[r for r in sources if r["id"] not in paired_ids]
        if not remaining:continue
        source=next((r for r in remaining if (WORK/r["edited"]).is_file()),remaining[0])
        uniq.append({"source_sha256":h,"representative_id":source["id"],
                     "original":source["original"],"size":source["original_size"],
                     "ids":[r["id"] for r in remaining],
                     "edited_members":[r["id"] for r in remaining if (WORK/r["edited"]).is_file()]})
    return sorted(uniq+composite_items,key=lambda r:r["representative_id"])
def sheet_path(index:int)->Path:return PRE/"sheets"/f"sheet-{index:04d}.jpg"
def create_sheets(uniq:list[dict],side:int=460,cols:int=4)->list[dict]:
    groups=[]
    for i in range(0,len(uniq),cols*cols):
        subset=uniq[i:i+cols*cols];sheet_id=i//(cols*cols)
        path=sheet_path(sheet_id)
        canvas=Image.new("RGB",(cols*(side+8),cols*(side+35)),(22,27,37))
        draw=ImageDraw.Draw(canvas)
        for local,row in enumerate(subset):
            im=Image.open(WORK/row["original"])
            with im:
                render=ImageOps.contain(im.convert("RGBA"),(side,side),Image.Resampling.LANCZOS)
            x=local%cols*(side+8)+4;y=local//cols*(side+35)+29
            canvas.paste(render.convert("RGB"),(x,y))
            draw.rectangle((x-2,y-26,x+side+2,y-1),fill=(4,10,19))
            draw.text((x+5,y-26),f"{local:02d}  {row['representative_id'][:26]}",font=font(17),fill="white")
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.is_file():canvas.save(path,format="JPEG",quality=88,subsampling=0)
        groups.append({"sheet":sheet_id,"image":path.relative_to(WORK).as_posix(),
                       "items":[{"index":j,"source_sha256":item["source_sha256"],
                                 "id":item["representative_id"]} for j,item in enumerate(subset)]})
    return groups
def response_text(result:dict)->str:
    if isinstance(result.get("output_text"),str) and result["output_text"].strip():
        return result["output_text"]
    for msg in result.get("output",[]):
        for c in msg.get("content",[]):
            if c.get("type")=="output_text" and c.get("text"):
                return str(c["text"])
    raise RuntimeError("No output_text: "+str([x.get("type") for x in result.get("output",[])]))
def classify(group:dict,timeout:int)->dict:
    source=WORK/group["image"]
    img="data:image/jpeg;base64,"+base64.b64encode(source.read_bytes()).decode("ascii")
    cfg=provider_config.load_config()
    full_prompt=RULES+" IMPORTANT: THIS particular sheet has EXACTLY "+str(len(group["items"]))+" populated tiles, numbered 00 through "+str(len(group["items"])-1).zfill(2)+". Return exactly "+str(len(group["items"]))+" records, no others."
    result=provider_config.vision_response(cfg,img,full_prompt,timeout)
    answer=response_text(result).strip()
    if answer.startswith("```"):answer=re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$","",answer,flags=re.I)
    doc=json.loads(answer)
    tiles=doc.get("tiles",[])
    if not isinstance(tiles,list) or len(tiles)!=len(group["items"]):
        raise ValueError(f"Sheet {group['sheet']}: wrong tiles count {len(tiles) if isinstance(tiles,list) else 'not list'}")
    ids=[t.get("index") for t in tiles]
    if sorted(ids)!=list(range(len(group["items"]))):raise ValueError(f"Sheet {group['sheet']}: index mismatch")
    for t in tiles:
        if t.get("label") not in LABELS or t.get("confidence") not in {"high","medium","low"}:
            raise ValueError(f"Sheet {group['sheet']}: bad classification {t!r}")
    return {"sheet":group["sheet"],"model":cfg["vision"]["model"],
            "image_sha256":sha(source),"labels":sorted(tiles,key=lambda t:t["index"]),
            "response_id":result.get("id"),"timestamp":time.strftime("%Y-%m-%d %H:%M:%S")}
def collect(uniq:list[dict],groups:list[dict])->dict:
    labels={}
    for group in groups:
        p=PRE/"classifications"/f"sheet-{group['sheet']:04d}.json"
        if not p.is_file():continue
        doc=json.loads(p.read_text(encoding="utf-8"))
        if doc.get("image_sha256")!=sha(WORK/group["image"]):continue
        for tile in doc["labels"]:
            pos=int(tile["index"])
            h=group["items"][pos]["source_sha256"]
            labels[h]=tile
    counts=Counter()
    output=[]
    for item in uniq:
        result=labels.get(item["source_sha256"])
        if item.get("composite_pair_id"):
            # A pair is prepared only after its continuous-picture relationship
            # was explicitly confirmed; do not shrink it into a 4x4 text gate.
            state="needs_image_edit"
            result={"label":"jp_text","confidence":"user_confirmed",
                    "evidence":"Explicitly source-bound top/bottom composite"}
        elif not result:state="pending_visual_classification"
        elif result["label"]=="jp_text":state="needs_image_edit"
        elif result["label"]=="no_jp_text" and result["confidence"]=="high":
            state="skip_no_japanese_high_confidence"
        else:state="manual_review"
        # Preserve already-created images as reviewable evidence; never delete them.
        row={**item,"status":state,"classification":result or None}
        output.append(row);counts[state]+=1
    report={"schema_version":2,"total_texture_objects":sum(len(x["ids"]) for x in uniq),
            "unique_original_images":len(uniq),
            "approved_composites":sum(bool(x.get("composite_pair_id")) for x in uniq),
            "exact_duplicate_tasks":sum(len(x["ids"])-1 for x in uniq),
            "classified_sheets":sum((PRE/"classifications"/f"sheet-{x['sheet']:04d}.json").exists() for x in groups),
            "total_sheets":len(groups),"counts":dict(counts),
            "policy":"No Japanese high confidence: excluded from GPT Image, retained for audit; uncertain stays review.",
            "rows":output}
    atomic(PRE/"preprocess-report.json",report)
    q=PRE/"image-edit-candidates.jsonl"
    with q.open("w",encoding="utf-8",newline="\n") as f:
        for row in output:
            if row["status"]=="needs_image_edit":
                f.write(json.dumps(row,ensure_ascii=False)+"\n")
    atomic(PRE/"preprocess-summary.json",{k:v for k,v in report.items() if k!="rows"})
    return report
def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--classify",action="store_true",help="Use local vision API for pending sheets")
    ap.add_argument("--max-sheets",type=int,default=0,help="0 means classify every pending sheet")
    ap.add_argument("--timeout",type=int,default=180)
    args=ap.parse_args()
    uniq=inventory();groups=create_sheets(uniq)
    atomic(PRE/"sheets-manifest.json",groups)
    print("PREPARED",json.dumps({"all_textures":sum(len(x["ids"]) for x in uniq),
                                  "unique_images":len(uniq),"contact_sheets":len(groups),
                                  "approved_composites":sum(bool(x.get("composite_pair_id")) for x in uniq)}),flush=True)
    if args.classify:
        pending=[g for g in groups if not (PRE/"classifications"/f"sheet-{g['sheet']:04d}.json").exists()]
        if args.max_sheets>0:pending=pending[:args.max_sheets]
        for i,g in enumerate(pending,1):
            try:
                data=classify(g,args.timeout)
                atomic(PRE/"classifications"/f"sheet-{g['sheet']:04d}.json",data)
                print("CLASSIFIED",i,len(pending),"sheet",g["sheet"],
                      dict(Counter(x["label"] for x in data["labels"])),flush=True)
            except Exception as ex:
                atomic(PRE/"failures"/f"sheet-{g['sheet']:04d}.json",
                       {"sheet":g["sheet"],"error":type(ex).__name__+": "+str(ex)[:2000]})
                print("CLASSIFICATION_ERROR",g["sheet"],type(ex).__name__,str(ex)[:300],flush=True)
                break  # fail closed; don't flood provider
    report=collect(uniq,groups)
    print("FINAL",json.dumps({k:v for k,v in report.items() if k!="rows"},ensure_ascii=False),flush=True)
    return 0
if __name__=="__main__":raise SystemExit(main())
