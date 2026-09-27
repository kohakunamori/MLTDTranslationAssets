#!/usr/bin/env python3
"""Extract every audited pure-Texture2D MLTD bundle into a source-bound review workset.

Only reads immutable asset data. Writes originals and a resumable manifest under
work/image-localization-25; does not modify game archives, live MT, or devices.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import re
import sqlite3
import urllib.request
from pathlib import Path
from collections import Counter

ROOT=Path(__file__).resolve().parents[2]
INVENTORY=ROOT/"build/localization-90200/nongtx-uncovered-object-classification.json"
REL_DB=ROOT/"build/current-unity-assets.sqlite"
CACHE=ROOT/"work/local-assets/jp-android"
DEST=ROOT/"work/image-localization-25"
CDN="https://td-assets.bn765.com/1077100/production/2018/Android/"
SAFE=re.compile(r"[^a-zA-Z0-9._-]")
def sha_file(p:Path)->str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024), b""):h.update(chunk)
    return h.hexdigest()
def label(logical:str)->str:
    return logical.removesuffix(".unity3d")
def safe(value:str)->str:
    return SAFE.sub("_",value).strip("._")[:80] or "unnamed"
def source_records()->list[dict]:
    data=json.loads(INVENTORY.read_text(encoding="utf-8"))
    rows=data["rows"]
    if data["candidate_count"]!=232 or len(rows)!=232:
        raise ValueError("Inventory changed: require reviewed 232-bundle authority")
    con=sqlite3.connect(f"file:{REL_DB.as_posix()}?mode=ro",uri=True)
    for row in rows:
        extra=con.execute("SELECT archive_sha256,archive_size FROM source_asset WHERE logical_name=?",(row["logical"],)).fetchone()
        if not extra:raise ValueError("Missing source_asset provenance: "+row["logical"])
        row["archive_sha256"],row["archive_size"]=extra
    con.close()
    return rows
def get_bundle(row:dict,dest:Path)->Path:
    remote=row["remote"]
    expect_bytes=row["size"];expect_hash=row["archive_sha256"]
    candidates=[CACHE/remote,DEST/"bundles"/remote]
    for p in candidates:
        if p.is_file() and p.stat().st_size==expect_bytes and sha_file(p).lower()==expect_hash.lower():
            return p
    p=candidates[1];p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+".partial")
    try:
        with urllib.request.urlopen(CDN+remote,timeout=90) as resp,tmp.open("wb") as f:
            while chunk:=resp.read(1024*1024):f.write(chunk)
        if tmp.stat().st_size!=expect_bytes or sha_file(tmp).lower()!=expect_hash.lower():
            raise ValueError("download length/SHA mismatch")
        tmp.replace(p)
        return p
    finally:
        if tmp.exists():tmp.unlink()
def extract(row:dict)->tuple[list[dict],dict|None]:
    try:
        import UnityPy
        p=get_bundle(row,DEST)
        env=UnityPy.load(str(p))
        sprites=[]
        for ob in env.objects:
            if ob.type.name=="Sprite":
                try:
                    data=ob.read()
                    sprites.append({"name":str(data.m_Name),"path_id":int(ob.path_id)})
                except Exception:pass
        textures=[]
        for ob in env.objects:
            if ob.type.name!="Texture2D":continue
            try:
                obj=ob.read();im=obj.image
                if im is None:raise ValueError("Texture2D image missing")
                name=f'{int(ob.path_id)}_{safe(str(obj.m_Name))}.png'
                path=DEST/"original"/label(row["logical"])/name
                path.parent.mkdir(parents=True,exist_ok=True)
                if not path.is_file():im.save(path)
                fmt=im.mode;w,h=im.size
                source_sha=sha_file(path)
                rel=path.relative_to(DEST).as_posix()
                edited=f'edited/{label(row["logical"])}/{name}'
                preview=f'review/{label(row["logical"])}/{name}'
                textures.append({
                    "id":f'{label(row["logical"])}:{int(ob.path_id)}',
                    "bundle":row["logical"],"remote":row["remote"],"archive_sha256":row["archive_sha256"],
                    "type":"Texture2D","texture_name":str(obj.m_Name),"texture_path_id":int(ob.path_id),
                    "original":rel,"original_sha256":source_sha,"original_size":[w,h],"original_mode":fmt,
                    "edited":edited,"review":preview,"sprite_members":sprites,
                    "review_status":"unreviewed",
                })
            except Exception as ex:
                raise RuntimeError(f"{row['logical']} Texture2D path_id={ob.path_id}: {ex}") from ex
        if not textures:raise RuntimeError("No exportable Texture2D objects")
        return textures,None
    except Exception as ex:
        return [],{"bundle":row["logical"],"remote":row["remote"],"error":str(ex)}
def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--workers",type=int,default=4)
    args=ap.parse_args()
    if not 1<=args.workers<=8:raise ValueError("--workers must be 1..8")
    rows=source_records()
    DEST.mkdir(parents=True,exist_ok=True)
    manifest=DEST/"manifest.jsonl"; summary=DEST/"prepare-summary.json"
    tasks=[];errors=[]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        future={ex.submit(extract,row):row for row in rows}
        for i,f in enumerate(as_completed(future),1):
            result,error=f.result()
            if error:errors.append(error);print("ERROR",error,flush=True)
            else:tasks.extend(result);print(f"[{i}/{len(rows)}] {future[f]['logical']} textures={len(result)}",flush=True)
    tasks.sort(key=lambda r:(r["bundle"],r["texture_name"],r["texture_path_id"]))
    with manifest.open("w",encoding="utf-8",newline="\n") as f:
        for t in tasks:f.write(json.dumps(t,ensure_ascii=False)+"\n")
    data={"inventory_bundles":len(rows),"prepared_bundles":len(set(t["bundle"] for t in tasks)),
          "texture_tasks":len(tasks),"failed_bundles":len(errors),
          "error_rows":errors,"manifest":str(manifest),"source":"nongtx-uncovered-object-classification.json",
          "note":"Texture tasks exceed bundle count; result remains unreviewed until human review."}
    summary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("FINAL",json.dumps({k:v for k,v in data.items() if k not in ("error_rows",)},ensure_ascii=False),flush=True)
    return 0 if not errors else 2
if __name__=="__main__":raise SystemExit(main())
