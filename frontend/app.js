let map, userMarker, routeLayers = [], selectedIdx = -1, routesData = [];
let targetKm = 10, profileChart = null;
let userPos = null;  // {lat, lon} once granted
let hoverMarker = null;   // marker on map linked to elevation-chart hover
let arrowLayers = [];     // direction arrows for the selected route

const COLORS = ["#4ade80","#38bdf8","#fbbf24","#f472b6","#a78bfa","#fb923c","#34d399","#e879f9"];

function initMap(){
  map = L.map("map",{zoomControl:true}).setView([46.2,6.15],13);
  L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",{
    maxZoom:17, attribution:"© OpenTopoMap, © OpenStreetMap"
  }).addTo(map);
}

function setStatus(msg, isErr){
  const el = document.getElementById("status");
  el.innerHTML = msg; el.className = "status" + (isErr ? " err" : "");
}

document.getElementById("distButtons").addEventListener("click", e=>{
  if(e.target.tagName!=="BUTTON") return;
  document.querySelectorAll("#distButtons button").forEach(b=>b.classList.remove("active"));
  e.target.classList.add("active");
  targetKm = +e.target.dataset.km;
});

document.getElementById("findBtn").addEventListener("click", findRoutes);

// Ask for location. iOS Safari requires (a) a secure context (https or
// localhost) and (b) a user gesture — so we call this from the button tap.
function requestLocation(onOk, onErr){
  if(!navigator.geolocation){
    setStatus("Geolocation isn’t supported by this browser.", true);
    onErr && onErr(); return;
  }
  // Secure-context check: iOS Safari silently blocks geolocation over plain HTTP.
  if(window.isSecureContext === false){
    setStatus("Location needs a secure connection. Open this page over "+
      "<b>https://</b> (or on the computer via <b>localhost</b>).", true);
    onErr && onErr(); return;
  }
  setStatus('<span class="spinner"></span>Getting your location…');
  navigator.geolocation.getCurrentPosition(pos=>{
    userPos = {lat: pos.coords.latitude, lon: pos.coords.longitude};
    showUser(userPos.lat, userPos.lon);
    onOk && onOk(userPos);
  }, err=>{
    let msg = "Couldn’t get your location.";
    if(err.code === 1) msg = "Location permission denied. Enable it in Settings → "+
      "Safari → Location, then tap “Find my routes” again.";
    else if(err.code === 2) msg = "Location unavailable right now. Try again outdoors.";
    else if(err.code === 3) msg = "Location timed out. Tap “Find my routes” to retry.";
    setStatus(msg, true);
    onErr && onErr(err);
  }, {enableHighAccuracy:true, timeout:15000, maximumAge:60000});
}

function findRoutes(){
  const btn = document.getElementById("findBtn");
  btn.disabled = true;
  if(userPos){
    fetchRoutes(userPos.lat, userPos.lon);
    return;
  }
  requestLocation(pos=>{
    fetchRoutes(pos.lat, pos.lon);
  }, ()=>{ btn.disabled = false; });
}

function showUser(lat, lon){
  map.setView([lat,lon], 14);
  if(userMarker) map.removeLayer(userMarker);
  userMarker = L.circleMarker([lat,lon],{radius:8,color:"#fff",weight:2,
    fillColor:"#4ade80",fillOpacity:1}).addTo(map).bindPopup("You are here");
}

async function fetchRoutes(lat, lon){
  setStatus('<span class="spinner"></span>Searching paths & building '+targetKm+' km loops…');
  try{
    const r = await fetch(`/api/routes?lat=${lat}&lon=${lon}&distance_km=${targetKm}`);
    const data = await r.json();
    if(data.error || !data.routes || !data.routes.length){
      setStatus(data.error || "No routes found here.", true);
      document.getElementById("findBtn").disabled = false;
      return;
    }
    routesData = data.routes;
    const src = data.source === "mock" ? " ⚠︎ demo network (no OSM data reachable)" : "";
    const flatNote = data.elevation_limited
      ? " · ⚠ flat-route mode limited (no local DEM)" : "";
    setStatus(`Found ${data.routes.length} routes · ${data.source} · elev: ${data.elevation_source||"n/a"}${src}${flatNote}`);
    renderRoutes();
    drawAllRoutes();
    selectRoute(0);
    if(isMobile()) setSheet("half");   // reveal the route cards on mobile
  }catch(e){
    setStatus("Error: "+e.message, true);
  }
  document.getElementById("findBtn").disabled = false;
}

