import React,{useEffect,useRef,useState} from "react";
import L from "leaflet";
import {facilityAppearance,facilityLevelStyles} from "./population";

type Facility={facility_name:string;facility_level?:string;beds?:number|null;lat:number;lng:number;county?:string;subcounty?:string};
type Boundary={area_level:string;area_name:string;field:string;label?:string;parents?:Record<string,string>;geometry_geojson:any};
export type Geography={facilities:Facility[];all_facilities:Facility[];boundaries:Boundary[];counts:Record<string,number>;hierarchy?:{field:string;label:string}[];notes:string[];truncated:boolean;source_chart_ids:number[]};
export default function GeographicMap({geography,title,selected,selectArea,selectFacility}:{geography?:Geography|null;title:string;selected:Record<string,string[]>;selectArea:(field:string,value:string,parents?:Record<string,string>)=>void;selectFacility:(name:string)=>void}) {
 const host=useRef<HTMLDivElement>(null),map=useRef<L.Map>(),groups=useRef<Record<string,L.LayerGroup>>({}),control=useRef<L.Control.Layers>(),facilities=useRef<L.LayerGroup>(),fitted=useRef(false);
 const [tilesFailed,setTilesFailed]=useState(false);
 const styles=facilityLevelStyles(geography?.all_facilities || []);
 const levels=geography?.hierarchy || [{field:"county",label:"County"},{field:"subcounty",label:"Subcounty"}];
 useEffect(()=>{
  if(!host.current)return;
  const instance=L.map(host.current).setView([0,0],2);map.current=instance;
  const streets=L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',maxZoom:19});
  const satellite=L.tileLayer("https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{attribution:"Esri, Vantor, Earthstar Geographics, GIS User Community",maxZoom:19}).addTo(instance);
  [streets,satellite].forEach(layer=>layer.on("tileerror",()=>setTilesFailed(true)).on("tileload",()=>setTilesFailed(false)));
  facilities.current=L.layerGroup().addTo(instance);
  control.current=L.control.layers({Satellite:satellite,"Street map":streets},{"Facility locations":facilities.current},{position:"topright"}).addTo(instance);
  const observer=new ResizeObserver(()=>instance.invalidateSize());observer.observe(host.current);
  return()=>{observer.disconnect();instance.remove();groups.current={};};
 },[]);
 function fit(){const bounds=L.latLngBounds([]);geography?.boundaries.forEach(b=>bounds.extend(L.geoJSON(b.geometry_geojson).getBounds()));geography?.facilities.forEach(f=>bounds.extend([f.lat,f.lng]));if(bounds.isValid())map.current?.fitBounds(bounds,{padding:[30,30],maxZoom:13,animate:false});}
 useEffect(()=>{
  if(!map.current || !facilities.current || !control.current)return;
  Object.entries(groups.current).forEach(([field,layer])=>{if(!levels.some(l=>l.field===field)){control.current?.removeLayer(layer);map.current?.removeLayer(layer);delete groups.current[field];}else layer.clearLayers();});
  levels.forEach(level=>{if(!groups.current[level.field]){const layer=L.layerGroup().addTo(map.current!);groups.current[level.field]=layer;control.current?.addOverlay(layer,`${level.label} boundaries`);}});
  facilities.current.clearLayers();
  geography?.boundaries.forEach(area=>{
   const active=selected[area.field]?.some(v=>v.toLowerCase()===area.area_name.toLowerCase());
   const depth=Math.max(0,levels.findIndex(l=>l.field===area.field)),outer=depth===0;
   const node=document.createElement("div");node.textContent=`${area.label || levels[depth]?.label || area.area_level}: ${area.area_name} · Click to select`;
   const layer=groups.current[area.field];if(!layer)return;
   L.geoJSON(area.geometry_geojson,{style:{color:active?"#a43455":["#374b65","#527ca3","#7196a3","#58a597","#91a756"][Math.min(depth,4)],weight:active?3:outer?3:1.5,dashArray:outer?"7 4":undefined,fill:!outer,fillColor:active?"#b33f5d":"#60b7aa",fillOpacity:active?.18:.04,className:`pd-${area.field}-boundary`},onEachFeature:(_,shape)=>shape.bindTooltip(node).on("click",()=>selectArea(area.field,area.area_name,area.parents))}).addTo(layer);
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
 return <section className="pd-map-card pd-card" data-source-chart-ids={geography?.source_chart_ids.join(",")}><div className="pd-card-heading"><div><span className="pd-eyebrow">GEOGRAPHIC DISCOVERY</span><h2>{title} · Facilities & boundaries</h2></div><button onClick={fit}>Fit map</button></div><div ref={host} className="pd-map" aria-label="Facility locations with administrative boundaries"/><div className="pd-map-legend"><span>{geography?.facilities.length ?? "—"} facility locations</span><span><i className="pd-polygon"/>{levels.map(l=>`${l.label}: ${geography?.counts[l.field] ?? 0}`).join(" · ")}</span>{Object.entries(styles).map(([key,style])=><span key={key}><i className="pd-facility-key" style={{backgroundColor:style.color}}/>{style.label}</span>)}<span>Size: level + beds · Click to select</span></div>{geography?.notes.map(note=><p className="pd-inline-note" key={note}>{note}</p>)}{tilesFailed&&<p className="pd-inline-note">Background tiles unavailable. Facility locations and boundaries remain interactive.</p>}</section>;
}
