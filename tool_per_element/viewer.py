"""Builds one self-contained HTML page: the source on the left (page images with every element's box, or the web
page), the merged Markdown on the right in reading order, each element with its tool, time and confidence."""
import base64
import html
import io
import json

TYPE_COLOURS = {"heading": "#2563eb", "text": "#6b7280", "table": "#16a34a", "formula": "#9333ea",
                "key_value": "#ea580c", "image": "#0891b2"}


def page_images(pdf_path, scale=1.5):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_path)
    out = []
    for i in range(len(pdf)):
        img = pdf[i].render(scale=scale).to_pil().convert("RGB")
        out.append(img)
    return out


def b64(img, fmt="JPEG", quality=82):
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=quality)
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()


def crop(img, bbox):
    W, H = img.size
    x0, y0, x1, y1 = bbox
    return img.crop((max(0, int(x0 * W) - 4), max(0, int(y0 * H) - 4), min(W, int(x1 * W) + 4), min(H, int(y1 * H) + 4)))


def build(data, out_path, pdf_path=None, html_source=None):
    pages = []
    if pdf_path:
        imgs = page_images(pdf_path)
        for e in data["merged"]:
            if e["type"] == "image" and e.get("bbox"):
                e["crop"] = b64(crop(imgs[e["page"] - 1], e["bbox"]))
        pages = [b64(im) for im in imgs]
    payload = json.dumps({**data, "pages": pages, "colours": TYPE_COLOURS}, ensure_ascii=False, default=str)
    payload = payload.replace("</", "<\\/")
    src_frame = html.escape(html_source or "", quote=True)
    page = TEMPLATE.replace("__DATA__", payload).replace("__SRCDOC__", src_frame).replace("__TITLE__", html.escape(data["source_name"]))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>__TITLE__ - one tool per element</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif;color:#1f2328;background:#f6f7f9}