function bar(label, val, color){
  const pct = Math.round(val*100);
  return `<div class="bar-row"><span class="lbl">${label}</span>
    <span class="bar-track"><span class="bar-fill" style="width:${pct}%;background:${color}"></span></span>
    <span class="val">${pct}%</span></div>`;
}

function renderRoutes(){
  const list = document.getElementById("routeList");
  list.innerHTML = "";
  routesData.forEach((rt, i)=>{
    const m = rt.metrics, b = rt.breakdown, c = COLORS[i%COLORS.length];
    const km = (m.distance_m/1000).toFixed(1);
    const div = document.createElement("div");
    div.className = "card"; div.dataset.idx = i;
    div.style.borderLeft = `4px solid ${c}`;
    div.innerHTML = `
      <div class="cat">${rt.category}${rt.shape==="out-and-back"?' <span class="shape-badge">out &amp; back</span>':''}</div>
      <div class="head">
        <div class="main-stat">${km}<small> km</small> · +${Math.round(m.elevation_gain_m)}<small> m</small></div>
        <div class="score">${rt.score}<small>/100</small></div>
      </div>
      <div class="bars">
        ${bar("Nature", m.nature_pct, c)}
        ${bar("Flatness", b.flatness_display!=null?b.flatness_display:b.elevation, "#38bdf8")}
      </div>
      <div class="chips">
        <span class="chip">Roads <b>${Math.round(m.road_pct*100)}%</b></span>
        <span class="chip">Start <b>${Math.round(m.start_distance_m)} m</b></span>
        <span class="chip">${b.difficulty}</span>
      </div>`;
    div.addEventListener("click", ()=>{ selectRoute(i); if(isMobile()) setSheet("open"); });
    list.appendChild(div);
  });
}

function drawAllRoutes(){
  routeLayers.forEach(l=>map.removeLayer(l));
  routeLayers = [];
  arrowLayers.forEach(l=>map.removeLayer(l));
  arrowLayers = [];
  if(typeof endpointMarkers!=="undefined"){ endpointMarkers.forEach(m=>map.removeLayer(m)); endpointMarkers=[]; }
  if(hoverMarker){ map.removeLayer(hoverMarker); hoverMarker=null; }
  routesData.forEach((rt,i)=>{
    const c = COLORS[i%COLORS.length];
    const line = L.polyline(rt.coords,{color:c,weight:4,opacity:.55}).addTo(map);
    line.on("click",()=>selectRoute(i));
    routeLayers.push(line);
  });
}

function selectRoute(i){
  selectedIdx = i;
  document.querySelectorAll(".card").forEach(el=>
    el.classList.toggle("sel", +el.dataset.idx===i));
  routeLayers.forEach((l,j)=>{
    l.setStyle({opacity: j===i?1:.3, weight: j===i?6:4});
    if(j===i) l.bringToFront();
  });
  const rt = routesData[i];
  // on mobile the bottom sheet covers the lower part of the map; pad the fit so
  // the whole route stays visible above the sheet.
  const fitOpts = isMobile()
    ? {paddingTopLeft:[30,60], paddingBottomRight:[30, Math.round(window.innerHeight*0.45)]}
    : {padding:[40,40]};
  map.fitBounds(routeLayers[i].getBounds(), fitOpts);
  drawDirectionArrows(rt.coords, COLORS[i%COLORS.length]);
  drawEndpoints(rt);
  showDetail(rt, i);
}

let endpointMarkers = [];
function drawEndpoints(rt){
  endpointMarkers.forEach(m=>map.removeLayer(m));
  endpointMarkers = [];
  const coords = rt.coords;
  if(coords.length < 2) return;
  const startPt = coords[0];
  // for out-and-back the "end" of interest is the turnaround (farthest point);
  // for a loop, the end coincides with start, so mark the mid/farthest point red.
  let farIdx = 0, farD = 0;
  for(let k=0;k<coords.length;k++){
    const d = (coords[k][0]-startPt[0])**2 + (coords[k][1]-startPt[1])**2;
    if(d>farD){ farD=d; farIdx=k; }
  }
  const mk = (pt, cls, label) => L.marker(pt, {icon: L.divIcon({
    className:"endpoint-icon",
    html:`<div class="endpoint-pin ${cls}"><span>${label}</span></div>`,
    iconSize:[26,34], iconAnchor:[13,34]})}).addTo(map);
  endpointMarkers.push(mk(startPt, "start-pin", "S"));
  const endLabel = rt.shape==="out-and-back" ? "T" : "½";
  endpointMarkers.push(mk(coords[farIdx], "end-pin", endLabel));
}

