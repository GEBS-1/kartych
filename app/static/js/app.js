const webApp = () => window.WebApp || null;
const toast = (message) => { const el = document.getElementById("toast"); if (el) {el.textContent = message; el.hidden = false;} };
function scanStatus(message) { const el = document.getElementById("scan-status"); if (el) el.textContent = message; }
const scanner = {generation: 0, stream: null, busy: false, torch: false};
function stopCamera() {
  scanner.generation += 1;
  if (scanner.stream) scanner.stream.getTracks().forEach(track => track.stop());
  scanner.stream = null;
  const video = document.getElementById("scan-video");
  if (video) video.srcObject = null;
}
function closeOverlay() {
  stopCamera();
  const box = document.getElementById("scan-overlay");
  if (box?.open) box.close();
}
async function submitCode(code) {
  if (!code || scanner.busy) return;
  scanner.busy = true;
  stopCamera();
  scanStatus("Проверяем QR…");
  try {
    const response = await fetch("/app/scan", {method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({code})});
    const data = await response.json();
    if (!data.ok) { scanStatus(data.message || "Не удалось прочитать код."); return; }
    closeOverlay();
    sessionStorage.setItem("cup-flash", data.message || "Готово!");
    window.location.assign(data.next || (document.body.dataset.cabinet === "biz" ? "/biz/scan" : "/me"));
  } catch { scanStatus("Нет связи с сервером. Проверь интернет и попробуй ещё раз."); }
  finally { scanner.busy = false; }
}
async function scanQr() {
  const box = document.getElementById("scan-overlay");
  if (!box || box.open) return;
  box.showModal();
  scanStatus("Открываю камеру…");
  const generation = ++scanner.generation;
  try {
    if (!navigator.mediaDevices?.getUserMedia) {
      scanStatus("Камера недоступна. Открой сайт по HTTPS или вставь код ниже.");
      return;
    }
    const stream = await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:"environment"}},audio:false});
    if (generation !== scanner.generation) {stream.getTracks().forEach(t=>t.stop()); return;}
    scanner.stream = stream;
    const video = document.getElementById("scan-video");
    video.srcObject = stream;
    await video.play();
    let detector = null;
    if (window.BarcodeDetector) {
      try { detector = new BarcodeDetector({formats:["qr_code"]}); } catch {}
    }
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", {willReadFrequently:true});
    scanStatus("Держи QR внутри рамки");
    while (generation === scanner.generation && box.open) {
      let code = "";
      if (video.readyState >= 2) {
        if (detector) {
          try {code = (await detector.detect(video))[0]?.rawValue || "";} catch {detector = null;}
        }
        if (!detector && window.jsQR) {
          canvas.width = video.videoWidth; canvas.height = video.videoHeight;
          context.drawImage(video,0,0);
          const pixels = context.getImageData(0,0,canvas.width,canvas.height);
          code = window.jsQR(pixels.data,canvas.width,canvas.height,{inversionAttempts:"attemptBoth"})?.data || "";
        }
        if (!detector && !window.jsQR) scanStatus("Автораспознавание недоступно. Вставь текст QR ниже.");
      }
      if (code) { await submitCode(code); break; }
      await new Promise(resolve => setTimeout(resolve,180));
    }
  } catch {
    stopCamera();
    const app = webApp();
    if (app && typeof app.openCodeReader === "function") {
      scanStatus("Пробую сканер MAX…");
      try {
        const result = await app.openCodeReader(false);
        if (generation !== scanner.generation) return;
        const code = typeof result === "string" ? result : result?.data || result?.code;
        if (code) await submitCode(code);
        else scanStatus("Сканирование отменено. Можно вставить код ниже.");
        return;
      } catch {}
    }
    scanStatus("Нет доступа к камере. Разреши его в настройках браузера или вставь код ниже.");
  }
}
async function waitMaxLogin() {
  const token = document.getElementById("max-wait")?.dataset.token;
  if (!token) return;
  for(let i=0;i<80;i++) {
    try {
      const response = await fetch("/login/status/"+encodeURIComponent(token));
      const data = await response.json();
      if(data.status==="ok") {window.location.replace("/login/complete/"+encodeURIComponent(token));return;}
      if(["expired","missing","used"].includes(data.status)) {window.location.replace("/login?err=expired");return;}
    } catch {}
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
  toast("Подтверждение не получено. Вернись на экран входа и попробуй ещё раз.");
}
let placeFilter = "all";
function filterRows() {
  const search = document.querySelector("[data-search]");
  if (!search) return;
  const query = search.value.toLocaleLowerCase("ru");
  let visible = 0;
  document.querySelectorAll(search.dataset.search).forEach(row => {
    const matches = (row.dataset.searchText || row.textContent).toLocaleLowerCase("ru").includes(query);
    const group = placeFilter === "all" || (placeFilter === "mine" ? row.dataset.linked === "true" : row.dataset.linked !== "true");
    row.hidden = !(matches && group);
    if (!row.hidden) visible++;
  });
  const empty = document.querySelector("[data-search-empty]");
  if (empty) empty.hidden = visible > 0;
  if (window.cupMapMarkers) {
    const shown = [];
    window.cupMapMarkers.forEach(({marker,id})=>{
      const row = Array.from(document.querySelectorAll(".place-row")).find(r=>r.dataset.id===id);
      if (row && !row.hidden) { marker.addTo(window.cupMap); shown.push(marker); }
      else marker.remove();
    });
    if (window.cupMap && shown.length === 1) {
      const latlng = shown[0].getLatLng();
      window.cupMap.setView(latlng, 15);
    } else if (window.cupMap && shown.length > 1) {
      window.cupMap.fitBounds(shown.map(m=>m.getLatLng()), {padding:[35,35], maxZoom:15});
    }
  }
}
function addFreeTiles(map) {
  if (window.L?.Icon?.Default) L.Icon.Default.imagePath = "/static/vendor/leaflet/images/";
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const opts = {maxZoom:19, className: dark ? "map-tiles-dark" : ""};
  const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    ...opts,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  });
  let switched = false;
  osm.on("tileerror", () => {
    if (switched) return;
    switched = true;
    map.removeLayer(osm);
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {
      ...opts,
      attribution: "Tiles &copy; Esri"
    }).addTo(map);
  });
  osm.addTo(map);
}
function setupMap() {
  const el = document.getElementById("places-map"), data = document.getElementById("map-data");
  if (!el || !window.L) return;
  const places = data ? JSON.parse(data.textContent || "[]") : [];
  el.replaceChildren();
  const start = places[0] ? [places[0].lat, places[0].lng] : [55.7558, 37.6173];
  const map = L.map(el, {scrollWheelZoom:true}).setView(start, places.length ? 13 : 10);
  addFreeTiles(map);
  window.cupMap = map;
  window.cupMapMarkers = places.map(place=>{
    const marker = L.marker([place.lat,place.lng],{icon:L.divIcon({className:"map-pin",iconSize:[24,24],iconAnchor:[12,24]})}).addTo(map);
    const box = document.createElement("div");
    const link = document.createElement("a"); link.href="/shops/"+encodeURIComponent(place.id); link.textContent=place.name;
    const meta = document.createElement("small"); meta.textContent=[place.city, place.address].filter(Boolean).join(" · ");
    box.append(link, meta);
    marker.bindPopup(box);
    return {marker,id:place.id};
  });
  if(places.length>1) map.fitBounds(places.map(p=>[p.lat,p.lng]),{padding:[35,35],maxZoom:14});
  setTimeout(()=>map.invalidateSize(), 200);
}
document.addEventListener("click",async event=>{
  if(event.target.closest("#scan-btn, [data-open-scanner]")) {event.preventDefault();scanQr();}
  if(event.target.closest("#scan-cancel")) closeOverlay();
  const filter=event.target.closest("[data-place-filter]");
  if(filter) {
    placeFilter=filter.dataset.placeFilter;
    document.querySelectorAll("[data-place-filter]").forEach(b=>b.classList.toggle("active",b===filter));
    filterRows();
  }
  if(event.target.closest("#locate-me")) {
    if (!navigator.geolocation) {toast("Геолокация недоступна. Выбери место из списка.");return;}
    navigator.geolocation.getCurrentPosition(position=>{
      if(!window.cupMap){toast("Точки ещё не добавили координаты. Адреса есть в списке.");return;}
      const point=[position.coords.latitude,position.coords.longitude];
      window.cupMap.setView(point,14);
      L.circleMarker(point,{radius:7,color:"#fff",fillColor:"#3b82f6",fillOpacity:1,weight:3}).addTo(window.cupMap);
    },()=>toast("Геолокация недоступна. Можно найти место по названию или адресу."));
  }
  if(event.target.closest("#scan-torch")) {
    const track=scanner.stream?.getVideoTracks()[0];
    try {
      if (!track?.getCapabilities?.().torch) {scanStatus("Фонарик недоступен на этом устройстве.");return;}
      scanner.torch=!scanner.torch; await track.applyConstraints({advanced:[{torch:scanner.torch}]});
    } catch {scanStatus("Не удалось включить фонарик.");}
  }
});
document.getElementById("manual-scan")?.addEventListener("submit",event=>{event.preventDefault();submitCode(document.getElementById("manual-code").value.trim());});
document.getElementById("scan-overlay")?.addEventListener("close",stopCamera);
document.querySelectorAll("[data-search]").forEach(input=>input.addEventListener("input",filterRows));
document.querySelectorAll("[data-auto-submit]").forEach(input=>input.addEventListener("change",()=>input.form.requestSubmit()));
window.addEventListener("pagehide",stopCamera);
document.addEventListener("visibilitychange",()=>{if(document.hidden) closeOverlay();});
try {const flash=sessionStorage.getItem("cup-flash");if(flash){toast(flash);sessionStorage.removeItem("cup-flash");setTimeout(()=>{document.getElementById("toast").hidden=true;},7000);}}catch {}
function setupTheme() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("kartych-theme", next); } catch {}
    const color = next === "dark" ? "#12151c" : "#f3f5fa";
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", color);
  });
}
function setupApplyMap() {
  const el = document.getElementById("apply-map");
  const lat = document.getElementById("apply-lat");
  const lng = document.getElementById("apply-lng");
  if (!el || !window.L) return;
  const startLat = Number(lat?.value) || 55.7558;
  const startLng = Number(lng?.value) || 37.6173;
  const map = L.map(el, {scrollWheelZoom:true}).setView([startLat, startLng], lat?.value ? 15 : 10);
  addFreeTiles(map);
  setTimeout(()=>map.invalidateSize(), 200);
  let marker = lat?.value && lng?.value ? L.marker([startLat, startLng]).addTo(map) : null;
  const put = (point) => {
    if (!lat || !lng) return;
    lat.value = point.lat.toFixed(6);
    lng.value = point.lng.toFixed(6);
    if (marker) marker.setLatLng(point);
    else marker = L.marker(point).addTo(map);
    map.setView(point, 16);
  };
  map.on("click", event => put(event.latlng));
  document.getElementById("geocode-btn")?.addEventListener("click", async () => {
    const form = document.getElementById("apply-form");
    const q = ["city","address","name"].map(name => form?.elements[name]?.value || "").join(" ").trim();
    if (q.length < 3) { toast("Укажи город и адрес"); return; }
    try {
      const response = await fetch("/biz/geocode", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({q})});
      const data = await response.json();
      if (!data.ok) { toast(data.message || "Адрес не найден"); return; }
      put({lat: data.lat, lng: data.lng});
    } catch { toast("Не удалось определить адрес"); }
  });
}
setupMap(); setupApplyMap(); setupTheme(); waitMaxLogin();
if (document.body.dataset.page === "scan" || /\/scan\/?$/.test(location.pathname)) scanQr();

