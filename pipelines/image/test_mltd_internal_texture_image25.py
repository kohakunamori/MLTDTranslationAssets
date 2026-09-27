"""Aspect-safe single-texture internal-composition regression tests."""
from __future__ import annotations
import io,json,sys
from pathlib import Path
import pytest
from PIL import Image,ImageChops
sys.path.insert(0,str(Path(__file__).resolve().parent))
import internal_texture_image25 as unit
import run_mltd_internal_image25 as runner

@pytest.fixture
def source(tmp_path,monkeypatch):
 monkeypatch.setattr(unit,"WORK",tmp_path)
 monkeypatch.setattr(unit,"OUT",tmp_path/"internal-composites")
 original=tmp_path/"original"/"sample"/"texture.png"
 original.parent.mkdir(parents=True)
 image=Image.new("RGBA",(32,64),(7,17,27,255))
 for y in range(8,30):
  for x in range(5,21):
   image.putpixel((x,y),(55,80,120,150))
 for y in range(36,58):
  for x in range(5,21):
   image.putpixel((x,y),(65,90,130,170))
 image.save(original)
 row={"id":"sample:1","bundle":"sample.unity3d","texture_path_id":17,
      "original":original.relative_to(tmp_path).as_posix(),
      "original_sha256":unit.sha(original),"original_size":[32,64]}
 (tmp_path/"manifest.jsonl").write_text(json.dumps(row)+"\n",encoding="utf-8")
 regions=[{"rect":[5,8,16,22],"order":0},{"rect":[5,36,16,22],"order":1}]
 info=unit.prepare({"task_id":"single-texture-test","texture_id":row["id"],"regions":regions,
                    "compose_direction":"vertical"})
 return tmp_path,original,row,info

def test_model_output_different_dimensions_resized_as_one_image(source):
 root,src,row,info=source
 assert info["prepared_size"]==[16,44]
 # GPT Image routinely returns a different absolute resolution. The
 # composite aspect must be equal, but 16x44 -> 800x2200 is allowed.
 img=Image.new("RGBA",(800,2200),(210,20,45,255))
 model=root/"model.png";img.save(model)
 restored=unit.restore("single-texture-test",model)
 assert restored["model_output_size"]==[800,2200]
 assert restored["status"]=="generated_unreviewed"
 with Image.open(root/restored["restored_image"]) as actual,Image.open(src) as original:
  assert actual.size==original.size
  assert actual.getchannel("A").tobytes()==original.getchannel("A").tobytes()
  # The white-space/outside-ROI geometry is truly pixel-perfect.
  for y in list(range(0,8))+list(range(30,36))+list(range(58,64)):
   assert all(actual.getpixel((x,y))==original.getpixel((x,y)) for x in range(32))
  assert actual.getpixel((10,12))[:3]!=(55,80,120)

def test_aspect_mismatch_retains_raw_without_creating_fake_texture(source):
 root,src,row,info=source
 model=root/"square.png";Image.new("RGBA",(1254,1254),(255,0,0,255)).save(model)
 with pytest.raises(ValueError,match="aspect ratio"):
  unit.restore("single-texture-test",model)
 warning=json.loads((root/"internal-composites"/"single-texture-test"/"geometry-warning.json").read_text(encoding="utf-8"))
 assert warning["model_output_size"]==[1254,1254]
 assert warning["status"]=="needs_geometry_review"
 assert not (root/"internal-composites"/"single-texture-test"/"restored-texture.png").exists()
 assert model.is_file() and unit.sha(src)==row["original_sha256"]

def test_overlapping_roi_fails_closed(tmp_path,monkeypatch):
 monkeypatch.setattr(unit,"WORK",tmp_path)
 monkeypatch.setattr(unit,"OUT",tmp_path/"internal-composites")
 path=tmp_path/"original"/"a.png";path.parent.mkdir(parents=True)
 Image.new("RGB",(32,32),"white").save(path)
 row={"id":"a","bundle":"a.unity3d","texture_path_id":5,
      "original":"original/a.png","original_sha256":unit.sha(path),"original_size":[32,32]}
 (tmp_path/"manifest.jsonl").write_text(json.dumps(row),encoding="utf8")
 with pytest.raises(ValueError,match="overlap"):
  unit.prepare({"task_id":"bad","texture_id":"a",
   "regions":[{"rect":[0,0,20,20],"order":0},
              {"rect":[10,10,20,20],"order":1}]})

def test_runner_consumes_full_composite_once_and_preserves_model_png(source,monkeypatch):
 root,src,row,info=source
 monkeypatch.setattr(runner,"WORK",root)
 fake=Image.new("RGBA",(800,2200),(10,180,25,255))
 import io
 buffer=io.BytesIO();fake.save(buffer,format="PNG")
 calls=[]
 def request(r,quality,timeout):
  calls.append((r["original"],r["original_size"],r["edit_prompt"]))
  return buffer.getvalue(),{"response_id":"mock","requested_image_model":"gpt-image-2.5-sunburst"}
 monkeypatch.setattr(runner.provider,"request_edit",request)
 result=runner.edit("single-texture-test")
 assert result["status"]=="generated_unreviewed"
 assert len(calls)==1 and calls[0][0]==info["prepared_image"]
 assert calls[0][1]==[16,44]
 assert (root/result["restored_image"]).is_file()
 with Image.open(root/result["raw_model"]) as original_model:
  assert original_model.size==(800,2200)
 result2=runner.edit("single-texture-test")
 assert result2["status"]=="generated_unreviewed" and len(calls)==1


def test_real_sprite_shape_rotate_small_ccw_then_inverse(tmp_path,monkeypatch):
 """The two Sprite regions have different widths before the 90-degree turn."""
 monkeypatch.setattr(unit,"WORK",tmp_path)
 monkeypatch.setattr(unit,"OUT",tmp_path/"internal-composites")
 path=tmp_path/"original"/"sample"/"info_04.png"
 path.parent.mkdir(parents=True)
 source=Image.new("RGB",(512,512),(19,26,38))
 for y in range(1,367):
  for x in range(1,511):
   source.putpixel((x,y),(x%256,y%256,140))
 for y in range(371,511):
  for x in range(1,367):
   source.putpixel((x,y),(70,x%256,y%256))
 source.save(path)
 row={"id":"same-texture","bundle":"costumesalesinfo0015.unity3d",
      "texture_path_id":-4003115733690907056,
      "original":path.relative_to(tmp_path).as_posix(),
      "original_sha256":unit.sha(path),"original_size":[512,512]}
 (tmp_path/"manifest.jsonl").write_text(json.dumps(row),encoding="utf8")
 prepared=unit.prepare({"task_id":"rotated-sprite","texture_id":row["id"],
  "compose_direction":"horizontal","regions":[
   {"rect":[1,1,510,366],"order":0,"transform":"identity"},
   {"rect":[1,371,366,140],"order":1,"transform":"rotate270"}]})
 assert prepared["prepared_size"]==[650,366]
 assert prepared["region_map"][1]["canvas_box"]==[510,0,650,366]
 with Image.open(tmp_path/prepared["prepared_image"]) as full:
  expected_small=source.crop((1,371,367,511)).transpose(Image.Transpose.ROTATE_90)
  assert list(full.crop((510,0,650,366)).getdata())==list(expected_small.getdata())
 result=unit.restore("rotated-sprite",tmp_path/prepared["prepared_image"])
 with Image.open(tmp_path/result["restored_image"]) as restored:
  assert restored.size==(512,512)
  assert restored.tobytes()==source.tobytes()