// ---- direction arrows along the selected route ----
function bearingDeg(a, b){
  const toRad = d=>d*Math.PI/180, toDeg = r=>r*180/Math.PI;
  const dLon = toRad(b[1]-a[1]);
  const y = Math.sin(dLon)*Math.cos(toRad(b[0]));
  const x = Math.cos(toRad(a[0]))*Math.sin(toRad(b[0])) -
            Math.sin(toRad(a[0]))*Math.cos(toRad(b[0]))*Math.cos(dLon);
  return (toDeg(Math.atan2(y,x))+360)%360;
}

function drawDirectionArrows(coords, color){
  arrowLayers.forEach(l=>map.removeLayer(l));
  arrowLayers = [];
  if(coords.length < 2) return;
  // place an arrow roughly every ~7% of the points (min spacing)
  const step = Math.max(3, Math.floor(coords.length/14));
  for(let k=step; k<coords.length-1; k+=step){
    const p = coords[k], nxt = coords[k+1];
    const ang = bearingDeg(p, nxt);
    const icon = L.divIcon({
      className: "dir-arrow",
      html: `<div class="arrow-glyph" style="transform:rotate(${ang}deg);color:${color}">➤</div>`,
      iconSize: [18,18], iconAnchor: [9,9]
    });
    arrowLayers.push(L.marker(p, {icon, interactive:false}).addTo(map));
  }
}

function showDetail(rt, i){
  const m = rt.metrics, c = COLORS[i%COLORS.length];
  const d = document.getElementById("detail");
  d.classList.remove("hidden");
  const start = rt.start;
  const gmap = `https://www.google.com/maps/dir/?api=1&travelmode=walking&destination=${start[0]},${start[1]}`;
  d.innerHTML = `
    <h3>${rt.category} — ${rt.score}/100</h3>
    <div class="detail-grid">
      <div class="dstat"><div class="k">Distance</div><div class="v">${(m.distance_m/1000).toFixed(2)} km</div></div>
      <div class="dstat"><div class="k">Elev gain</div><div class="v">+${Math.round(m.elevation_gain_m)} m</div></div>
      <div class="dstat"><div class="k">Elev loss</div><div class="v">−${Math.round(m.elevation_loss_m)} m</div></div>
      <div class="dstat"><div class="k">Highest pt</div><div class="v">${Math.round(m.highest_m)} m</div></div>
      <div class="dstat"><div class="k">Nature</div><div class="v">${Math.round(m.nature_pct*100)}%</div></div>
      <div class="dstat"><div class="k">Start away</div><div class="v">${Math.round(m.start_distance_m)} m</div></div>
    </div>
    <div class="profile-wrap"><canvas id="profileChart"></canvas></div>
    <div class="detail-actions">
      <a class="ext-btn" href="${gmap}" target="_blank">Navigate to start ↗</a>
      <button class="ext-btn gpx-btn" id="gpxBtn">Export GPX (Garmin) ⤓</button>
    </div>`;
  drawProfile(rt, c);
  document.getElementById("gpxBtn").addEventListener("click", ()=>exportGPX(rt, i));
}

