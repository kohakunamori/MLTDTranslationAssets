"""Safety-declined images are retained for human review; later jobs continue."""
from __future__ import annotations
import io,json,sys,urllib.error
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import run_reconstructed_batch as batch
import build_mltd_image25_review_gallery as gallery

def error(code:int,payload:dict)->urllib.error.HTTPError:
 return urllib.error.HTTPError("http://127.0.0.1:15721/v1/images/edits",
    code,"mock error",None,io.BytesIO(json.dumps(payload).encode("utf-8")))

def setup(tmp_path,monkeypatch):
 root=tmp_path/"image-work"
 out=root/"internal-composites"
 out.mkdir(parents=True)
 proc=root/"reconstructed-preprocess";proc.mkdir()
 jobs=[
  {"task_id":"recon-first","texture_id":"a:1","source_sha256":"sha-a",
   "composite_sha256":"composite-a","source_ids":["a:1"]},
  {"task_id":"recon-second","texture_id":"b:2","source_sha256":"sha-b",
   "composite_sha256":"composite-b","source_ids":["b:2"]}]
 monkeypatch.setattr(batch,"WORK",root)
 monkeypatch.setattr(batch,"PRE",proc)
 monkeypatch.setattr(batch,"PROGRESS",proc/"batch-progress.json")
 monkeypatch.setattr(batch,"QUEUE",proc/"image-edit-queue.jsonl")
 monkeypatch.setattr(batch,"SUMMARY",proc/"summary.json")
 monkeypatch.setattr(batch.atlas,"OUT",out)
 monkeypatch.setattr(batch,"prepare_queue",lambda:jobs)
 monkeypatch.setattr(batch.provider_config,"load_config",lambda:{
  "image_provider":{"quality":"high","timeout_seconds":30,
                    "model":"gpt-image-2.5-sunburst","mode":"images_edits"},
  "retry":{"http":1}})
 monkeypatch.setattr(gallery,"main",lambda argv:0)
 monkeypatch.setattr(sys,"argv",["run_reconstructed_batch.py"])
 batch.SUMMARY.write_text(json.dumps({"automatic_no_japanese_skip_unique":0,
                                      "automatic_no_japanese_skip_objects":0}),encoding="utf-8")
 batch.QUEUE.write_text("immutable-queue\n",encoding="utf-8")
 return out,jobs

def test_moderation_blocks_only_explicit_upstream_code():
 payload={"error":{"code":"moderation_blocked","type":"image_generation_user_error",
  "moderation_details":{"moderation_stage":"input","categories":["sexual"]}}}
 reason=batch.moderation_reason(400,json.dumps(payload))
 assert reason["upstream_error_code"]=="moderation_blocked"
 assert reason["moderation_stage"]=="input"
 assert reason["categories"]==["sexual"]
 assert batch.moderation_reason(401,json.dumps(payload)) is None
 assert batch.moderation_reason(400,'{"error":{"code":"invalid_request_error"}}') is None
 assert batch.moderation_reason(400,"truncated json") is None
 assert batch.moderation_reason(408,json.dumps(payload)) is None

def test_blocked_job_saved_and_not_resent_on_resume(tmp_path,monkeypatch):
 out,jobs=setup(tmp_path,monkeypatch)
 payload={"error":{"code":"moderation_blocked","moderation_details":{
   "moderation_stage":"input","categories":["sexual"]}}}
 calls=[]
 def edit(tid,quality,timeout):
  calls.append(tid)
  if tid=="recon-first":raise error(400,payload)
  folder=out/tid;folder.mkdir()
  (folder/"restored-texture.png").write_bytes(b"mock output")
  (folder/"task.json").write_text(json.dumps({"status":"generated_unreviewed"}),encoding="utf8")
  return {"status":"generated_unreviewed"}
 monkeypatch.setattr(batch.editor,"edit",edit)
 assert batch.main()==0
 assert calls==["recon-first","recon-second"]
 record=batch.blocked_record(jobs[0])
 assert record["status"]=="moderation_blocked_manual_review"
 assert record["source_sha256"]=="sha-a"
 assert record["categories"]==["sexual"]
 assert not (out/"recon-first"/"restored-texture.png").exists()
 progress=json.loads(batch.PROGRESS.read_text(encoding="utf8"))
 assert progress["completed_total"]==1
 assert progress["moderation_blocked_total"]==1
 assert progress["moderation_blocked_this_run"]==1
 assert progress["failed"]==0
 assert batch.main()==0
 assert calls==["recon-first","recon-second"],"resume must not re-submit declined image"
 progress=json.loads(batch.PROGRESS.read_text(encoding="utf8"))
 assert progress["selected_this_run"]==0
 assert progress["already_generated"]==1
 assert progress["moderation_blocked_total"]==1
 assert progress["moderation_blocked_this_run"]==0

def test_unrelated_http_400_still_stops_batch(tmp_path,monkeypatch):
 out,jobs=setup(tmp_path,monkeypatch)
 calls=[]
 def edit(tid,quality,timeout):
  calls.append(tid)
  raise error(400,{"error":{"code":"bad_request","message":"missing model"}})
 monkeypatch.setattr(batch.editor,"edit",edit)
 assert batch.main()==2
 assert calls==["recon-first"]
 assert not batch.moderation_path(jobs[0]["task_id"]).is_file()
 progress=json.loads(batch.PROGRESS.read_text(encoding="utf8"))
 assert progress["status"]=="stopped_proxy_http_error"
 assert progress["moderation_blocked_total"]==0
