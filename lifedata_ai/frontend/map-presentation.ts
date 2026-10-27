import L from "leaflet";
export type SymbolStyle={shape?:"circle"|"square"|"diamond"|"pin"|"icon";icon?:"none"|"hospital"|"clinic"|"person"|"home"|"laboratory"|"water"|"school"|"star";color?:string;radius?:number};
export type MapStyle={basemap?:"street"|"satellite";markers?:SymbolStyle & {outline_color?:string;outline_width?:number;opacity?:number;category_field?:string;categories?:Record<string,SymbolStyle>};boundaries?:{color?:string;fill_color?:string;selected_color?:string;weight?:number;fill_opacity?:number};popup?:{title_field?:string;hide_empty?:boolean;show_select?:boolean;fields?:{field:string;label:string;format?:"text"|"number"|"boolean"}[]}};
const paths:Record<string,string>={hospital:"M27 19h10v10h10v10H37v10H27V39H17V29h10z",clinic:"M27 19h10v10h10v10H37v10H27V39H17V29h10z",person:"M32 16a8 8 0 1 0 0 16 8 8 0 0 0 0-16M20 49V39q12-10 24 0v10z",home:"M14 31l18-16 18 16h-5v19H19V31z",laboratory:"M25 15h14v5h-3v12l12 18H16l12-18V20h-3z",water:"M32 13C27 23 18 32 18 40a14 14 0 0 0 28 0c0-8-9-17-14-27z",school:"M15 24l17-10 17 10v27H15zM27 33h10v18H27z",star:"M32 12l6 13 15 2-11 10 3 15-13-7-13 7 3-15-11-10 15-2z"};
export function symbolStyle(row:Record<string,unknown>,style:MapStyle,defaultColor:string,defaultRadius:number):SymbolStyle {
 const markers=style.markers || {},category=markers.category_field ? String(row[markers.category_field] ?? "") : "";
 return {color:defaultColor,radius:defaultRadius,...markers,...markers.categories?.[category]};
}
export function makeMarker(row:Record<string,unknown>,style:MapStyle,color:string,radius:number):L.Layer {
 const symbol=symbolStyle(row,style,color,radius),options=style.markers || {};
 const fill=symbol.color || color,r=symbol.radius || radius,outline=options.outline_color || "#ffffff",weight=options.outline_width ?? 2,opacity=options.opacity ?? 1;
 const point:L.LatLngExpression=[Number(row.lat),Number(row.lng)];
 const hasIcon=symbol.icon==="none"?false:Boolean(symbol.icon || symbol.shape==="icon");
 if((!symbol.shape || symbol.shape==="circle")&&!hasIcon)return L.circleMarker(point,{radius:r,color:outline,weight,fillColor:fill,fillOpacity:opacity,className:"pd-facility-marker"});
 const node=document.createElement("div"),svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
 svg.setAttribute("viewBox","0 0 64 64");svg.setAttribute("width","100%");svg.setAttribute("height","100%");svg.setAttribute("aria-hidden","true");svg.style.opacity=String(opacity);
 const shape=document.createElementNS(svg.namespaceURI,"path");
 shape.setAttribute("d",symbol.shape==="square"?"M10 10h44v44H10z":symbol.shape==="diamond"?"M32 3L61 32 32 61 3 32z":symbol.shape==="pin"?"M32 61C23 47 9 35 9 25a23 23 0 1 1 46 0c0 10-14 22-23 36z":"M32 3a29 29 0 1 0 0 58 29 29 0 0 0 0-58");
 shape.setAttribute("fill",fill);shape.setAttribute("stroke",outline);shape.setAttribute("stroke-width",String(weight*2));svg.append(shape);
 if(hasIcon){const path=document.createElementNS(svg.namespaceURI,"path");path.setAttribute("d",paths[symbol.icon || "hospital"]);path.setAttribute("fill",outline);svg.append(path);}
 node.append(svg);const size=2*r+8;
 return L.marker(point,{icon:L.divIcon({html:node,className:"pd-facility-marker vima-symbol-marker",iconSize:[size,size],iconAnchor:[size/2,symbol.shape==="pin"?size:size/2],popupAnchor:[0,-size/2]}),keyboard:true,alt:String(row.facility_name || "Map location")});
}
export function popupContent(row:Record<string,unknown>,style:MapStyle,onSelect:()=>void):HTMLElement {
 const popup=style.popup || {},node=document.createElement("div"),heading=document.createElement("strong");node.className="vima-map-popup";
 heading.textContent=String(row[popup.title_field || "facility_name"] ?? "Not recorded");node.append(heading);
 const fields=popup.fields || [{field:"facility_level",label:"Level"},{field:"beds",label:"Beds",format:"number" as const},{field:"subcounty",label:"Subcounty"}];
 const list=document.createElement("dl");
 fields.forEach(item=>{const value=row[item.field],empty=value==null || value==="";if(empty&&popup.hide_empty)return;const term=document.createElement("dt"),definition=document.createElement("dd");term.textContent=item.label;
 definition.textContent=empty?"Not recorded":item.format==="number"&&Number.isFinite(Number(value))?Number(value).toLocaleString():item.format==="boolean"&&typeof value==="boolean"?(value?"True":"False"):Array.isArray(value)?value.map(String).join(", "):String(value);list.append(term,definition);});node.append(list);
 if(popup.show_select!==false){const button=document.createElement("button");button.type="button";button.textContent="Select facility";button.onclick=onSelect;node.append(button);}
 return node;
}
