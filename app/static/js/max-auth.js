// Restore a verified MAX identity on every fresh launch. Never store credentials
// in localStorage or infer an identity from an unsigned user ID.
(function () {
  let running = false;
  let attempted = false;
  let askedAllow = false;
  function launchData() {
    const bridge = window.WebApp;
    if (typeof bridge?.initData === "string" && bridge.initData) return bridge.initData;
    const params = new URLSearchParams(window.location.hash.slice(1));
    const values = params.getAll("WebAppData");
    return values.length === 1 ? values[0] : "";
  }
  function hasMaxBridge() {
    try {
      const bridge = window.WebApp;
      if (!bridge) return false;
      if (bridge.platform) return true;
      if (typeof bridge.ready === "function") return true;
      if (typeof bridge.initData === "string") return true;
      if (bridge.initDataUnsafe) return true;
    } catch {}
    return false;
  }
  function inMaxWebApp() {
    if (hasMaxBridge() && launchData()) return true;
    try {
      if (typeof window.WebApp?.initData === "string" && window.WebApp.initData) return true;
      if (window.WebApp?.initDataUnsafe?.user) return true;
    } catch {}
    const hash = window.location.hash || "";
    if (hash.includes("WebAppData")) return true;
    const query = new URLSearchParams(window.location.search || "");
    return Boolean(query.get("WebAppStartParam") || query.get("startapp") || query.get("start"));
  }
  function onLauncher() {
    const path = window.location.pathname || "";
    if (path === "/app" || path === "/app/") return true;
    if ((path === "/" || path === "") && inMaxWebApp()) return true;
    return false;
  }
  function startParam() {
    try {
      const fromBridge = window.WebApp?.initDataUnsafe?.start_param;
      if (fromBridge) return String(fromBridge);
    } catch {}
    const raw = launchData();
    if (raw) {
      const fromLaunch = new URLSearchParams(raw).get("start_param");
      if (fromLaunch) return fromLaunch;
    }
    const query = new URLSearchParams(window.location.search || "");
    return query.get("start") || query.get("startapp") || query.get("WebAppStartParam") || "";
  }
  function liveBiz() {
    const ds = document.body.dataset || {};
    return ds.canEarn === "1" || ds.role === "business";
  }
  function nextPath(param, live) {
    const start = String(param || "").trim();
    if (start.startsWith("s_")) return "/biz/scan";
    if (start.startsWith("p_")) return "/me/qr";
    const key = start.toLowerCase();
    const canScan = (document.body.dataset || {}).canScan === "1";
    if (key === "admin" || key === "review") {
      return (document.body.dataset || {}).isAdmin === "1" ? "/admin" : "/me";
    }
    if (key === "qr" || key === "showqr" || key.endsWith("qr")) {
      return canScan ? "/biz/scan" : "/me/qr";
    }
    if (key === "scan" || key.endsWith("scan")) return live ? "/biz/scan" : "/me/scan";
    if (key === "cabinet" || key === "home" || key === "openapp" || key === "lk" || key === "me" || !key) {
      return live ? "/biz" : "/me";
    }
    return live ? "/biz" : "/me";
  }
  function showError(message) {
    const status = document.querySelector("#cabinet .muted");
    if (status) status.textContent = message;
    const box = document.getElementById("toast");
    if (box) { box.textContent = message; box.hidden = false; }
  }
  function wakeBridge() {
    try { window.WebApp?.ready?.(); } catch {}
    try { window.WebApp?.expand?.(); } catch {}
  }
  function openInBrowser() {
    const token = document.getElementById("max-wait")?.dataset?.token;
    const url = token
      ? window.location.origin + "/login?ticket=" + encodeURIComponent(token)
      : window.location.origin + (window.location.pathname === "/app" ? "/login" : window.location.pathname + window.location.search);
    try {
      if (typeof window.WebApp?.openLink === "function") {
        window.WebApp.openLink(url);
        return;
      }
    } catch {}
    window.location.assign(url);
  }
  function showNeedAllow() {
    askedAllow = true;
    document.documentElement.classList.add("miniapp");
    showError("Нажми «Разрешить» во всплывающем окне MAX. Без этого вход не работает. Кнопка «Старт» не нужна — она в MAX часто не нажимается.");
    const login = document.getElementById("max-login-link");
    if (login) login.hidden = true;
    const browser = document.getElementById("max-browser");
    if (browser) browser.hidden = false;
    const allow = document.getElementById("max-allow");
    if (allow) allow.hidden = false;
  }
  async function boot() {
    if (running || attempted) return;
    wakeBridge();
    const path = window.location.pathname || "";
    const atRoot = path === "/" || path === "";
    if (document.body.dataset.role && !onLauncher()) {
      if (!atRoot) attempted = true;
      return;
    }
    if (document.body.dataset.role && onLauncher()) {
      attempted = true;
      window.location.replace(nextPath(startParam(), liveBiz()));
      return;
    }
    const raw = launchData();
    if (!raw) {
      if (hasMaxBridge()) showNeedAllow();
      return;
    }
    running = true;
    document.documentElement.classList.add("miniapp");
    try { sessionStorage.setItem("cup-miniapp", "1"); } catch {}
    try { window.WebApp?.ready?.(); } catch {}
    try {
      const response = await fetch("/app/auth", {
        method: "POST", credentials: "include",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({init_data: raw}),
      });
      if (!response.ok) {
        attempted = true;
        showNeedAllow();
        showError("MAX не подтвердил вход. Нажми «Разрешить» и открой кабинет кнопкой в боте, не «Старт».");
        return;
      }
      const data = await response.json();
      const session = await fetch("/app/session", {credentials: "include", cache: "no-store"});
      attempted = true;
      if (!session.ok) {
        showError("Это окно блокирует сохранение входа. Нажми «Открыть в браузере».");
        const browser = document.getElementById("max-browser");
        if (browser) browser.hidden = false;
        return;
      }
      const live = data.role === "business" || document.body.dataset.canEarn === "1";
      window.location.replace(data.next || nextPath(data.start_param || startParam(), live));
    } catch {
      showError("Не удалось восстановить вход. Проверяем соединение…");
    } finally { running = false; }
  }
  document.addEventListener("click", (event) => {
    const allow = event.target.closest?.("#max-allow");
    if (allow) {
      event.preventDefault();
      attempted = false;
      askedAllow = false;
      wakeBridge();
      boot();
      return;
    }
    const browser = event.target.closest?.("#max-browser");
    if (browser) {
      event.preventDefault();
      openInBrowser();
    }
  });
  let tries = 0;
  boot();
  const timer = setInterval(() => {
    tries += 1;
    if (attempted || tries >= 120) { clearInterval(timer); return; }
    boot();
  }, 250);
  window.addEventListener("pageshow", () => {
    if (!document.body.dataset.role || onLauncher()) { attempted = false; boot(); }
  });
})();
