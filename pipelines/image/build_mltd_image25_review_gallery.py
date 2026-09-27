#!/usr/bin/env python3
"""Build an offline human-review gallery with independent original and edited PNG links."""
from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
WORK=ROOT/"work/image-localization-25"
def sha_file(path:Path)->str:
    if not path.is_file():return ""
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()
def internal_candidates()->dict:
    """Surface only source-bound, SHA-verified ONE-texture edits for review."""
    entries={}
    choice=WORK/"internal-composites"/"review-selection.json"
    selected=json.loads(choice.read_text(encoding="utf-8")) if choice.is_file() else {}
    for doc in sorted((WORK/"internal-composites").glob("*/task.json")):
        info=json.loads(doc.read_text(encoding="utf-8"))
        task=info["task_id"]
        tid=info["texture_id"]
        if tid in selected and task!=selected[tid]:
            continue  # Preserve competing versions; display only the selected one.
        folder=WORK/"internal-composites"/task
        raw=folder/"edited-composite-model.png"
        source=WORK/info["prepared_image"]
        if not source.is_file() or sha_file(source)!=info["prepared_sha256"]:
            raise ValueError("Internal composite source changed: "+task)
        if tid in entries:
            raise ValueError("Multiple internal edits require a review-selection entry: "+tid)
        rel=info.get("restored_image","")
        target=WORK/rel if rel else None
        ready=bool(info.get("status")=="generated_unreviewed" and target
                   and target.is_file() and sha_file(target)==info.get("restored_sha256",""))
        moderation_file=folder/"moderation-blocked.json"
        moderation=json.loads(moderation_file.read_text(encoding="utf-8")) if moderation_file.is_file() else None
        if moderation and (moderation.get("status")!="moderation_blocked_manual_review"
                           or moderation.get("task_id")!=task
                           or moderation.get("source_sha256")!=info["source_sha256"]
                           or moderation.get("composite_sha256")!=info["prepared_sha256"]):
            raise ValueError("Moderation record cannot be attributed to source: "+task)
        entries[tid]={
            "blocked":bool(moderation),
            "pair_id":task,
            "whole_original":info["prepared_image"],
            "whole_edited":raw.relative_to(WORK).as_posix() if raw.is_file() else "",
            "raw_model":raw.relative_to(WORK).as_posix() if raw.is_file() else "",
            "edited":rel if ready else "",
            "edited_source_size":info.get("model_output_size"),
            "source_composite_size":info["prepared_size"],
            "original_sha256":info["source_sha256"],
        }
    # A single reconstructed edit is source-PNG deduplicated, but Unity may
    # reference that PNG via several bundle + Texture2D path_id locators.
    # Show each locator the CURRENT source-bound picture, never a stale raw-atlas
    # edit, and retain each locator as its own review/export row.
    queue=WORK/"reconstructed-preprocess"/"image-edit-queue.jsonl"
    if queue.is_file():
        originals={r["id"]:r for s in (WORK/"manifest.jsonl").read_text(encoding="utf-8").splitlines()
                   if s.strip() and (r:=json.loads(s))}
        for s in queue.read_text(encoding="utf-8").splitlines():
            if not s.strip():continue
            job=json.loads(s)
            representative=job["texture_id"]
            current=entries.get(representative)
            if current is None or current["pair_id"]!=job["task_id"]:
                raise ValueError("Current reconstructed edit missing from gallery: "+job["task_id"])
            for tid in job["source_ids"]:
                if (tid not in originals or
                    originals[tid]["original_sha256"]!=job["source_sha256"]):
                    raise ValueError("Reconstructed duplicate source SHA changed: "+tid)
                if tid in selected and selected[tid]!=job["task_id"]:
                    raise ValueError("Review selection conflicts with current reconstructed task: "+tid)
                entries[tid]=dict(current)
    for tid,task in selected.items():
        if tid not in entries:
            raise ValueError("Selected internal edit is missing: "+tid+" / "+task)
    return entries
