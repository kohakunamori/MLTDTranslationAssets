#!/usr/bin/env python3
"""Validated, credential-safe image/vision transport configuration for this tool.

network.http_proxy and https_proxy affect THIS PYTHON CLIENT -> configured
CLIProxyAPI base_url. The CLIProxyAPI SERVER -> Codex upstream proxy belongs
in CLIProxyAPI's separate config.yaml proxy-url setting.
"""
from __future__ import annotations
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from threading import Lock
from time import monotonic, sleep

CONFIG_PATH=Path(__file__).with_name("config.json")
DEFAULTS={
 "image_provider":{"base_url":"http://127.0.0.1:15721/v1","mode":"images_edits",
  "model":"imas-trans-image","api_key":"","api_key_env":"CLIPROXY_API_KEY",
  "require_api_key":False,"quality":"high","size":"auto","output_format":"png",
  "timeout_seconds":330,"min_request_interval_seconds":60,
  "legacy_controller_model":"gpt-5.6-luna"},
 "vision":{"base_url":"","model":"gpt-5.6-luna","timeout_seconds":220},
 "network":{"http_proxy":"","https_proxy":"","no_proxy":"localhost,127.0.0.1,::1",
  "trust_env_proxy":False,"proxy_localhost":False},
 "retry":{"http":1}
}
PNG_SIGNATURE=b"\x89PNG\r\n\x1a\n"

# The upstream image service enforces a requests-per-minute ceiling, so image
# edits leave this client at most once per min_request_interval_seconds.  The
# gate is process-global: extra threads queue behind it instead of stacking
# calls, which is why callers must also stay serial.
_IMAGE_REQUEST_GATE=Lock()
_last_image_request_at=0.0


def throttle_image_request(config:dict)->float:
 """Block until the next image edit may start; return the seconds slept.

 This is the single pacing point for image generation.  It never applies to
 vision/classification calls, which are not the rate-limited surface.
 """
 global _last_image_request_at
 interval=float(config["image_provider"].get("min_request_interval_seconds",60.0))
 if interval<=0:return 0.0
 with _IMAGE_REQUEST_GATE:
  now=monotonic()
  wait=_last_image_request_at+interval-now
  if wait>0:
   sleep(wait)
   now=monotonic()
  _last_image_request_at=now
  return max(0.0,wait)


