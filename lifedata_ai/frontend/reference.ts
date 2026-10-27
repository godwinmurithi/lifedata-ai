/** Runtime for the immutable SINCEP source. Preview runs its curated logic;
 * live publications never evaluate that illustrative dataset. */
import * as echarts from "echarts/core";
import {BarChart,LineChart,PieChart,ScatterChart} from "echarts/charts";
import {GridComponent,LegendComponent,TooltipComponent} from "echarts/components";
import {CanvasRenderer} from "echarts/renderers";
import {Encoding,resolveEncoding,pivotRows,numeric,formatValue} from "./chart-encoding";
echarts.use([BarChart,LineChart,PieChart,ScatterChart,GridComponent,LegendComponent,TooltipComponent,CanvasRenderer]);
import {mountQueryMap} from "./query-map";
type Binding={geography?:unknown;chart_ids:number[];title?:string;encoding?:Encoding};
type Slot={label:string;page:string;kind:string};
type Config={publication_id:string|null;title:string;bindings:Record<string,Binding>;slots:Record<string,Slot>;mode:string};
const config=JSON.parse(document.getElementById("vima-reference-data")!.textContent!) as Config;
const root=document.querySelector("x-dc")!;
const original=Array.from(root.childNodes).map(node=>node.cloneNode(true));
const css=document.createElement("style");
css.textContent=`x-dc,sc-if,sc-for{display:contents}.vima-ref-banner{font:13px system-ui;background:#f5f2ec;color:#3b4d45;padding:8px 16px;border-bottom:1px solid #c9c2b6}.vima-ref-live{font-family:Inter,system-ui,sans-serif}.vima-ref-live h2{font-size:16px}.vima-ref-slot{display:flex;flex-direction:column;min-height:0;flex:1;gap:8px}.vima-ref-plot{width:100%;min-height:210px;flex:1}.vima-ref-tabs{display:flex;gap:5px;overflow:auto;flex:none}.vima-ref-tabs button{font:12px system-ui;border:1px solid #c9c2b6;border-radius:6px;padding:7px 10px;background:#fff;color:#36554a;white-space:nowrap;cursor:pointer}.vima-ref-tabs button[aria-selected=true]{background:#2f6670;color:white}.vima-ref-table{overflow:auto;min-height:0;max-height:280px;font:13px system-ui}.vima-ref-table table{width:100%;border-collapse:collapse}.vima-ref-table td,.vima-ref-table th{text-align:left;padding:8px;border-bottom:1px solid #e6e1d7}.vima-ref-message{font:13px system-ui;color:#66615a;padding:14px;line-height:1.5}.vima-ref-error{color:#962c24}.vima-ref-kpi{font:700 28px system-ui}.vima-ref-live button:focus-visible,.vima-ref-live select:focus-visible{outline:3px solid #187d8c;outline-offset:2px}.vima-ref-page-controls{display:flex;gap:8px;padding:8px 14px;background:#f5f2ec}.vima-ref-page-controls button{font:13px system-ui;background:#fff;border:1px solid #d4cabb;border-radius:6px;padding:6px 12px;cursor:pointer}.vima-ref-page-controls button[aria-current=true]{background:#2f6670;color:#fff}`;
document.head.append(css);
const banner=document.createElement("div");banner.className="vima-ref-banner";
banner.textContent=config.mode==="live"?`${config.title} · Live saved queries · Draft until verified` : "Supplied master · Illustrative reference data · Interactive preview";
document.body.prepend(banner);
// This evaluator only handles expressions from our fingerprinted, curated HTML.
// Values returned from queries are assigned as text/data; never evaluated.
function evaluate(expression:string,scope:Record<string,unknown>):unknown {
 try{return Function("scope",`with(scope){return (${expression})}`)(scope);}catch{return undefined;}
}
function interpolate(value:string,scope:Record<string,unknown>):string {
 return value.replace(/{{([\s\S]*?)}}/g,(_,exp)=>{const v=evaluate(exp.trim(),scope);return v==null?"":String(v);});
}
function build(node:Node,scope:Record<string,unknown>):Node[] {
 if(node.nodeType===Node.TEXT_NODE)return [document.createTextNode(interpolate(node.textContent||"",scope))];
 if(!(node instanceof Element))return [];
 if(node.tagName==="HELMET"){for(const child of Array.from(node.children))if(child.tagName==="STYLE")document.head.append(child.cloneNode(true));return [];}
 if(node.tagName==="SC-IF"){
  const exp=node.getAttribute("value")?.replace(/^{{|}}$/g,"").trim()||"false";
  return evaluate(exp,scope)?Array.from(node.childNodes).flatMap(child=>build(child,scope)):[];
 }
 if(node.tagName==="SC-FOR"){
  const exp=node.getAttribute("list")?.replace(/^{{|}}$/g,"").trim()||"[]",items=evaluate(exp,scope),as=node.getAttribute("as")||"item";
  return Array.isArray(items)?items.flatMap(item=>Array.from(node.childNodes).flatMap(child=>build(child,{...scope,[as]:item}))):[];
 }
 const output=node.cloneNode(false) as Element;
 for(const attr of Array.from(node.attributes)){
  if(attr.name.startsWith("on")){
   output.removeAttribute(attr.name);const exp=attr.value.replace(/^{{|}}$/g,"").trim(),handler=evaluate(exp,scope);
   if(typeof handler==="function")output.addEventListener(attr.name.slice(2).toLowerCase(),handler as EventListener);
  }else output.setAttribute(attr.name,interpolate(attr.value,scope));
 }
 for(const child of Array.from(node.childNodes))for(const rendered of build(child,scope))output.appendChild(rendered);
 if(output instanceof HTMLSelectElement)output.value=interpolate(node.getAttribute("value")||"",scope);
 return [output];
}
function paint(scope:Record<string,unknown>){root.replaceChildren(...original.flatMap(node=>build(node,scope)));}
if(config.mode!=="live"){
 class DCLogic {state:Record<string,unknown>={};constructor(_props?:unknown){} setState(patch:Record<string,unknown>){Object.assign(this.state,patch);paint((this as unknown as {renderVals:()=>Record<string,unknown>}).renderVals());}}
 const script=document.querySelector('script[data-dc-script]')!.textContent!;
 const Component=Function("DCLogic",`${script};return Component;`)(DCLogic);
 const instance=new Component({});paint(instance.renderVals());
}else{
 document.body.classList.add("vima-ref-live");
 let page="overview",detail=false;
 const criteria:Record<string,string[]>={};
 const request=async(url:string)=>{const response=await fetch(url,{credentials:"same-origin"});if(!response.ok)throw new Error(`Live query failed (${response.status}).`);return response.json();};
 const instances:echarts.ECharts[]=[];
 function shell(){
  instances.splice(0).forEach(instance=>instance.dispose());
  const vals:Record<string,unknown>={showOverview:page==="overview",showSentinel:page==="sentinel",showGeo:page==="geo",isEmpty:false,hasChips:false,
   tabs:[{label:"Overview",key:"overview"},{label:"Sentinel surveillance",key:"sentinel"},{label:"Geospatial intelligence",key:"geo"}].map(tab=>({...tab,line:page===tab.key?"#7fa98b":"transparent",weight:page===tab.key?600:500,color:page===tab.key?"#fff":"#b9b9b9",pick:()=>{page=tab.key;detail=false;shell();}}))};
  paint(vals);
  // Replace the reference filters entirely: only validated publication filters
  // are offered. The original sample country/village options never execute.
  const nav=root.querySelector("nav")!,filterBar=nav.nextElementSibling;
  if(filterBar){filterBar.replaceChildren();(filterBar as HTMLElement).style.padding="8px 14px";void loadFilters(filterBar as HTMLElement);}
  const controls=document.createElement("div");controls.className="vima-ref-page-controls";
  for(const [label,value] of [["Main view",false],["Supporting detail",true]] as const){const b=document.createElement("button");b.textContent=label;b.setAttribute("aria-current",String(detail===value));b.onclick=()=>{detail=value;shell();};controls.append(b);}
  filterBar?.after(controls);
  const outer=root.firstElementChild as HTMLElement;if(outer){outer.style.minHeight="0";outer.style.padding="0 14px 14px";const inner=outer.firstElementChild as HTMLElement;if(inner)inner.style.minHeight="0";}
  const activeSlots=Object.entries(config.slots).filter(([,slot])=>slot.page===page);
  const detailKeys=new Set(["age_gender","housing","water","bednets","surveillance_signals","hotspot_villages"]);
  for(const [key,slot] of activeSlots){
   const heading=Array.from(root.querySelectorAll(slot.kind==="kpi"?"div":"h2")).find(node=>node.childElementCount===0 && node.textContent?.trim()===slot.label);
   if(!heading)continue;
   let card=heading.parentElement!;
   while(card.parentElement && !(card.style.border && card.style.borderRadius))card=card.parentElement;
   card.dataset.referenceSlot=key;
   const onDetail=detailKeys.has(key);
   if(slot.kind!=="kpi" && onDetail!==detail){card.hidden=true;card.style.display="none";continue;}
   const binding=config.bindings[key];
   card.replaceChildren();card.style.minHeight=slot.kind==="kpi"?"78px":"260px";card.style.height=slot.kind==="kpi"?"auto":"min(37vh, 350px)";card.style.overflow="hidden";
   const label=document.createElement(slot.kind==="kpi"?"div":"h2");label.textContent=binding?.title || slot.label;label.style.cssText="margin:0 0 8px;font-size:14px;color:#66615a";card.append(label);
   const mount=document.createElement("div");mount.className="vima-ref-slot";card.append(mount);
   if(!binding){message(mount,"Not available in the bound data.");continue;}
   let active=binding.chart_ids[0];
   if(binding.chart_ids.length>1){const tabs=document.createElement("div");tabs.className="vima-ref-tabs";tabs.setAttribute("role","tablist");for(const id of binding.chart_ids){const button=document.createElement("button");button.textContent=`Chart ${id}`;button.dataset.chart=String(id);button.setAttribute("role","tab");button.setAttribute("aria-selected",String(active===id));button.onclick=()=>{active=id;tabs.querySelectorAll("button").forEach(b=>b.setAttribute("aria-selected",String(b===button)));void load();};tabs.append(button);}mount.append(tabs);}
   const plot=document.createElement("div");plot.className="vima-ref-slot";plot.setAttribute("role","tabpanel");mount.append(plot);
   async function load(){message(plot,"Loading live data…");try{
    const data=await request(binding.geography?`/lifedata-ai/api/publications/${config.publication_id}/map-data/${key}/?criteria=${encodeURIComponent(JSON.stringify(criteria))}`:`/lifedata-ai/api/publications/${config.publication_id}/charts/${active}/?criteria=${encodeURIComponent(JSON.stringify(criteria))}`);
    if(!plot.isConnected)return;
    render(plot,data,binding.geography?{chart_type:"map",geometry_field:"geometry_geojson",name_field:"area_name",metrics:["record_count"],...binding.encoding}:binding.encoding || {},slot.kind);
    if(binding.geography){const note=document.createElement("small");note.textContent=[...(data.notes||[]),data.location_basis||"",data.truncated?"Source query is truncated; map coverage is partial.":""].filter(Boolean).join(" · ");plot.append(note);}
   }catch(error){message(plot,(error as Error).message,true);}}void load();
  }
  // Rows consisting solely of detail cards must release their unused height.
  root.querySelectorAll<HTMLElement>('[style*="display:flex"]').forEach(row=>{const cards=Array.from(row.children).filter(c=>(c as HTMLElement).dataset.referenceSlot);if(cards.length && cards.every(c=>(c as HTMLElement).hidden))row.style.display="none";});
  const ask=Array.from(root.querySelectorAll("button")).find(b=>b.textContent?.trim()==="Ask Vima");if(ask)ask.onclick=()=>window.parent.postMessage({type:"vima-open-assistant"},location.origin);
 }
 function message(el:HTMLElement,text:string,error=false){el.replaceChildren();const p=document.createElement("div");p.className=`vima-ref-message ${error?"vima-ref-error":""}`;p.textContent=text;if(error)p.setAttribute("role","alert");el.append(p);}
 async function loadFilters(el:HTMLElement){try{
  const data=await request(`/lifedata-ai/api/publications/${config.publication_id}/filter-options/?criteria=${encodeURIComponent(JSON.stringify(criteria))}`);
  if(!el.isConnected)return;
  el.style.cssText+=";display:flex;gap:12px;flex-wrap:wrap;color:#eee;font-size:13px";
  for(const filter of data.filters||[]){const label=document.createElement("label");label.textContent=filter.label+" ";const select=document.createElement("select");select.style.cssText="padding:7px;background:#18181a;color:white;border:1px solid #4a4d53;border-radius:5px";select.append(new Option("All",""));for(const value of filter.values)select.append(new Option(value,value));select.value=criteria[filter.field]?.[0]||"";select.onchange=()=>{criteria[filter.field]=select.value?[select.value]:[];shell();};label.append(select);el.append(label);}
  if(!(data.filters||[]).length){el.textContent="Live data · No source-backed dashboard filters configured";}
 }catch(error){el.textContent=(error as Error).message;}}
 function render(el:HTMLElement,data:{rows:Record<string,unknown>[];columns:string[];form_data:Record<string,unknown>;row_limit:number},explicit:Encoding,kind:string){
  el.replaceChildren();if(!data.rows.length){message(el,"No results for this selection.");return;}
  const encoding=resolveEncoding(data.columns,data.rows,data.form_data,explicit);
  const type=explicit.chart_type || (kind==="kpi"?"kpi":String(data.form_data.viz_type||"table"));
  if(type==="map"){const plot=document.createElement("div");plot.style.cssText="height:100%;min-height:210px;width:100%;position:relative";el.append(plot);mountQueryMap(plot,data.rows,encoding);return;}
  if(type==="kpi"||type.startsWith("big_number")){const metric=encoding.metrics[0];if(!metric){message(el,"No numeric metric was returned.");return;}if(data.rows.length!==1){message(el,"A headline metric requires a single full aggregate result.",true);return;}const value=numeric(data.rows[0][metric]);const strong=document.createElement("strong");strong.className="vima-ref-kpi";strong.textContent=value===null?"Not recorded":formatValue(value,encoding.number_format,encoding.unit);el.append(strong);return;}
  if(type==="table" || !/(bar|line|area|pie|donut|scatter)/.test(type)){const wrapper=document.createElement("div");wrapper.className="vima-ref-table";const table=document.createElement("table"),head=table.createTHead().insertRow();for(const col of data.columns){const th=document.createElement("th");th.textContent=col;head.append(th);}const body=table.createTBody();for(const row of data.rows){const tr=body.insertRow();for(const col of data.columns){const td=tr.insertCell();td.textContent=row[col]==null?"Not recorded":String(row[col]);}}wrapper.append(table);el.append(wrapper);return;}
  const plot=document.createElement("div");plot.className="vima-ref-plot";el.append(plot);
  const chart=echarts.init(plot);instances.push(chart);
  const pivot=pivotRows(data.rows,encoding),horizontal=data.form_data.orientation==="horizontal";
  const axis={type:"category",data:pivot.keys,axisLabel:{hideOverlap:true,fontSize:11}};
  const numericAxis={type:"value",name:encoding.unit||"",axisLabel:{formatter:(v:number)=>formatValue(v,encoding.number_format),fontSize:11}};
  const common={color:["#4a6b77","#85a88b","#a07860","#c4a47a","#a9503f"],tooltip:{trigger:"axis"},grid:{left:50,right:18,top:32,bottom:44},legend:{show:pivot.series.length>1,type:"scroll",textStyle:{fontSize:11}}};
  let option:unknown;
  if(/pie|donut/.test(type))option={...common,tooltip:{trigger:"item"},series:[{type:"pie",radius:["42%","66%"],data:data.rows.map(row=>({name:String(row[encoding.dimension]??"Not recorded"),value:numeric(row[encoding.metrics[0]])})),label:{fontSize:11}}]};
  else if(/scatter/.test(type)){if(encoding.metrics.length<2)throw new Error("Scatter charts require two numeric measures.");option={...common,xAxis:{...numericAxis,name:encoding.metrics[0]},yAxis:{...numericAxis,name:encoding.metrics[1]},series:[{type:"scatter",data:data.rows.map(row=>encoding.metrics.slice(0,2).map(metric=>numeric(row[metric])))}]};}
  else option={...common,xAxis:horizontal?numericAxis:encoding.time?{type:"time"}:axis,yAxis:horizontal?axis:numericAxis,series:pivot.series.map(series=>({name:series.name,type:type.includes("bar")?"bar":"line",stack:encoding.stack?"total":undefined,areaStyle:type.includes("area")?{}:undefined,connectNulls:false,data:encoding.time&&!horizontal?pivot.keys.map((key,i)=>[key,series.values[i]]):series.values}))};
  chart.setOption(option as echarts.EChartsCoreOption);const resize=new ResizeObserver(()=>chart.resize());resize.observe(plot);chart.on("finished",()=>{if(!plot.isConnected)resize.disconnect();});
  if(data.rows.length>=data.row_limit){const note=document.createElement("small");note.textContent=`Saved query limited to ${data.row_limit.toLocaleString()} rows.`;el.append(note);}
 }
 shell();
}