header{padding:14px 20px;background:#fff;border-bottom:1px solid #e3e5e8}h1{font-size:18px;margin:0 0 4px}
.mut{color:#6b7280}.wrap{padding:12px 20px}
table.sum{border-collapse:collapse;background:#fff;width:100%;font-size:13px}
table.sum th,table.sum td{border:1px solid #e3e5e8;padding:6px 8px;text-align:left;vertical-align:top}
table.sum th{background:#f0f2f5}td.num{text-align:right;font-variant-numeric:tabular-nums}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle}
.panes{display:flex;gap:12px;padding:0 20px 20px;height:calc(100vh - 40px)}
.left,.right{flex:1;overflow:auto;background:#fff;border:1px solid #e3e5e8;border-radius:6px}
.pg{position:relative;margin:10px auto;width:95%}.pg img{width:100%;display:block;border:1px solid #ddd}
.box{position:absolute;border:2px solid;cursor:pointer;opacity:.75}.box.approx{border-style:dashed}
.box.sel{opacity:1;box-shadow:0 0 0 3px #facc15}
.pgno{font-size:12px;color:#6b7280;margin:8px 2.5%}
.el{border-left:4px solid #ccc;margin:8px 10px;padding:6px 10px;background:#fff;cursor:pointer;border-radius:3px}
.el:hover{background:#f8fafc}.el.sel{background:#fefce8}.el.hid{opacity:.45}
.meta{font-size:12px;color:#6b7280;margin-bottom:3px}
.chip{display:inline-block;border:1px solid #d0d4da;border-radius:10px;padding:0 7px;margin-right:4px;font-size:11.5px}
.badge{display:inline-block;border-radius:10px;padding:0 8px;font-weight:600;font-size:12px;color:#fff}
.b1{background:#15803d}.b2{background:#65a30d}.b3{background:#d97706}.b4{background:#dc2626}.bn{background:#9ca3af}
.keyline{font-size:12.5px;margin-top:4px;padding:4px 6px;background:#f8fafc;border-radius:4px}
del{background:#fee2e2;color:#991b1b}ins{background:#dcfce7;color:#166534;text-decoration:none}
table.md{border-collapse:collapse;font-size:12.5px}table.md td,table.md th{border:1px solid #d0d4da;padding:2px 6px}
.el h1,.el h2,.el h3,.el h4{margin:2px 0;font-size:16px}.el p{margin:2px 0}
.miss{margin:8px 10px;padding:6px 10px;border:1px dashed #dc2626;border-radius:4px;background:#fff7f7}
.ctrl{padding:6px 10px;border-bottom:1px solid #eee;position:sticky;top:0;background:#fff;z-index:2;font-size:13px}
iframe{width:100%;height:100%;border:0}
</style></head><body>
<header><h1>__TITLE__</h1><div class="mut" id="sub"></div></header>
<div class="wrap"><table class="sum" id="sum"></table><div id="order" style="margin-top:8px"></div>
<p class="mut" style="font-size:12.5px;margin:8px 0 0">Confidence = similarity with the answer key read by a person from the PDF: 1.00 = exactly as printed (line breaks, spaces, letter case and Markdown symbols ignored); "not in key" = 0 (e.g. a heading that is not a heading); text paragraphs are scored on the key sentences they contain, the others are "not checked". Coverage = mean over the answer-key elements of their best match (missing = 0).</p></div>
<div class="panes"><div class="left" id="left"></div><div class="right"><div class="ctrl">
<label><input type="checkbox" id="showhid"> show paragraphs hidden as duplicates</label> &nbsp;
<label>type <select id="ftype"><option value="">all</option></select></label></div><div id="list"></div><div id="missing"></div></div></div>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id);const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=(v,d=2)=>v==null?"–":Number(v).toFixed(d);
$("sub").textContent=`${D.kind==="pdf"?D.n_pages+" pages":"web page"} · answer key: ${D.key_name||"none found"} · total extraction time ${fmt(D.total_seconds,2)} s · merge ${fmt(D.merge_seconds,2)} s`;
// summary
let h="<tr><th>Element type</th><th>Tool</th><th>Status</th><th>Extracted</th><th>Time (s)</th><th>ms / element</th><th>Mean confidence (checked)</th><th>Exact (1.00)</th><th>In answer key</th><th>Key elements found</th><th>Coverage of key</th></tr>";
for(const r of D.summary){h+=`<tr><td><span class="sw" style="background:${D.colours[r.type]}"></span>${r.type.replace("_","-")}</td><td>${r.tool}</td><td>${esc(r.status)}${r.error?`<div class="mut" style="font-size:11.5px">${esc(r.error)}</div>`:""}</td>
<td class="num">${r.extracted}</td><td class="num">${fmt(r.seconds,2)}</td><td class="num">${fmt(r.ms_per_element,1)}</td><td class="num">${fmt(r.mean_confidence,3)} ${r.checked?`<span class="mut">(${r.checked})</span>`:""}</td>
<td class="num">${r.exact}</td><td class="num">${r.in_key}</td><td class="num">${r.key_found} / ${r.in_key}</td><td class="num">${fmt(r.key_coverage,3)}</td></tr>`}
$("sum").innerHTML=h;
const o=D.order;$("order").innerHTML=`<b>Reading order of the merged output:</b> ${o.score==null?"no answer-key headings/sentences to compare":`${o.in_order} of ${o.pairs} neighbouring answer-key headings and sentences keep their printed order (<b>${fmt(o.score,3)}</b>)`}.`;
for(const t of Object.keys(D.colours)){const op=document.createElement("option");op.value=t;op.textContent=t;$("ftype").appendChild(op)}
function badge(e){const c=e.confidence;if(c==null)return`<span class="badge bn">not checked</span>`;
 const k=c>=0.999?"b1":c>=0.9?"b2":c>=0.7?"b3":"b4";return`<span class="badge ${k}">${fmt(c,2)}${c>=0.999?" exact":""}</span>`}
function tex(s,disp){try{return katex.renderToString(s,{displayMode:disp,throwOnError:true})}catch{return`<code>${esc(s)}</code>`}}
function body(e){
 if(e.type==="heading"){const l=Math.min(e.level||2,4);return`<h${l}>${esc(e.text)}</h${l}>`}
 if(e.type==="table"&&e.cells){return`<table class="md">`+e.cells.map((r,i)=>"<tr>"+r.map(c=>`<${i?"td":"th"}>${esc(c)}</${i?"td":"th"}>`).join("")+"</tr>").join("")+"</table>"}
 if(e.type==="formula")return e.plain_text?`<code style="font-size:14px">${esc(e.text)}</code>`:tex(e.latex||e.text,true);
 if(e.type==="key_value")return`<b>${esc(e.key)}:</b> ${esc(e.value)}`;
 if(e.type==="image")return(e.crop?`<img src="${e.crop}" style="max-width:100%;max-height:260px;display:block">`:"")+`<div class="mut">${esc(e.caption||"(no caption)")}</div>`;
 return`<p>${esc(e.md_merged??e.md)}</p>`}
function keyline(e){
 if(e.verdict==="exact")return"";
 if(e.confidence==null)return"";
 if(e.key_text==null)return`<div class="keyline">${esc(e.verdict)}</div>`;
 if(e.diff)return`<div class="keyline">vs answer key: ${e.diff}</div>`;
 if(e.type==="formula")return`<div class="keyline">answer key: ${tex(e.key_text,false)}</div>`;
 return`<div class="keyline">answer key: ${esc(e.key_text)}</div>`}
const left=$("left");
if(D.kind==="pdf"){D.pages.forEach((src,i)=>{const w=document.createElement("div");w.innerHTML=`<div class="pgno">page ${i+1}</div>`;
 const pg=document.createElement("div");pg.className="pg";pg.id="pg"+(i+1);pg.innerHTML=`<img src="${src}">`;w.appendChild(pg);left.appendChild(w)})}
else{left.innerHTML=`<iframe sandbox srcdoc="__SRCDOC__"></iframe>`}
function render(){const list=$("list");list.innerHTML="";document.querySelectorAll(".box").forEach(b=>b.remove());
 const sh=$("showhid").checked,ft=$("ftype").value;let last=0;
 D.merged.forEach((e,n)=>{if(e.hidden&&!sh)return;if(ft&&e.type!==ft)return;
  if(D.kind==="pdf"&&e.page!==last){last=e.page;list.insertAdjacentHTML("beforeend",`<div class="pgno">page ${e.page}</div>`)}
  const c=D.colours[e.type];const d=document.createElement("div");d.className="el"+(e.hidden?" hid":"");d.id="el"+n;d.style.borderLeftColor=c;
  d.innerHTML=`<div class="meta"><span class="chip" style="border-color:${c};color:${c}">${e.type.replace("_","-")}</span><span class="chip">${e.tool}</span>${badge(e)} ${e.hidden?`<span class="chip">hidden: ${esc(e.hidden)}</span>`:""}${e.located===false?`<span class="chip">position not found</span>`:""}</div>${body(e)}${keyline(e)}`;
  list.appendChild(d);
  if(D.kind==="pdf"&&e.bbox){const b=document.createElement("div");b.className="box"+(e.approx_box?" approx":"");b.id="bx"+n;b.style.borderColor=c;
   const[x0,y0,x1,y1]=e.bbox;Object.assign(b.style,{left:x0*100+"%",top:y0*100+"%",width:(x1-x0)*100+"%",height:(y1-y0)*100+"%"});
   b.title=`${e.type} · ${e.tool} · ${e.confidence==null?"not checked":fmt(e.confidence,2)}`;
   b.onclick=()=>select(n,"left");$("pg"+e.page).appendChild(b)}
  d.onclick=()=>select(n,"right")});
 const m=$("missing");m.innerHTML=D.missing.length?`<div class="pgno">In the answer key but not found (count as 0)</div>`+D.missing.map(k=>`<div class="miss"><span class="chip" style="border-color:${D.colours[k.type]};color:${D.colours[k.type]}">${k.type}</span><span class="chip">page ${k.page??1}</span>${k.type==="formula"?tex(k.key_text,false):esc(k.key_text)}</div>`).join(""):""}
function select(n,from){document.querySelectorAll(".sel").forEach(x=>x.classList.remove("sel"));
 const d=$("el"+n),b=$("bx"+n);if(d)d.classList.add("sel");if(b)b.classList.add("sel");
 if(from==="right"&&b)b.scrollIntoView({behavior:"smooth",block:"center"});if(from==="left"&&d)d.scrollIntoView({behavior:"smooth",block:"center"})}
$("showhid").onchange=render;$("ftype").onchange=render;render();
</script></body></html>"""