HTML=r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MLTD 图片汉化 · 原图/汉化图逐张验收</title>
<style>
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#f0f2f6;background:#111820}
*{box-sizing:border-box}body{margin:0 auto;padding:20px;max-width:1500px}h1{font-size:1.5rem}
small,.muted{color:#b5c7d9}.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}
button,select,input{padding:9px;background:#27374d;color:white;border:1px solid #677990;border-radius:6px}
button{cursor:pointer}.active{background:#14735a}.bad{background:#9b334b}
#meta{background:#243248;padding:12px;border-radius:8px;overflow-wrap:anywhere;margin:12px 0}
.compare{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.panel{padding:10px;border-radius:10px;background:#1f2b3e;min-width:0}
.panel img{display:block;width:100%;height:auto;max-height:76vh;object-fit:contain;background:repeating-conic-gradient(#bbb 0 25%,#eee 0 50%) 50%/20px 20px}
.panel h2{font-size:1.1rem}.panel a{color:#a2d1ff;display:block;margin:9px 0;word-break:break-all}
.warning{color:#ffb76b}#note{width:100%}#pending{min-height:200px;display:flex;align-items:center;justify-content:center}
@media(max-width:800px){.compare{grid-template-columns:1fr}.panel img{max-height:58vh}}
</style></head><body>
<h1>MLTD 图片汉化 · 逐张人工验收</h1>
<p class="muted">原图在左，等比例归一化后的汉化图在右；如尚未归一化则显示原始生成图并给出警告。两图均可单独打开，原始模型输出也保留独立链接。验收请定期导出 CSV；网页不会回填 Unity。</p>
<div class="bar"><button id="prev">← 上一张</button><button id="next">下一张 →</button><button id="refresh">刷新最新结果</button>
<select id="position"></select><select id="filter"><option value="all">全部</option><option value="generated">已有汉化图</option><option value="pending">未产出</option><option value="blocked">安全系统拒绝（待复核）</option><option value="unreviewed">尚未验收</option><option value="rejected">需返修</option></select><span id="counter"></span></div>
<div id="meta"></div><div class="bar"><a id="wholeOriginal" target="_blank"></a><a id="wholeEdited" target="_blank"></a></div><div class="compare">
<section class="panel"><h2>① 日文原图</h2><a id="originalLink" target="_blank"></a><img id="originalImage" alt="原图"></section>
<section class="panel"><h2>② GPT Image 2.5 汉化图（适配原图尺寸）</h2><a id="editedLink" target="_blank"></a><a id="rawLink" target="_blank"></a><div id="pending">等待处理 / 暂无汉化图</div><img id="editedImage" alt="待验收结果"></section>
</div>
<div class="bar"><button id="approve">✓ 通过</button><button id="reject">✗ 返修</button><button id="defer">待定</button><span id="decision"></span><button id="export">导出验收记录 CSV</button></div>
<input id="note" placeholder="指出错误区域 / 日文残留 / 建议中文译文">
<p class="muted">←/→ 切换，选择“返修”时写明问题；选择“通过”不意味着脚本已更改游戏素材，只有导出的验收记录用于后续正式回填。</p>
<script>
const ITEMS=__ROWS__;
const BUILD_INFO=__BUILD_INFO__;
const KEY='mltd-image25-review-v1';
const $=id=>document.getElementById(id);
const decisions=(()=>{try{return JSON.parse(localStorage.getItem(KEY)||'{}')}catch{return {}}})();
function getDecision(r){const d=decisions[r.id]||{};if(!d.decision)return {};return d.editedSha256===r.editedSha256&&d.normalizedSha256===r.normalizedSha256?d:{stale:true};}
let position=0;let visible=[];
function list(){
 const f=$('filter').value;
 visible=ITEMS.map((r,n)=>n).filter(n=>{
  const r=ITEMS[n],d=getDecision(r).decision;
  return f==='all'||f==='generated'&&r.hasEdit||f==='pending'&&!r.hasEdit||f==='blocked'&&r.blocked||f==='unreviewed'&&!d||f==='rejected'&&d==='reject';
 });
 if(!visible.length){$('counter').textContent='当前筛选下没有条目';return}
 if(!visible.includes(position))position=visible[0];render();
}
function render(){
 const r=ITEMS[position];if(!r)return;
 $('counter').textContent=(visible.indexOf(position)+1)+' / '+visible.length+' · 总计 '+ITEMS.length+' 条纹理 · 本轮完成 '+BUILD_INFO.current+' / '+BUILD_INFO.total+' 张整图（对应 '+BUILD_INFO.currentTextures+' 条纹理） · 历史样片 '+BUILD_INFO.historical+' 张 · 页面快照 '+BUILD_INFO.builtAt;
 $('position').innerHTML=visible.map(n=>'<option value="'+n+'">'+(n+1)+' '+ITEMS[n].bundle+' '+ITEMS[n].texture+'</option>').join('');
 $('position').value=position;
 const flags=[r.sizeChanged?'模型输出尺寸与原图不同':'',r.alphaChanged?'模型输出透明通道发生变化':'',r.hasEdit&&!r.hasNormalized?'尚无归一化图片':'',getDecision(r).stale?'图片版本发生变化：须重新验收':'',r.error||''].filter(Boolean);
 $('meta').textContent=r.id+' | '+r.bundle+' | Texture2D path_id='+r.pathId+' | '+r.remote+' | 原图 '+r.originalSize.join('×')+' | 生成 '+(r.editedSize?r.editedSize.join('×'):'待生成')+' | '+r.model+' | '+flags.join('；');
 $('meta').className=flags.length?'warning':'';
 $('wholeOriginal').textContent=r.wholeOriginal?'查看单纹理重组原图':'';$('wholeEdited').textContent=r.wholeEdited?'查看 GPT Image 整图输出':'';
 if(r.wholeOriginal){$('wholeOriginal').href='../'+r.wholeOriginal}else{$('wholeOriginal').removeAttribute('href')}
 if(r.wholeEdited){$('wholeEdited').href='../'+r.wholeEdited}else{$('wholeEdited').removeAttribute('href')}
 $('originalImage').src='../'+r.original;
 $('originalLink').href='../'+r.original;
 $('originalLink').textContent='打开/保存原图 PNG';
 const image=$('editedImage');
 image.style.display=r.hasEdit?'block':'none';$('pending').style.display=r.hasEdit?'none':'flex';$('pending').textContent=r.blocked?'图片接口拒绝自动处理；源图已保留，待人工复核':'等待处理 / 暂无汉化图';
 if(r.hasEdit){const target=r.hasNormalized?r.normalized:r.edited;image.src='../'+target;image.onerror=()=>{image.style.display='none';$('pending').style.display='flex'};$('editedLink').href='../'+target;$('editedLink').textContent=r.hasNormalized?'打开/保存适配后的汉化 PNG':'打开原始模型 PNG（未归一化）';$('rawLink').href='../'+(r.rawModel||r.edited);$('rawLink').textContent=r.hasNormalized?'单独查看原始模型输出 PNG':''}
 else{image.removeAttribute('src');$('editedLink').removeAttribute('href');$('editedLink').textContent='尚未生成';$('rawLink').removeAttribute('href');$('rawLink').textContent=''}
 const d=getDecision(r);$('note').value=d.note||'';$('decision').textContent=d.stale?'图片已更新，原验收失效':d.decision||'未验收';
 $('approve').className=d.decision==='approve'?'active':'';$('reject').className=d.decision==='reject'?'bad':'';
}
function move(step){if(!visible.length)return;const k=visible.indexOf(position);position=visible[(k+step+visible.length)%visible.length];render()}
function decide(value){const r=ITEMS[position];if(value==='approve'&&!r.hasEdit){alert('尚未生成汉化图片，不能标记通过');return}decisions[r.id]={decision:value,note:$('note').value,date:new Date().toISOString(),editedSha256:r.editedSha256,normalizedSha256:r.normalizedSha256};try{localStorage.setItem(KEY,JSON.stringify(decisions))}catch{}render()}
$('prev').onclick=()=>move(-1);$('next').onclick=()=>move(1);$('refresh').onclick=()=>window.location.reload();
$('position').onchange=e=>{position=Number(e.target.value);render()};
$('filter').onchange=list;
$('approve').onclick=()=>decide('approve');$('reject').onclick=()=>decide('reject');$('defer').onclick=()=>decide('pending');
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT')return;if(e.key==='ArrowLeft')move(-1);if(e.key==='ArrowRight')move(1)});
$('export').onclick=()=>{
 const quote=s=>'"'+String(s??'').replaceAll('"','""')+'"';
 const csv=['id,bundle,texture_path_id,decision,note,original,edited,normalized,original_sha256,edited_sha256,normalized_sha256,date'];
 for(const r of ITEMS){const d=getDecision(r);csv.push([r.id,r.bundle,r.pathId,d.decision||'unreviewed',d.note||'',r.original,r.edited,r.hasNormalized?r.normalized:'',r.originalSha256,r.editedSha256,r.normalizedSha256,d.date||''].map(quote).join(','))}
 const file=new Blob(['\uFEFF'+csv.join('\r\n')],{type:'text/csv;charset=utf-8'});
 const link=document.createElement('a');link.href=URL.createObjectURL(file);link.download='mltd-image25-human-review.csv';link.click();URL.revokeObjectURL(link.href);
};
list();
</script></body></html>"""
def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--open",action="store_true")
    args=ap.parse_args(argv)
    manifest=WORK/"manifest.jsonl"
    if not manifest.is_file():raise SystemExit("Run prepare_mltd_image25_review.py first.")
    rows=[]
    paired=internal_candidates()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():continue
        r=json.loads(line)
        pair=paired.get(r["id"],{})
        if pair and pair["original_sha256"]!=r["original_sha256"]:
            raise ValueError("Original Texture2D changed since internal edit: "+r["id"])
        output=WORK/(pair["edited"] if pair else r["edited"]) if pair.get("edited") or not pair else None
        normalized=(pair["edited"] if pair else
                    "normalized/"+Path(r["edited"]).relative_to("edited").as_posix())
        native=WORK/normalized if normalized else None
        ready=bool(output and output.is_file() and native and native.is_file()
                   and (pair or native.stat().st_mtime_ns>=output.stat().st_mtime_ns))
        m=WORK/"metrics"/Path(r["edited"]).with_suffix(".json")
        meta=json.loads(m.read_text(encoding="utf-8")) if m.is_file() and not pair else {}
        generated_size=(pair.get("edited_source_size") if pair else meta.get("generated_size"))
        source_size=pair.get("source_composite_size",r["original_size"])
        rows.append({"id":r["id"],"bundle":r["bundle"],"texture":r["texture_name"],
                     "pathId":r["texture_path_id"],"remote":r["remote"],
                     "original":r["original"],
                     "edited":pair["edited"] if pair else r["edited"],
                     "normalized":normalized or "",
                     "rawModel":pair.get("raw_model",""),
                     "wholeOriginal":pair.get("whole_original",""),
                     "wholeEdited":pair.get("whole_edited",""),
                     "pairId":pair.get("pair_id",""),
                     "originalSha256":r["original_sha256"],
                     "editedSha256":sha_file(output) if output else "",
                     "normalizedSha256":sha_file(native) if ready else "",
                     "originalSize":r["original_size"],
                     "hasEdit":bool(output and output.is_file()),"hasNormalized":ready,
                     "blocked":bool(pair.get("blocked",False)),
                     "editedSize":generated_size,
                     "sizeChanged":bool(generated_size and generated_size!=source_size),
                     "alphaChanged":meta.get("alpha_changed",False),
                     "model":"gpt-image-2.5-sunburst (single-texture reconstructed)" if pair else
                             meta.get("requested_image_model","pending"),
                     "error":"安全系统拒绝自动处理；保留源图待复核" if pair.get("blocked") else meta.get("error","")})
    dest=WORK/"review/index.html";dest.parent.mkdir(parents=True,exist_ok=True)
    from datetime import datetime
    current_ids={r["pairId"] for r in rows if r["hasEdit"] and r["pairId"].startswith("recon-")}
    current=len(current_ids)
    current_textures=sum(r["hasEdit"] and r["pairId"].startswith("recon-") for r in rows)
    historical=sum(r["hasEdit"] and not r["pairId"].startswith("recon-") for r in rows)
    build_info={"current":current,"currentTextures":current_textures,"historical":historical,"total":937,
                "builtAt":datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    document=(HTML.replace("__ROWS__",json.dumps(rows,ensure_ascii=False,separators=(",",":")).replace("</","<\\/"))
              .replace("__BUILD_INFO__",json.dumps(build_info,ensure_ascii=False)))
    tmp=dest.with_suffix(".html.writing")
    tmp.write_text(document,encoding="utf-8")
    tmp.replace(dest)
    print(json.dumps({"tasks":len(rows),"generated":sum(r["hasEdit"] for r in rows),
                       "current_run":current,"historical":historical,"gallery":str(dest)},ensure_ascii=False),flush=True)
    if args.open:
        import os;os.startfile(str(dest))
    return 0
if __name__=="__main__":raise SystemExit(main())
