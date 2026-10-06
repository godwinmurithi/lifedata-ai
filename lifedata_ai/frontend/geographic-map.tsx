import React,{useEffect,useRef,useState} from "react";
import L from "leaflet";
import {facilityAppearance,facilityLevelStyles} from "./population";

type Facility={facility_name:string;facility_level?:string;beds?:number|null;lat:number;lng:number;county?:string;subcounty?:string};
type Boundary={area_level:"county"|"subcounty";area_name:string;field:string;geometry_geojson:any};
export type Geography={facilities:Facility[];all_facilities:Facility[];boundaries:Boundary[];counts:{county:number;subcounty:number};notes:string[];truncated:boolean;source_chart_ids:number[]};
export default function GeographicMap({geography,title,selected,selectArea,selectFacility}:{geography?:Geography|null;title:string;selected:Record<string,string[]>;selectArea:(field:string,value:string)=>void;selectFacility:(name:string)=>void}) {
 const host=useRef<HTMLDivElement>(null),map=useRef<L.Map>(),counties=useRef<L.LayerGroup>(),subcounties=useRef<L.LayerGroup>(),facilities=useRef<L.LayerGroup>(),fitted=useRef(false);
 const [tilesFailed,setTilesFailed]=useState(false);
 const styles=facilityLevelStyles(geography?.all_facilities || []);
 useEffect(()=>{
  if(!host.current)return;
  const instance=L.map(host.current).setView([0,0],2);map.current=instance;
  const streets=L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',maxZoom:19});
  const satellite=L.tileLayer("https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{attribution:"Esri, Vantor, Earthstar Geographics, GIS User Community",maxZoom:19}).addTo(instance);
  [streets,satellite].forEach(layer=>layer.on("tileerror",()=>setTilesFailed(true)).on("tileload",()=>setTilesFailed(false)));
  counties.current=L.layerGroup().addTo(instance);subcounties.current=L.layerGroup().addTo(instance);facilities.current=L.layerGroup().addTo(instance);
  L.control.layers({Satellite:satellite,"Street map":streets},{"County boundaries":counties.current,"Subcounty boundaries":subcounties.current,"Facility locations":facilities.current},{position:"topright"}).addTo(instance);
  const observer=new ResizeObserver(()=>instance.invalidateSize());observer.observe(host.current);
  return()=>{observer.disconnect();instance.remove();};
 },[]);
 function fit(){const bounds=L.latLngBounds([]);geography?.boundaries.forEach(b=>bounds.extend(L.geoJSON(b.geometry_geojson).getBounds()));geography?.facilities.forEach(f=>bounds.extend([f.lat,f.lng]));if(bounds.isValid())map.current?.fitBounds(bounds,{padding:[30,30],maxZoom:13});}
 useEffect(()=>{
  if(!counties.current || !subcounties.current || !facilities.current)return;
  [counties.current,subcounties.current,facilities.current].forEach(layer=>layer.clearLayers());
  geography?.boundaries.forEach(area=>{
   const active=selected[area.field]?.some(v=>v.toLowerCase()===area.area_name.toLowerCase());
   const isCounty=area.area_level==="county";
   const node=document.createElement("div");node.textContent=`${isCounty?"County":"Subcounty"}: ${area.area_name} · Click to select`;
   L.geoJSON(area.geometry_geojson,{style:{color:active?"#a43455":isCounty?"#374b65":"#7196a3",weight:active?3:isCounty?3:1.5,dashArray:isCounty?"7 4":undefined,fill:!isCounty,fillColor:active?"#b33f5d":"#60b7aa",fillOpacity:active?.18:.07,className:isCounty?"pd-county-boundary":"pd-subcounty-boundary"},onEachFeature:(_,layer)=>layer.bindTooltip(node).on("click",()=>selectArea(area.field,area.area_name))}).addTo(isCounty?counties.current!:subcounties.current!);
  });
  geography?.facilities.forEach(f=>{
   const appearance=facilityAppearance(f,styles),node=document.createElement("div"),name=document.createElement("strong"),detail=document.createElement("p"),button=document.createElement("button");
   name.textContent=f.facility_name;detail.textContent=`${appearance.label} · ${appearance.beds==null?"Beds not recorded":`${appearance.beds} beds`} · ${f.subcounty || "Subcounty not recorded"}`;
   button.textContent="Select facility";button.onclick=()=>selectFacility(f.facility_name);node.append(name,detail,button);
   const tooltip=document.createElement("strong");tooltip.textContent=`${f.facility_name} · ${appearance.label} · ${appearance.beds==null?"Beds not recorded":`${appearance.beds} beds`}`;
   L.circleMarker([f.lat,f.lng],{radius:appearance.radius,color:"#fff",weight:2,fillColor:appearance.color,fillOpacity:1,className:"pd-facility-marker"}).bindTooltip(tooltip).bindPopup(node).addTo(facilities.current!);
  });
  if(!fitted.current&&geography&&(geography.boundaries.length || geography.facilities.length)){fitted.current=true;fit();}
 },[geography,JSON.stringify(selected)]);
 return <section className="pd-map-card pd-card" data-source-chart-ids={geography?.source_chart_ids.join(",")}><div className="pd-card-heading"><div><span className="pd-eyebrow">GEOGRAPHIC DISCOVERY</span><h2>{title} · Facilities & boundaries</h2></div><button onClick={fit}>Fit map</button></div><div ref={host} className="pd-map" aria-label="Facility locations with county and subcounty boundaries"/><div className="pd-map-legend"><span>{geography?.facilities.length ?? "—"} facility locations</span><span><i className="pd-polygon"/>{geography?.counts.county ?? "—"} county · {geography?.counts.subcounty ?? "—"} subcounties</span>{Object.entries(styles).map(([key,style])=><span key={key}><i className="pd-facility-key" style={{backgroundColor:style.color}}/>{style.label}</span>)}<span>Size: level + beds · Click to select</span></div>{geography?.notes.map(note=><p className="pd-inline-note" key={note}>{note}</p>)}{tilesFailed&&<p className="pd-inline-note">Background tiles unavailable. Facility locations and boundaries remain interactive.</p>}</section>;
}