// ---- GPX export for Garmin (Connect / Fenix) ----
function buildGPX(rt, idx){
  const name = `w2run ${rt.category} ${(rt.metrics.distance_m/1000).toFixed(1)}km`;
  const coords = rt.coords;
  const eles = rt.elevations || [];
  const esc = s => String(s).replace(/[<>&'"]/g, c=>(
    {"<":"&lt;",">":"&gt;","&":"&amp;","'":"&apos;",'"':"&quot;"}[c]));
  const now = new Date().toISOString();
  let pts = "";
  for(let k=0;k<coords.length;k++){
    const lat = coords[k][0], lon = coords[k][1];
    const ele = eles[k]!=null ? eles[k] : "";
    pts += `      <trkpt lat="${lat}" lon="${lon}">`+
           (ele!==""?`<ele>${ele}</ele>`:"")+`</trkpt>\n`;
  }
  // explicit Start and End/Turnaround waypoints so they show on the watch.
  const startPt = coords[0], endPt = coords[coords.length-1];
  // farthest point = turnaround (out-and-back) or halfway (loop)
  let farIdx = 0, farD = 0;
  for(let k=0;k<coords.length;k++){
    const d=(coords[k][0]-startPt[0])**2+(coords[k][1]-startPt[1])**2;
    if(d>farD){farD=d;farIdx=k;}
  }
  const farPt = coords[farIdx];
  const isLoop = rt.shape!=="out-and-back";
  const wpt=(pt,name,sym)=>`  <wpt lat="${pt[0]}" lon="${pt[1]}">`+
    (pt===startPt&&eles[0]!=null?`<ele>${eles[0]}</ele>`:"")+
    `<name>${esc(name)}</name><sym>${sym}</sym></wpt>\n`;
  let wpts = wpt(startPt, "Start", "Flag, Green");
  wpts += wpt(farPt, isLoop?"Halfway":"Turnaround", "Flag, Blue");
  if(!isLoop) wpts += wpt(endPt, "Finish", "Flag, Red");
  return `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="w2run" xmlns="http://www.topografix.com/GPX/1/1" `+
`xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" `+
`xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd">
  <metadata><name>${esc(name)}</name><time>${now}</time></metadata>
${wpts}  <trk>
    <name>${esc(name)}</name>
    <type>running</type>
    <trkseg>
${pts}    </trkseg>
  </trk>
</gpx>`;
}

