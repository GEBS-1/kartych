// Restore a verified MAX identity on every fresh launch. Never store credentials
// in localStorage or infer an identity from an unsigned user ID.
(function () {
  let running = false;
  let attempted = false;
  function launchData() {
    const bridge = window.WebApp;
    if (typeof bridge?.initData === "string" && bridge.initData) return bridge.initData;
    const params = new URLSearchParams(window.location.hash.slice(1));
    const values = params.getAll("WebAppData");
    return values.length === 1 ? values[0] : "";
  }
  function showError(message) {
    const status = document.querySelector("#cabinet .muted");
    if (status) status.textContent = message;
    const box = document.getElementById("toast");
    if (box) { box.textContent = message; box.hidden = false; }
  }
  async function boot() {
    if (running || attempted) return;
    const raw = launchData();
    if (!raw) return;
    running = true;
    document.documentElement.classList.add("miniapp");
    try { sessionStorage.setItem("cup-miniapp", "1"); } catch {}
    try { window.WebApp?.ready?.(); } catch {}
    // A successful session is already rendered on cabinet pages. Reauthenticate
    // the entry screen when cookies were lost, using fresh signed launch data.
    if (document.body.dataset.role) { attempted = true; running = false; return; }
    try {
      const response = await fetch("/app/auth", {
        method: "POST", credentials: "include",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({init_data: raw}),
      });
      if (!response.ok) {
        attempted = true;
        showError("MAX не подтвердил вход. Закрой и заново открой мини-приложение в MAX.");
        return;
      }
      const data = await response.json();
      const session = await fetch("/app/session", {credentials: "include", cache: "no-store"});
      attempted = true;
      if (!session.ok) {
        showError("Это окно блокирует сохранение входа. Открой мини-приложение кнопкой в MAX или разреши cookie для сайта.");
        return;
      }
      const param = data.start_param || "";
      let next = "/me";
      if (param.startsWith("s_")) next = "/biz/scan";
      else if (param.startsWith("p_")) next = "/me/qr";
      else if (data.role === "business") next = "/biz";
      else if (param.endsWith("scan")) next = "/me/scan";
      else if (param.endsWith("qr")) next = "/me/qr";
      window.location.replace(next);
    } catch {
      showError("Не удалось восстановить вход. Проверяем соединение…");
    } finally { running = false; }
  }
  let tries = 0;
  boot();
  const timer = setInterval(() => {
    tries += 1;
    if (attempted || tries >= 40) { clearInterval(timer); return; }
    boot();
  }, 250);
  window.addEventListener("pageshow", () => {
    if (!document.body.dataset.role) { attempted = false; boot(); }
  });
})();
