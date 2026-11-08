#!/usr/bin/env python3
"""Extract exact internal Sprite ROI geometry from the frozen Unity object index.

A source Texture2D with two Sprite objects named <texture>_00/_01 and pointing
back to the same texture gets a provenance-bound single-texture composition.
Never guess region rectangles from visual similarity, never join two textures.
"""
from __future__ import annotations
import argparse,json,sqlite3,zlib
from pathlib import Path
from typing import Any
import internal_texture_image25 as internal

ROOT=Path(__file__).resolve().parents[2]
DEFAULT_DB=ROOT/"build/current-unity-assets.sqlite"
def normalized(blob:bytes)->dict[str,Any]:
 if not blob:raise ValueError("Sprite normalized_json missing")
 return json.loads(zlib.decompress(blob).decode("utf-8"))
def sprite_pair_layout(texture_name, texture_path_id, size, sprites):
    """Recognize only the verified 512-square two-Sprite page atlas rule.

    Sprite rectangles prove the atlas ROIs, not arbitrary page adjacency.
    Rotation/order come from this explicit, versioned MLTD layout rule.
    Unknown atlases are not guessed; malformed matching pairs fail closed.
    """
    wanted = {texture_name + "_00", texture_name + "_01"}
    candidates = [(pid, data) for pid, data in sprites if data.get("m_Name") in wanted]
    if not candidates:
        return None
    if len(candidates) != 2 or {d["m_Name"] for _, d in candidates} != wanted:
        raise ValueError("Ambiguous or incomplete same-texture Sprite pair")
    if list(size) != [512, 512]:
        raise ValueError("Unsupported paired Sprite atlas dimensions")
    matched = {}
    for pid, data in candidates:
        rd = data.get("m_RD", {})
        ref = rd.get("texture", {})
        if ref.get("m_FileID") != 0 or ref.get("m_PathID") != texture_path_id:
            raise ValueError("Sprite pair references another texture")
        rect = data.get("m_Rect")
        if not isinstance(rect, dict) or rd.get("textureRect") != rect or rd.get("settingsRaw") != 64:
            raise ValueError("Unsupported Sprite packing geometry")
        vals = [rect.get(k) for k in ("x", "y", "width", "height")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v for v in vals):
            raise ValueError("Non-integer Sprite ROI")
        x, y, w, h = map(int, vals)
        matched[data["m_Name"]] = {"sprite_path_id": pid, "unity_rect": rect,
                                 "rect": [x, size[1]-y-h, w, h]}
    main, side = matched[texture_name+"_00"], matched[texture_name+"_01"]
    if main["rect"] != [1, 1, 510, 366] or side["rect"] != [1, 371, 366, 140]:
        raise ValueError("Unsupported Sprite pair layout; do not guess rotation")
    return {"algorithm": "mltd-sprite-page-510x366-ccw90-v1",
            "metadata_authority": "unity_sprite_texture_rect", "sprites": matched,
            "compose_direction": "horizontal",
            "regions": [{"order": 0, "rect": main["rect"], "transform": "identity"},
                        {"order": 1, "rect": side["rect"], "transform": "rotate_ccw90"}]}


def discover(texture_id:str,task_id:str,db:Path=DEFAULT_DB,force:bool=False)->dict:
 rows=internal.manifest()
 if texture_id not in rows:raise ValueError("Unknown source Texture2D ID")
 item=rows[texture_id]
 with sqlite3.connect(f"file:{db.as_posix()}?mode=ro",uri=True,timeout=2) as con:
  records=con.execute(
    "select path_id,object_name,normalized_json from unity_object "
    "indexed by idx_unity_object_key_nocase "
    "where logical_name=? and type_name='Sprite'",
    (item["bundle"],)).fetchall()
 target=item["texture_name"]
 matched={}
 w,h=item["original_size"]
 for pid,name,blob in records:
  if name not in (target+"_00",target+"_01"):continue
  info=normalized(blob)
  reference=info.get("m_RD",{}).get("texture",{})
  if int(reference.get("m_FileID",-1))!=0 or int(reference.get("m_PathID",0))!=int(item["texture_path_id"]):
   continue
  rect=info.get("m_Rect")
  if not isinstance(rect,dict):raise ValueError("Missing exact Unity Sprite rect")
  x,y,rw,rh=[rect[k] for k in ("x","y","width","height")]
  vals=(x,h-y-rh,rw,rh)
  if any(round(v)!=v for v in vals):raise ValueError("Non-integer Sprite ROI")
  rx,ry,bw,bh=map(int,vals)
  if name in matched:raise ValueError("Duplicate Sprite name")
  matched[name]={"rect":[rx,ry,bw,bh],
                 "sprite_path_id":pid,"unity_rect":rect}
 if set(matched)!={target+"_00",target+"_01"}:
  raise ValueError("A unique source-bound _00/_01 Sprite pair was not found")
 top,bottom=matched[target+"_00"],matched[target+"_01"]
 if top["rect"][1]>=bottom["rect"][1]:raise ValueError("Unexpected Sprite vertical order")
 # The narrow _01 Sprite becomes the right-hand continuation of _00
 # after rotating counterclockwise by 90 degrees (366x140 -> 140x366).
 # Use an explicit semantic name rather than ambiguous 'rotate270'.
 if top["rect"][3]!=bottom["rect"][2]:
  raise ValueError("Small Sprite rotated height does not match main Sprite")
 spec={"task_id":task_id,"texture_id":texture_id,"compose_direction":"horizontal",
       "regions":[{"rect":top["rect"],"order":0,"transform":"identity"},
                  {"rect":bottom["rect"],"order":1,"transform":"rotate_ccw90"}]}
 prepared=internal.prepare(spec,force=force)
 evidence={"schema_version":1,"texture_id":texture_id,
           "source_sha256":item["original_sha256"],"bundle":item["bundle"],
           "archive_sha256":item["archive_sha256"],"texture_path_id":item["texture_path_id"],
           "sprites":matched,"mapping":spec,"metadata_authority":"unity_sprite_normalized_json",
           "prepared_composite_sha256":prepared["prepared_sha256"]}
 internal.write(internal.OUT/task_id/"sprite-layout-evidence.json",evidence)
 return prepared
def main()->int:
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--texture-id",required=True)
 ap.add_argument("--task-id",required=True)
 ap.add_argument("--db",type=Path,default=DEFAULT_DB)
 ap.add_argument("--force",action="store_true")
 args=ap.parse_args()
 out=discover(args.texture_id,args.task_id,args.db,args.force)
 print(json.dumps({k:out[k] for k in ("task_id","texture_id","original_size",
               "prepared_size","prepared_image","region_map","status")},ensure_ascii=False,indent=2))
 return 0
if __name__=="__main__":raise SystemExit(main())