function exportGPX(rt, idx){
  const gpx = buildGPX(rt, idx);
  const blob = new Blob([gpx], {type:"application/gpx+xml"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const fname = `w2run-${rt.category.toLowerCase().replace(/[^a-z0-9]+/g,"-")}-`+
                `${(rt.metrics.distance_m/1000).toFixed(1)}km.gpx`;
  a.href = url; a.download = fname;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a);
  setTimeout(()=>URL.revokeObjectURL(url), 1000);
}

function drawProfile(rt, color){
  const ctx = document.getElementById("profileChart");
  const prof = rt.elevation_profile;
  const labels = prof.map(p=>(p.d/1000).toFixed(1));
  const data = prof.map(p=>p.e);
  if(profileChart) profileChart.destroy();

  // vertical crosshair line at the hovered point (Komoot-style)
  const crosshair = {
    id: "crosshair",
    afterDraw(chart){
      const act = chart.tooltip?._active;
      if(!act || !act.length) return;
      const x = act[0].element.x;
      const {top, bottom} = chart.chartArea;
      const cx = chart.ctx;
      cx.save();
      cx.beginPath();
      cx.moveTo(x, top); cx.lineTo(x, bottom);
      cx.lineWidth = 1.5; cx.strokeStyle = color;
      cx.setLineDash([4,3]);
      cx.stroke();
      cx.restore();
    }
  };

  profileChart = new Chart(ctx,{
    type:"line",
    data:{labels,datasets:[{data,borderColor:color,backgroundColor:color+"33",
      fill:true,tension:.3,pointRadius:0,pointHoverRadius:5,
      pointHoverBackgroundColor:color,pointHoverBorderColor:"#fff",
      pointHoverBorderWidth:2,borderWidth:2}]},
    options:{responsive:true,maintainAspectRatio:false,
      interaction:{mode:"index",intersect:false},
      onHover:(e,els)=>{
        if(els && els.length){ moveHoverMarker(prof[els[0].index], color); }
      },
      plugins:{legend:{display:false},tooltip:{
        mode:"index",intersect:false,
        callbacks:{
          title:i=>"km "+i[0].label,
          label:c=>Math.round(c.raw)+" m"}}},
      scales:{
        x:{ticks:{color:"#8ba597",maxTicksLimit:6},grid:{color:"#26402f"},title:{display:true,text:"km",color:"#8ba597"}},
        y:{ticks:{color:"#8ba597"},grid:{color:"#26402f"},title:{display:true,text:"elevation (m)",color:"#8ba597"}}
      }},
    plugins:[crosshair]
  });

  // clear the map marker when the pointer leaves the chart
  ctx.onmouseleave = ()=>{ if(hoverMarker){ map.removeLayer(hoverMarker); hoverMarker=null; } };
}

// place/move a marker on the map corresponding to the hovered chart point
function moveHoverMarker(pt, color){
  if(!pt || pt.lat==null) return;
  const html = `<div class="hover-dot" style="border-color:${color}"></div>
    <div class="hover-label">${Math.round(pt.e)} m · km ${(pt.d/1000).toFixed(1)}</div>`;
  const icon = L.divIcon({className:"hover-icon", html, iconSize:[0,0], iconAnchor:[0,0]});
  if(!hoverMarker){
    hoverMarker = L.marker([pt.lat, pt.lon], {icon, interactive:false, zIndexOffset:1000}).addTo(map);
  } else {
    hoverMarker.setLatLng([pt.lat, pt.lon]);
    hoverMarker.setIcon(icon);
  }
}

initMap();

// ---------------- Mobile bottom-sheet ----------------
const sheet = document.getElementById("sidebar");
const sheetHandle = document.getElementById("sheetHandle");
const isMobile = () => window.matchMedia("(max-width:760px)").matches;

function setSheet(state){ // 'peek' | 'half' | 'open'
  sheet.classList.remove("sheet-half","sheet-open");
  if(state==="half") sheet.classList.add("sheet-half");
  else if(state==="open") sheet.classList.add("sheet-open");
  // let Leaflet recompute size after the sheet animates
  setTimeout(()=>map && map.invalidateSize(), 320);
}

(function initSheetDrag(){
  if(!sheetHandle) return;
  let startY=0, startTransform=0, dragging=false;
  const vh = () => window.innerHeight;

  const currentTranslate = () => {
    const m = new DOMMatrixReadOnly(getComputedStyle(sheet).transform);
    return m.m42; // translateY in px
  };
  const onDown = e=>{
    if(!isMobile()) return;
    dragging=true; sheet.classList.add("dragging");
    startY = (e.touches?e.touches[0].clientY:e.clientY);
    startTransform = currentTranslate();
    e.preventDefault();
  };
  const onMove = e=>{
    if(!dragging) return;
    const y = (e.touches?e.touches[0].clientY:e.clientY);
    let ty = Math.max(0, startTransform + (y-startY));
    sheet.style.transform = `translateY(${ty}px)`;
  };
  const onUp = ()=>{
    if(!dragging) return;
    dragging=false; sheet.classList.remove("dragging");
    const ty = currentTranslate();
    sheet.style.transform = ""; // hand back to CSS classes
    const h = vh();
    // snap to nearest of open(0) / half(~45%) / peek(bottom)
    if(ty < h*0.25) setSheet("open");
    else if(ty < h*0.6) setSheet("half");
    else setSheet("peek");
  };
  sheetHandle.addEventListener("touchstart", onDown, {passive:false});
  window.addEventListener("touchmove", onMove, {passive:false});
  window.addEventListener("touchend", onUp);
  sheetHandle.addEventListener("mousedown", onDown);
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
  // tap the handle to toggle open/peek
  sheetHandle.addEventListener("click", ()=>{
    if(!isMobile()) return;
    if(sheet.classList.contains("sheet-open")) setSheet("peek");
    else setSheet("open");
  });
})();

// On open: if we can't use geolocation (insecure context), tell the user up
// front. Otherwise auto-start on desktop; on mobile wait for the button tap so
// iOS Safari shows the permission prompt (it needs a user gesture).
(function bootstrap(){
  if(window.isSecureContext === false){
    setStatus("Location needs a secure connection. Open via <b>https://</b> "+
      "(or <b>localhost</b> on your computer). Then tap “Find my routes”.", true);
    return;
  }
  if(isMobile()){
    setStatus("Tap “Find my routes” to share your location.");
    return;
  }
  requestLocation(pos=>{
    document.getElementById("findBtn").disabled = true;
    fetchRoutes(pos.lat, pos.lon);
  });
})();

// keep the map sized correctly through orientation / viewport changes
window.addEventListener("resize", ()=>{ if(map) map.invalidateSize(); });
window.addEventListener("orientationchange", ()=>{
  setTimeout(()=>map && map.invalidateSize(), 300);
});