def _url(value:str,field:str,allow_empty:bool=False)->str:
 if allow_empty and not value:return ""
 if not isinstance(value,str) or not value:
  raise ValueError(field+" must be a nonempty HTTP(S) URL")
 u=urllib.parse.urlsplit(value)
 if u.scheme not in {"http","https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
  raise ValueError(field+" must be an HTTP(S) base URL without credentials/query/fragment")
 return value.rstrip("/")


def load_config(path:Path|None=None)->dict:
 source=CONFIG_PATH if path is None else Path(path)
 if not source.is_file():
  example = source.with_name("config.example.json")
  if example.is_file():
   source = example
  else:
   raise FileNotFoundError("Image localization config not found: "+str(source))
 doc=json.loads(source.read_text(encoding="utf-8-sig"))
 if not isinstance(doc,dict):raise ValueError("Config root must be a JSON object")
 config={}
 for group,defaults in DEFAULTS.items():
  given=doc.get(group,{})
  if not isinstance(given,dict):raise ValueError(group+" must be an object")
  unknown=set(given)-set(defaults)
  if unknown:raise ValueError(group+" has unknown keys: "+",".join(sorted(unknown)))
  config[group]={**defaults,**given}
 image=config["image_provider"];net=config["network"];vision=config["vision"]
 image["base_url"]=_url(image["base_url"],"image_provider.base_url")
 vision["base_url"]=_url(vision["base_url"],"vision.base_url",allow_empty=True)
 if image["mode"] not in {"images_edits","legacy_responses"}:
  raise ValueError("image_provider.mode must be images_edits or legacy_responses")
 if not isinstance(image["model"],str) or not image["model"].strip():
  raise ValueError("image_provider.model must be a nonempty string")
 if image["quality"] not in {"low","medium","high","xhigh","max","auto"}:
  raise ValueError("image_provider.quality is invalid")
 if image["output_format"]!="png":
  raise ValueError("Only PNG is supported by the lossless atlas postprocessor")
 if not isinstance(image["size"],str) or not re.fullmatch(r"auto|[1-9][0-9]{2,4}x[1-9][0-9]{2,4}",image["size"]):
  raise ValueError("image_provider.size must be auto or WIDTHxHEIGHT")
 if not isinstance(image["timeout_seconds"],int) or not 10<=image["timeout_seconds"]<=1200:
  raise ValueError("image_provider.timeout_seconds must be 10..1200")
 interval=image["min_request_interval_seconds"]
 if isinstance(interval,bool) or not isinstance(interval,(int,float)) or not 0<=float(interval)<=3600:
  raise ValueError("image_provider.min_request_interval_seconds must be 0..3600 seconds")
 if not isinstance(vision["timeout_seconds"],int) or not 10<=vision["timeout_seconds"]<=1200:
  raise ValueError("vision.timeout_seconds must be 10..1200")
 if not isinstance(image["api_key"],str):
  raise ValueError("image_provider.api_key must be a string")
 if not isinstance(image["api_key_env"],str) or (image["api_key_env"] and not image["api_key_env"].isidentifier()):
  raise ValueError("image_provider.api_key_env must be an environment variable name")
 for key in ("require_api_key",):
  if not isinstance(image[key],bool):raise ValueError("image_provider."+key+" must be boolean")
 for key in ("trust_env_proxy","proxy_localhost"):
  if not isinstance(net[key],bool):raise ValueError("network."+key+" must be boolean")
 for key in ("http_proxy","https_proxy"):
  net[key]=_url(net[key],"network."+key,allow_empty=True)
 if not isinstance(net["no_proxy"],str):
  raise ValueError("network.no_proxy must be comma-separated hostnames")
 if not isinstance(config["retry"]["http"],int) or not 0<=config["retry"]["http"]<=2:
  raise ValueError("retry.http must be 0, 1, or 2")
 return config


def api_key(config:dict)->str:
 image=config["image_provider"]
 name=image["api_key_env"]
 key=image["api_key"].strip() or (os.environ.get(name,"") if name else "")
 if image["require_api_key"] and not key:
  raise ValueError("Set image_provider.api_key in config.json (or environment variable "+name+")")
 return key


def headers(config:dict,content_type:str|None=None)->dict[str,str]:
 h={"Accept":"application/json"}
 if content_type:h["Content-Type"]=content_type
 key=api_key(config)
 if key:h["Authorization"]="Bearer "+key
 return h


def _bypass_proxy(host:str,no_proxy:str,proxy_localhost:bool)->bool:
 h=host.lower().strip("[]")
 if not proxy_localhost and h in {"localhost","127.0.0.1","::1"}:
  return True
 for entry in no_proxy.split(","):
  entry=entry.strip().lower().lstrip(".")
  if not entry:continue
  if proxy_localhost and h in {"localhost","127.0.0.1","::1"}:continue
  if h==entry or h.endswith("."+entry):return True
 return False


def build_opener(config:dict,url:str):
 target=urllib.parse.urlsplit(url)
 if target.scheme not in {"http","https"}:
  raise ValueError("Only HTTP(S) requests are allowed")
 net=config["network"]
 if _bypass_proxy(target.hostname or "",net["no_proxy"],net["proxy_localhost"]):
  proxies={}
 else:
  proxies=urllib.request.getproxies() if net["trust_env_proxy"] else {}
  proxies={k:v for k,v in proxies.items() if k in {"http","https"}}
  for key in ("http_proxy","https_proxy"):
   if net[key]:proxies[key.removesuffix("_proxy")]=net[key]
 return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))


def send(config:dict,request:urllib.request.Request,timeout:int|None=None)->dict:
 seconds=timeout or config["image_provider"]["timeout_seconds"]
 with build_opener(config,request.full_url).open(request,timeout=seconds) as response:
  return json.load(response)


