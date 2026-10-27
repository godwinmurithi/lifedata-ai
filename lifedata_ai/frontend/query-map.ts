import L from "leaflet";
import "leaflet/dist/leaflet.css";
import {Encoding,DataRow,numeric} from "./chart-encoding";

/** Render only coordinates/GeoJSON returned by the authorized saved query.
 * Bounds come from data; labels are DOM text, never trusted HTML. */
export function mountQueryMap(container:HTMLElement,rows:DataRow[],encoding:Encoding):()=>void {
 const map=L.map(container,{attributionControl:true,zoomControl:true});
 L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",{attribution:'&copy; OpenStreetMap &copy; CARTO',maxZoom:19}).addTo(map);
 const layers:L.Layer[]=[];let invalid=0;
 for(const row of rows){
  const name=encoding.name_field?row[encoding.name_field]:undefined;
  const label=document.createElement("div");label.textContent=name==null?"Mapped observation":String(name);
  if(encoding.category_field){const category=document.createElement("div");category.textContent=String(row[encoding.category_field]??"Not recorded");label.append(category);}
  if(encoding.geometry_field && row[encoding.geometry_field]){
   try{const raw=row[encoding.geometry_field],geometry=typeof raw==="string"?JSON.parse(raw):raw;const layer=L.geoJSON(geometry as GeoJSON.GeoJsonObject,{style:{color:"#2f6670",weight:2,fillOpacity:.2}}).bindPopup(label);layer.addTo(map);layers.push(layer);continue;}catch{invalid++;}
  }
  const lat=numeric(row[encoding.latitude || "latitude"]),lon=numeric(row[encoding.longitude || "longitude"]);
  if(lat===null || lon===null || Math.abs(lat)>90 || Math.abs(lon)>180){invalid++;continue;}
  const marker=L.circleMarker([lat,lon],{radius:6,color:"#2f6670",fillColor:"#85a88b",fillOpacity:.8,weight:2}).bindPopup(label).addTo(map);layers.push(marker);
 }
 if(layers.length){const bounds=L.featureGroup(layers).getBounds();if(bounds.isValid())map.fitBounds(bounds,{padding:[16,16],maxZoom:13});}
 else {map.setView([0,0],2);const message=document.createElement("div");message.textContent="No verified coordinates or geometry for this selection.";message.style.cssText="position:absolute;z-index:1000;top:45%;left:10%;right:10%;background:white;padding:15px;border-radius:7px;font:13px system-ui";container.append(message);}
 if(invalid){const note=document.createElement("div");note.textContent=`${invalid.toLocaleString()} observations without usable geography`;note.style.cssText="position:absolute;bottom:18px;left:8px;z-index:500;background:#ffffffdd;padding:4px;font:11px system-ui";container.append(note);}
 const resize=new ResizeObserver(()=>map.invalidateSize());resize.observe(container);
 return()=>{resize.disconnect();map.remove();};
}
