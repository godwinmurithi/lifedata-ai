import React,{useEffect,useRef,useState} from "react";
import L from "leaflet";
import {facilityAppearance,facilityLevelStyles} from "./population";
import {MapStyle,makeMarker,popupContent,symbolStyle} from "./map-presentation";

type Facility={facility_name:string;facility_level?:string;beds?:number|null;lat:number;lng:number;county?:string;subcounty?:string};
type Boundary={area_level:string;area_name:string;field:string;label?:string;parents?:Record<string,string>;geometry_geojson:any;representative_point?:{lat:number;lng:number}};
export type Geography={facilities:Facility[];all_facilities:Facility[];boundaries:Boundary[];counts:Record<string,number>;hierarchy?:{field:string;label:string}[];notes:string[];truncated:boolean;source_chart_ids:number[]};
export default function GeographicMap({geography,title,selected,selectArea,selectFacility,mapStyle={}}:{geography?:Geography|null;title:string;selected:Record<string,string[]>;selectArea:(field:string,value:string,parents?:Record<string,string>)=>void;selectFacility:(name:string)=>void;mapStyle?:MapStyle}) {
 const host=useRef<HTMLDivElement>(null),map=useRef<L.Map>(),groups=useRef<Record<string,L.LayerGroup>>({}),control=useRef<L.Control.Layers>(),facilities=useRef<L.LayerGroup>(),fitted=useRef(false);
 const [tilesFailed,setTilesFailed]=useState(false);
 const boundaryLayers=useRef(new Map<string,{signature:string;polygon:L.GeoJSON;pin?:L.CircleMarker;field:string}>());
 const callbacks=useRef({selectArea,selectFacility});callbacks.current={selectArea,selectFacility};
 const styles=facilityLevelStyles(geography?.all_facilities || []);
 const markerLegend:Record<string,string>={};
 geography?.all_facilities.forEach(f=>{const a=facilityAppearance(f,styles),s=symbolStyle(f,mapStyle,a.color,a.radius),field=mapStyle.markers?.category_field;
 const label=field?String((f as Record<string,unknown>)[field] ?? "Not recorded"):mapStyle.markers?.color?"Locations":a.label;markerLegend[label]=s.color || a.color;});
 const levels=geography?.hierarchy || [{field:"county",label:"County"},{field:"subcounty",label:"Subcounty"}];
 useEffect(()=>{
  if(!host.current)return;
  const instance=L.map(host.current).setView([0,0],2);map.current=instance;
  const streets=L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',maxZoom:19});
  const satellite=L.tileLayer("https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{attribution:"Esri, Vantor, Earthstar Geographics, GIS User Community",maxZoom:19});
  (mapStyle.basemap==="street"?streets:satellite).addTo(instance);fitted.current=false;
  [streets,satellite].forEach(layer=>layer.on("tileerror",()=>setTilesFailed(true)).on("tileload",()=>setTilesFailed(false)));
  facilities.current=L.layerGroup().addTo(instance);
  control.current=L.control.layers({Satellite:satellite,"Street map":streets},{"Facility locations":facilities.current},{position:"topright"}).addTo(instance);
  const observer=new ResizeObserver(()=>instance.invalidateSize());observer.observe(host.current);
  return()=>{observer.disconnect();instance.remove();groups.current={};boundaryLayers.current.clear();};
 },[mapStyle.basemap]);
 function fit(){const bounds=L.latLngBounds([]);geography?.boundaries.forEach(b=>bounds.extend(L.geoJSON(b.geometry_geojson).getBounds()));geography?.facilities.forEach(f=>bounds.extend([f.lat,f.lng]));if(bounds.isValid())map.current?.fitBounds(bounds,{padding:[30,30],maxZoom:13,animate:false});}
 useEffect(()=>{
  if(!map.current || !facilities.current || !control.current)return;
  Object.entries(groups.current).forEach(([field,layer])=>{if(!levels.some(l=>l.field===field)){control.current?.removeLayer(layer);map.current?.removeLayer(layer);delete groups.current[field];}});
  levels.forEach(level=>{if(!groups.current[level.field]){const layer=L.layerGroup().addTo(map.current!);groups.current[level.field]=layer;control.current?.addOverlay(layer,`${level.label} boundaries`);}});
  facilities.current.clearLayers();
  const visibleBoundaries=new Set<string>();
  geography?.boundaries.forEach(area=>{
   const key=JSON.stringify([area.field,area.area_name,area.parents]);visibleBoundaries.add(key);
   const signature=JSON.stringify([area.geometry_geojson,area.representative_point,mapStyle.boundaries,levels]);
   const active=selected[area.field]?.some(v=>v.toLowerCase()===area.area_name.toLowerCase());
   const depth=Math.max(0,levels.findIndex(l=>l.field===area.field)),outer=depth===0;
   const node=document.createElement("div");node.textContent=`${area.label || levels[depth]?.label || area.area_level}: ${area.area_name} · Click to select`;
   const layer=groups.current[area.field];if(!layer)return;
   const style={color:active?(mapStyle.boundaries?.selected_color || "#a43455"):mapStyle.boundaries?.color || ["#374b65","#527ca3","#7196a3","#58a597","#91a756"][Math.min(depth,4)],weight:active?3:mapStyle.boundaries?.weight ?? (outer?3:1.5),dashArray:outer?"7 4":undefined,fill:!outer || mapStyle.boundaries?.fill_opacity!=null,fillColor:active?(mapStyle.boundaries?.selected_color || "#b33f5d"):(mapStyle.boundaries?.fill_color || "#60b7aa"),fillOpacity:active?.18:(mapStyle.boundaries?.fill_opacity ?? .04),className:`pd-${area.field}-boundary`};
   const cached=boundaryLayers.current.get(key);
   if(cached?.signature===signature){cached.polygon.setStyle(style);cached.pin?.setStyle({fillColor:active?(mapStyle.boundaries?.selected_color || "#a43455"):(mapStyle.boundaries?.color || "#2E7D32")});return;}
   if(cached){layer.removeLayer(cached.polygon);if(cached.pin)layer.removeLayer(cached.pin);}
   const polygon=L.geoJSON(area.geometry_geojson,{style,onEachFeature:(_,shape)=>shape.bindTooltip(node).on("click",()=>callbacks.current.selectArea(area.field,area.area_name,area.parents))}).addTo(layer);
   const entry:{signature:string;polygon:L.GeoJSON;pin?:L.CircleMarker;field:string}={signature,polygon,field:area.field};boundaryLayers.current.set(key,entry);
   // One marker per area that actually has a submission (every entry here
   // does — see administrative.py's `covered()` guard) at a GEOS
   // `point_on_surface`, i.e. guaranteed to sit inside the polygon rather
   // than a plain centroid, which can land outside a concave or multi-part
   // shape. Same click handler as the polygon itself, so a click here
   // cross-filters the dashboard exactly like clicking the boundary does.
   if(area.representative_point){
    const pin=document.createElement("div");pin.textContent=`${area.label || levels[depth]?.label || area.area_level}: ${area.area_name} · Click to select`;
    entry.pin=L.circleMarker([area.representative_point.lat,area.representative_point.lng],{radius:outer?7:5,color:"#ffffff",weight:2,fillColor:active?(mapStyle.boundaries?.selected_color || "#a43455"):(mapStyle.boundaries?.color || "#2E7D32"),fillOpacity:1,className:`pd-${area.field}-marker`}).bindTooltip(pin).on("click",()=>callbacks.current.selectArea(area.field,area.area_name,area.parents)).addTo(layer);
   }
  });
  boundaryLayers.current.forEach((entry,key)=>{if(!visibleBoundaries.has(key)){groups.current[entry.field]?.removeLayer(entry.polygon);if(entry.pin)groups.current[entry.field]?.removeLayer(entry.pin);boundaryLayers.current.delete(key);}});
  geography?.facilities.forEach(f=>{
   const appearance=facilityAppearance(f,styles),node=popupContent(f,mapStyle,()=>selectFacility(f.facility_name));
   const tooltip=document.createElement("strong");tooltip.textContent=`${f.facility_name} · ${appearance.label} · ${appearance.beds==null?"Beds not recorded":`${appearance.beds} beds`}`;
   makeMarker(f,mapStyle,appearance.color,appearance.radius).bindTooltip(tooltip).bindPopup(node).addTo(facilities.current!);
  });
  if(!fitted.current&&geography&&(geography.boundaries.length || geography.facilities.length)){fitted.current=true;fit();}
 },[geography,JSON.stringify(selected),JSON.stringify(mapStyle)]);
 return <section className="pd-map-card pd-card" data-source-chart-ids={geography?.source_chart_ids.join(",")}><div className="pd-card-heading"><div><span className="pd-eyebrow">GEOGRAPHIC DISCOVERY</span><h2>{title} · Facilities & boundaries</h2></div><button onClick={fit}>Fit map</button></div><div ref={host} className="pd-map" aria-label="Facility locations with administrative boundaries"/><div className="pd-map-legend"><span>{geography?.facilities.length ?? "—"} facility locations</span><span><i className="pd-polygon"/>{levels.map(l=>`${l.label}: ${geography?.counts[l.field] ?? 0}`).join(" · ")}</span>{Object.entries(markerLegend).map(([key,color])=><span key={key}><i className="pd-facility-key" style={{backgroundColor:color}}/>{key}</span>)}<span>Size: {mapStyle.markers?.radius ? "fixed radius" : "level + beds"} · {mapStyle.markers?.shape || "circle"} {mapStyle.markers?.icon || ""} · Click to select</span></div>{tilesFailed&&<p className="pd-inline-note">Background tiles unavailable. Facility locations and boundaries remain interactive.</p>}</section>;
}
