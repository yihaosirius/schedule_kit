/* ScheduleKit 前端公共工具。
 *
 * 无构建步骤：直接原生 ESM 风格挂在 window.sk 上，供各页面脚本使用。
 * 所有写操作都会自动带上 X-CSRF-Token（从 Cookie 读取），
 * 服务端对 Cookie 认证的写请求强制校验它。
 */
(function () {
  "use strict";

  function readCookie(name) {
    const prefix = name + "=";
    for (const part of document.cookie.split(";")) {
      const item = part.trim();
      if (item.startsWith(prefix)) return decodeURIComponent(item.slice(prefix.length));
    }
    return "";
  }

  async function api(method, path, body) {
    const headers = { Accept: "application/json" };
    const options = { method, headers, credentials: "same-origin" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    if (method !== "GET" && method !== "HEAD") {
      headers["X-CSRF-Token"] = readCookie("sk_csrf");
    }
    const response = await fetch(path, options);
    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch (_) { payload = { detail: text }; }
    }
    if (!response.ok) {
      const detail = (payload && payload.detail) || response.statusText;
      const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      error.status = response.status;
      error.traceId = response.headers.get("X-Trace-Id") || "";
      throw error;
    }
    return payload;
  }

  function toast(message, kind) {
    let host = document.getElementById("sk-toast");
    if (!host) {
      host = document.createElement("div");
      host.id = "sk-toast";
      host.style.cssText =
        "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:99;" +
        "padding:9px 16px;border-radius:8px;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.15);" +
        "background:#1c1e21;color:#fff;opacity:0;transition:opacity .15s";
      document.body.appendChild(host);
    }
    host.textContent = message;
    host.style.background = kind === "error" ? "#dc2626" : "#1c1e21";
    host.style.opacity = "1";
    clearTimeout(host._timer);
    host._timer = setTimeout(() => { host.style.opacity = "0"; }, 2600);
  }

  function reportError(error) {
    const suffix = error && error.traceId ? `（追踪号 ${error.traceId}）` : "";
    toast(((error && error.message) || "操作失败") + suffix, "error");
  }

  window.sk = { api, readCookie, toast, reportError };

  /* 注册 Service Worker（仅缓存静态外壳，接口一律直连网络）。
     http 页面里 navigator.serviceWorker 不可用，所以必须先判协议——
     本地开发用 http://127.0.0.1 时不会注册，也不会报错。 */
  if ("serviceWorker" in navigator && window.location.protocol === "https:") {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        /* 注册失败不影响使用，静默忽略 */
      });
    });
  }
})();
