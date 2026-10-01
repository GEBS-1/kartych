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
  const box = document.getElementById("max-wait");
  const token = box?.dataset.token;
  if (!token) return;
  const copy = box.querySelector("[data-copy-code]");
  if (copy) {
    copy.addEventListener("click", async () => {
      const code = box.dataset.payload || copy.textContent || "";
      try {
        await navigator.clipboard.writeText(code.trim());
        toast("Код скопирован. Вставь его сообщением боту в приложении MAX.");
      } catch {
        toast("Скопируй код руками и отправь боту в MAX.");
      }
    });
  }
  for(let i=0;i<120;i++) {
    try {
      const response = await fetch("/login/status/"+encodeURIComponent(token));
      const data = await response.json();
      if(data.status==="ok" || data.status==="used") {window.location.replace("/login/complete/"+encodeURIComponent(token));return;}
      if(["expired","missing"].includes(data.status)) {window.location.replace("/login?err=expired");return;}
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
  if (window.L?.Control?.Attribution) L.Control.Attribution.mergeOptions({prefix:false});
  if (window.L?.Map) L.Map.mergeOptions({attributionControl:false});
  if (window.L?.Icon?.Default) L.Icon.Default.imagePath = "/static/vendor/leaflet/images/";
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    minZoom: 8,
    attribution: "",
    className: dark ? "map-tiles-dark" : ""
  }).addTo(map);
}
function kmBetween(a, b) {
  const toRad = value => value * Math.PI / 180;
  const dLat = toRad(b.lat - a.lat), dLng = toRad(b.lng - a.lng);
  const s = Math.sin(dLat/2)**2 + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLng/2)**2;
  return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(s)));
}
function yandexRoute(lat, lng) {
  return "https://yandex.ru/maps/?rtext=~" + lat + "," + lng;
}
function shopPopup(place) {
  const box = document.createElement("div");
  box.className = "map-popup";
  const title = document.createElement("a");
  title.href = "/shops/" + encodeURIComponent(place.id);
  title.textContent = place.name;
  const meta = document.createElement("small");
  meta.textContent = [place.city, place.address].filter(Boolean).join(" · ");
  const actions = document.createElement("div");
  actions.className = "map-popup-actions";
  const open = document.createElement("a");
  open.className = "btn small";
  open.href = "/shops/" + encodeURIComponent(place.id);
  open.textContent = "Зайти";
  const route = document.createElement("a");
  route.className = "text-link";
  route.target = "_blank";
  route.rel = "noopener";
  route.href = yandexRoute(place.lat, place.lng);
  route.textContent = "Маршрут";
  actions.append(open, route);
  box.append(title, meta, actions);
  return box;
}
function markOnMap(map, lat, lng, kind) {
  const point = [lat, lng];
  map.setView(point, 15);
  const key = kind === "me" ? "cupMeMarker" : "cupSearchMarker";
  const style = kind === "me"
    ? {radius:8,color:"#fff",fillColor:"#2d5aa0",fillOpacity:1,weight:3}
    : {radius:7,color:"#fff",fillColor:"#e8941a",fillOpacity:1,weight:3};
  if (window[key]) window[key].setLatLng(point);
  else window[key] = L.circleMarker(point, style).addTo(map);
  return point;
}
function sortRowsByDistance(origin) {
  const list = document.querySelector(".place-list");
  if (!list || !window.cupMapMarkers) return;
  const rows = Array.from(list.querySelectorAll(".place-row"));
  rows.sort((left, right) => {
    const a = window.cupMapMarkers.find(item => item.id === left.dataset.id);
    const b = window.cupMapMarkers.find(item => item.id === right.dataset.id);
    const da = a ? kmBetween(origin, a.marker.getLatLng()) : 9999;
    const db = b ? kmBetween(origin, b.marker.getLatLng()) : 9999;
    return da - db;
  });
  rows.forEach(row => {
    const marker = window.cupMapMarkers.find(item => item.id === row.dataset.id);
    const dist = row.querySelector(".place-dist");
    if (marker) {
      const km = kmBetween(origin, marker.marker.getLatLng());
      const label = km < 1 ? Math.round(km * 1000) + " м" : km.toFixed(1) + " км";
      if (dist) dist.textContent = label;
      else {
        const small = document.createElement("small");
        small.className = "place-dist";
        small.textContent = label;
        row.querySelector(".place-info small")?.after(small);
      }
    }
    list.append(row);
  });
}
async function readMyLocation() {
  const app = webApp();
  if (app && typeof app.requestLocation === "function") {
    try {
      const loc = await app.requestLocation();
      const lat = loc?.latitude ?? loc?.lat;
      const lng = loc?.longitude ?? loc?.lng ?? loc?.lon;
      if (Number.isFinite(lat) && Number.isFinite(lng)) return {lat, lng};
    } catch {}
  }
  if (!navigator.geolocation) throw new Error("no-geo");
  return await new Promise((resolve, reject) => {
    navigator.geolocation.getCurrentPosition(
      position => resolve({lat: position.coords.latitude, lng: position.coords.longitude}),
      reject,
      {enableHighAccuracy:true, timeout:12000, maximumAge:20000}
    );
  });
}
async function locateOnMap(map, {sort=false}={}) {
  if (!map) { toast("Карта ещё открывается."); return null; }
  try {
    const point = await readMyLocation();
    markOnMap(map, point.lat, point.lng, "me");
    if (sort) sortRowsByDistance(point);
    return point;
  } catch {
    toast("Не вижу геолокацию. Разреши её или найди адрес в поиске.");
    return null;
  }
}
function openShopMarker(id) {
  const found = window.cupMapMarkers?.find(item => item.id === id);
  if (!found || !window.cupMap) return;
  window.cupMap.setView(found.marker.getLatLng(), 16);
  found.marker.openPopup();
  document.querySelector(`.place-row[data-id="${id}"]`)?.scrollIntoView({block:"nearest", behavior:"smooth"});
}
function hideSuggest() {
  const box = document.getElementById("map-suggest");
  if (box) box.hidden = true;
}
function showSuggest(shops, places) {
  const box = document.getElementById("map-suggest");
  if (!box) return;
  box.replaceChildren();
  const add = (label, detail, onPick) => {
    const btn = document.createElement("button");
    btn.type = "button";
    const strong = document.createElement("strong");
    strong.textContent = label;
    const small = document.createElement("small");
    small.textContent = detail;
    btn.append(strong, small);
    btn.addEventListener("click", onPick);
    box.append(btn);
  };
  shops.forEach(shop => add(shop.name, [shop.city, shop.address].filter(Boolean).join(" · ") || "Точка Картыча", () => {
    document.getElementById("map-query").value = shop.name;
    filterRows();
    hideSuggest();
    if (shop.lat != null) openShopMarker(shop.id);
    else window.location.assign("/shops/" + encodeURIComponent(shop.id));
  }));
  places.forEach(place => add(place.label.split(",")[0], place.label, () => {
    hideSuggest();
    if (!window.cupMap) return;
    markOnMap(window.cupMap, place.lat, place.lng, "search");
    window.cupSearchMarker?.bindPopup(place.label).openPopup();
    sortRowsByDistance({lat: place.lat, lng: place.lng});
  }));
  box.hidden = box.childElementCount === 0;
}
let mapSearchTimer = 0;
async function searchMapQuery(query, {openFirst=false}={}) {
  const text = (query || "").trim();
  if (text.length < 2) { hideSuggest(); return; }
  try {
    const response = await fetch("/places/search", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({q:text})});
    const data = await response.json();
    if (!data.ok) return;
    showSuggest(data.shops || [], data.places || []);
    if (openFirst && data.shops?.[0]?.lat != null) openShopMarker(data.shops[0].id);
    else if (openFirst && data.places?.[0] && window.cupMap) {
      hideSuggest();
      markOnMap(window.cupMap, data.places[0].lat, data.places[0].lng, "search");
      window.cupSearchMarker?.bindPopup(data.places[0].label).openPopup();
      sortRowsByDistance({lat: data.places[0].lat, lng: data.places[0].lng});
    }
  } catch { if (openFirst) toast("Не удалось найти это место."); }
}
function setupMap() {
  const el = document.getElementById("places-map"), data = document.getElementById("map-data");
  if (!el || !window.L) return;
  const places = data ? JSON.parse(data.textContent || "[]") : [];
  el.replaceChildren();
  const start = places[0] ? [places[0].lat, places[0].lng] : [55.7558, 37.6173];
  const map = L.map(el, {scrollWheelZoom:true, attributionControl:false, minZoom:8, zoomControl:true}).setView(start, places.length ? 13 : 11);
  addFreeTiles(map);
  window.cupMap = map;
  window.cupMapMarkers = places.map(place=>{
    const marker = L.marker([place.lat,place.lng],{icon:L.divIcon({className:"map-pin",iconSize:[24,24],iconAnchor:[12,24]})}).addTo(map);
    marker.bindPopup(shopPopup(place));
    marker.on("click", () => document.querySelector(`.place-row[data-id="${place.id}"]`)?.scrollIntoView({block:"nearest"}));
    return {marker,id:place.id};
  });
  if(places.length>1) map.fitBounds(places.map(p=>[p.lat,p.lng]),{padding:[35,35],maxZoom:14});
  setTimeout(()=>map.invalidateSize(), 200);
  const query = document.getElementById("map-query");
  query?.addEventListener("input", () => {
    filterRows();
    clearTimeout(mapSearchTimer);
    mapSearchTimer = setTimeout(() => searchMapQuery(query.value), 350);
  });
  query?.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      searchMapQuery(query.value, {openFirst:true});
    }
    if (event.key === "Escape") hideSuggest();
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".map-search")) hideSuggest();
  });
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
    event.preventDefault();
    locateOnMap(window.cupMap, {sort:true});
  }
  if(event.target.closest("#apply-locate")) {
    event.preventDefault();
    const mapEl = document.getElementById("apply-map");
    if (!mapEl || !window.cupApplyMap) return;
    locateOnMap(window.cupApplyMap).then(point => {
      if (!point) return;
      const lat = document.getElementById("apply-lat");
      const lng = document.getElementById("apply-lng");
      if (lat && lng) { lat.value = point.lat.toFixed(6); lng.value = point.lng.toFixed(6); }
      window.cupApplyPut?.({lat: point.lat, lng: point.lng});
    });
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
  const map = L.map(el, {scrollWheelZoom:true, attributionControl:false, minZoom:8, zoomControl:true}).setView([startLat, startLng], lat?.value ? 16 : 11);
  addFreeTiles(map);
  window.cupApplyMap = map;
  setTimeout(()=>map.invalidateSize(), 200);
  const pin = () => L.divIcon({className:"map-pin",iconSize:[24,24],iconAnchor:[12,24]});
  let marker = lat?.value && lng?.value ? L.marker([startLat, startLng], {icon: pin()}).addTo(map) : null;
  const note = document.getElementById("apply-pin-label");
  const put = (point, label) => {
    if (!lat || !lng) return;
    lat.value = Number(point.lat).toFixed(6);
    lng.value = Number(point.lng).toFixed(6);
    if (marker) marker.setLatLng(point);
    else marker = L.marker(point, {icon: pin()}).addTo(map);
    map.setView(point, 17);
    if (note) note.textContent = label ? ("Метка: " + label) : "Метка стоит на карте. Можно подвинуть пальцем.";
  };
  window.cupApplyPut = put;
  map.on("click", event => put(event.latlng, "точка на карте"));
  const hideApplySuggest = () => {
    const box = document.getElementById("apply-suggest");
    if (box) box.hidden = true;
  };
  const showApplySuggest = (places) => {
    const box = document.getElementById("apply-suggest");
    if (!box) return;
    box.replaceChildren();
    places.forEach(place => {
      const btn = document.createElement("button");
      btn.type = "button";
      const strong = document.createElement("strong");
      strong.textContent = place.label.split(",")[0];
      const small = document.createElement("small");
      small.textContent = place.label;
      btn.append(strong, small);
      btn.addEventListener("click", () => {
        hideApplySuggest();
        put({lat: place.lat, lng: place.lng}, place.label);
      });
      box.append(btn);
    });
    box.hidden = box.childElementCount === 0;
  };
  const addressQuery = () => {
    const form = document.getElementById("apply-form") || lat?.form;
    const city = document.getElementById("apply-city")?.value || form?.elements?.city?.value || "";
    const address = document.getElementById("apply-address")?.value || form?.elements?.address?.value || "";
    const typed = document.getElementById("apply-search")?.value || "";
    return {q: (typed.trim() || address).trim(), city: city.trim()};
  };
  const findAddress = async ({openFirst=true}={}) => {
    const {q, city} = addressQuery();
    if ((q || city).replace(/\s+/g,"").length < 3) { toast("Напиши город, улицу и номер дома"); return; }
    try {
      const response = await fetch("/biz/geocode", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({q, city})});
      const data = await response.json();
      const places = data.places || [];
      if (!places.length) { toast(data.message || "Адрес не найден"); return; }
      showApplySuggest(places);
      if (openFirst) put({lat: places[0].lat, lng: places[0].lng}, places[0].label);
    } catch { toast("Не удалось найти адрес"); }
  };
  document.getElementById("geocode-btn")?.addEventListener("click", async () => {
    await findAddress({openFirst:true});
  });
  const applySearch = document.getElementById("apply-search");
  let applyTimer = 0;
  applySearch?.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); findAddress({openFirst:true}); }
    if (event.key === "Escape") hideApplySuggest();
  });
  applySearch?.addEventListener("input", () => {
    clearTimeout(applyTimer);
    applyTimer = setTimeout(() => findAddress({openFirst:false}), 450);
  });
  ["apply-city","apply-address"].forEach(id => {
    document.getElementById(id)?.addEventListener("change", () => findAddress({openFirst:true}));
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".map-search")) hideApplySuggest();
  });
}
setupMap(); setupApplyMap(); setupTheme(); waitMaxLogin();
if (document.body.dataset.page === "scan" || /\/scan\/?$/.test(location.pathname)) scanQr();