def direct_edit(config:dict,source:bytes,prompt:str,
                quality:str|None=None,timeout:int|None=None)->tuple[bytes,dict]:
 image=config["image_provider"]
 if image["mode"]!="images_edits":
  raise ValueError("direct_edit requires image_provider.mode=images_edits")
 throttle_image_request(config)
 if not source.startswith(PNG_SIGNATURE):
  raise ValueError("The reconstructed source is not a PNG")
 boundary="mltd-image-"+uuid.uuid4().hex
 pieces=[]
 def field(key:str,value:str)->None:
  pieces.append(("--"+boundary+"\r\nContent-Disposition: form-data; name=\""+key+"\"\r\n\r\n"+value+"\r\n").encode("utf-8"))
 field("model",image["model"]);field("prompt",prompt)
 field("quality",quality or image["quality"])
 field("size",image["size"]);field("n","1");field("output_format","png")
 pieces.append(("--"+boundary+"\r\nContent-Disposition: form-data; name=\"image\"; filename=\"reconstructed.png\"\r\nContent-Type: image/png\r\n\r\n").encode("ascii"))
 pieces.append(source);pieces.append(b"\r\n")
 pieces.append(("--"+boundary+"--\r\n").encode("ascii"))
 url=image["base_url"]+"/images/edits"
 req=urllib.request.Request(url,data=b"".join(pieces),
  headers=headers(config,"multipart/form-data; boundary="+boundary),method="POST")
 try:result=send(config,req,timeout)
 except urllib.error.HTTPError as exc:
  # Do not hide an unsupported CC Switch route or stale Codex OAuth as a
  # successful generation. No silent fallback to billed API key routes.
  raise
 if not isinstance(result,dict) or not isinstance(result.get("data"),list) or len(result["data"])!=1:
  raise RuntimeError("CLIProxyAPI image edit did not return exactly one data item")
 item=result["data"][0]
 if not isinstance(item,dict) or not isinstance(item.get("b64_json"),str):
  raise RuntimeError("CLIProxyAPI /images/edits returned no b64_json; configure PNG base64 image output")
 try:raw=base64.b64decode(item["b64_json"],validate=True)
 except (ValueError,base64.binascii.Error) as exc:raise RuntimeError("Invalid base64 image response") from exc
 if not raw.startswith(PNG_SIGNATURE):
  raise RuntimeError("CLIProxyAPI returned image bytes that are not PNG")
 return raw,{"response_id":result.get("id"),"requested_image_model":image["model"],
  "endpoint":"/v1/images/edits","mode":"images_edits","quality":quality or image["quality"],
  "size_requested":image["size"],"output_format":"png"}


def legacy_responses(config:dict,source:bytes,prompt:str,
                     quality:str|None=None,timeout:int|None=None)->tuple[bytes,dict]:
 image=config["image_provider"]
 if image["mode"]!="legacy_responses":raise ValueError("legacy mode is not configured")
 throttle_image_request(config)
 uri="data:image/png;base64,"+base64.b64encode(source).decode("ascii")
 payload={"model":image["legacy_controller_model"],
   "input":[{"role":"user","content":[{"type":"input_text","text":prompt},
     {"type":"input_image","image_url":uri}]}],
   "tools":[{"type":"image_generation","model":image["model"],"quality":quality or image["quality"]}],
   "tool_choice":"required"}
 req=urllib.request.Request(image["base_url"]+"/responses",
  data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),
  headers=headers(config,"application/json"),method="POST")
 result=send(config,req,timeout)
 calls=[v for v in result.get("output",[]) if v.get("type")=="image_generation_call" and v.get("result")]
 if not calls:raise RuntimeError("No image_generation_call result")
 raw=base64.b64decode(calls[0]["result"],validate=True)
 if not raw.startswith(PNG_SIGNATURE):raise RuntimeError("Hosted image tool returned non-PNG bytes")
 return raw,{"response_id":result.get("id"),"requested_image_model":image["model"],
   "controller":image["legacy_controller_model"],"endpoint":"/v1/responses",
   "mode":"legacy_responses","quality":quality or image["quality"]}


def image_edit(config:dict,source:bytes,prompt:str,
               quality:str|None=None,timeout:int|None=None)->tuple[bytes,dict]:
 if config["image_provider"]["mode"]=="images_edits":
  return direct_edit(config,source,prompt,quality,timeout)
 return legacy_responses(config,source,prompt,quality,timeout)


def vision_response(config:dict,image_data_uri:str,prompt:str,
                    timeout:int|None=None)->dict:
 vision=config["vision"];base=vision["base_url"] or config["image_provider"]["base_url"]
 payload={"model":vision["model"],
  "input":[{"role":"user","content":[{"type":"input_text","text":prompt},
                                  {"type":"input_image","image_url":image_data_uri}]}],
  "max_output_tokens":2300}
 req=urllib.request.Request(base+"/responses",
  data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),
  headers=headers(config,"application/json"),method="POST")
 return send(config,req,timeout or vision["timeout_seconds"])


def inspect(config:dict)->dict:
 image=config["image_provider"]
 return {"image_mode":image["mode"],"image_model":image["model"],
         "image_endpoint":image["base_url"]+("/images/edits" if image["mode"]=="images_edits" else "/responses"),
         "vision_endpoint":(config["vision"]["base_url"] or image["base_url"])+"/responses",
         "vision_model":config["vision"]["model"],
         "quality":image["quality"],"size":image["size"],"output_format":image["output_format"],
         "timeout_seconds":image["timeout_seconds"],
         "min_request_interval_seconds":image["min_request_interval_seconds"],
         "api_key_env":image["api_key_env"],"api_key_present":bool(api_key(config)),
         "http_proxy_configured":bool(config["network"]["http_proxy"]),
         "https_proxy_configured":bool(config["network"]["https_proxy"]),
         "proxy_localhost":config["network"]["proxy_localhost"],
         "retry_http":config["retry"]["http"]}
