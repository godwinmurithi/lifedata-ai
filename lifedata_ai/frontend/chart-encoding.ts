/** Explicit query-result encodings. Missing values stay missing; duplicate cells
 * are rejected unless the author explicitly declares an aggregation. */
export type Encoding = {dimension?:string; metrics?:string[]; series?:string; time?:boolean; stack?:boolean; number_format?:string; unit?:string; aggregation?:"sum"|"mean"; latitude?:string;longitude?:string;geometry_field?:string;name_field?:string;category_field?:string;chart_type?:"map"|"bar"|"line"|"area"|"pie"|"scatter"|"table"|"kpi"};
export type DataRow = Record<string,unknown>;
export function numeric(value:unknown):number|null {
  if(value===null || value===undefined || value==="" || typeof value==="boolean")return null;
  const n=typeof value==="number"?value:Number(String(value).replace(/,/g,""));
  return Number.isFinite(n)?n:null;
}
export function formatValue(value:number,format?:string,unit?:string):string {
  let result:string;
  const precision=Number(format?.match(/\.(\d+)/)?.[1] ?? (Number.isInteger(value)?0:1));
  if(format?.includes("%"))result=new Intl.NumberFormat(undefined,{style:"percent",maximumFractionDigits:precision}).format(value);
  else if(format?.includes("s"))result=new Intl.NumberFormat(undefined,{notation:"compact",maximumFractionDigits:precision}).format(value);
  else result=new Intl.NumberFormat(undefined,{maximumFractionDigits:Math.min(precision,8)}).format(value);
  return unit ? `${result} ${unit}` : result;
}
export function resolveEncoding(columns:string[], rows:DataRow[], form:Record<string,unknown>, explicit:Encoding={}):Encoding & {dimension:string;metrics:string[]} {
 const groups=Array.isArray(form.groupby)?form.groupby.filter((x):x is string=>typeof x==="string"):[];
 const dimension=explicit.dimension || (typeof form.x_axis==="string"?form.x_axis:groups[0]) || columns[0];
 const metricNames=Array.isArray(form.metrics)?form.metrics.map(x=>typeof x==="string"?x:(x as {label?:string})?.label).filter((x):x is string=>!!x):[];
 const metrics=explicit.metrics || (metricNames.length && metricNames.every(x=>columns.includes(x)) ? metricNames : columns.filter(c=>c!==dimension && !groups.includes(c) && rows.some(r=>numeric(r[c])!==null)));
 const series=explicit.series || groups.find(x=>x!==dimension);
 for(const field of [dimension,...metrics,...(series?[series]:[]),...[explicit.latitude,explicit.longitude,explicit.geometry_field,explicit.name_field,explicit.category_field].filter((x):x is string=>!!x)])if(!columns.includes(field))throw new Error(`The saved query did not return the configured field “${field}”.`);
 return {...explicit,dimension,metrics,series,time:explicit.time ?? (dimension==="__timestamp"),stack:explicit.stack ?? Boolean(form.stack || form.stack_series),number_format:explicit.number_format || (typeof form.y_axis_format==="string"?form.y_axis_format:undefined)};
}
export function pivotRows(rows:DataRow[], encoding:Encoding & {dimension:string;metrics:string[]}) {
 const {dimension,metrics,series,time}=encoding;
 const keys=Array.from(new Set(rows.map(r=>String(r[dimension]??"Not recorded"))));
 if(time)keys.sort((a,b)=>new Date(a).getTime()-new Date(b).getTime());
 const groups=series?Array.from(new Set(rows.map(r=>String(r[series]??"Not recorded")))):[undefined];
 const output=groups.flatMap(group=>metrics.map(metric=>{
  const cells=new Map<string,number[]>();
  for(const row of rows){
   if(series && String(row[series]??"Not recorded")!==group)continue;
   const key=String(row[dimension]??"Not recorded"),value=numeric(row[metric]);
   if(value===null)continue;
   const values=cells.get(key)||[];values.push(value);cells.set(key,values);
  }
  const values=keys.map(key=>{
   const cell=cells.get(key);if(!cell?.length)return null;
   if(cell.length>1 && !encoding.aggregation)throw new Error("Multiple result rows occupy the same chart cell. Include the missing series dimension or declare a justified aggregation.");
   const total=cell.reduce((a,b)=>a+b,0);return encoding.aggregation==="mean"?total/cell.length:total;
  });
  return {name:group===undefined?metric:(metrics.length===1?group:`${group} · ${metric}`),values};
 }));
 return {keys,series:output};
}
