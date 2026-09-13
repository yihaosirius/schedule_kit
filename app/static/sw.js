/* ScheduleKit Service Worker —— 只做"应用外壳"缓存。
 *
 * 刻意保守，三条规矩：
 *   1. **绝不缓存任何 /api/ 响应**。任务列表、草稿这类数据必须实时，
 *      缓存了会出现"勾选完刷新又变回来"这种极难排查的错觉。
 *   2. 页面导航走 network-first —— 服务端是渲染数据的，拿到旧 HTML
 *      就等于看到旧任务。断网时才退回缓存的外壳。
 *   3. 静态资源走 cache-first —— 它们有版本号或很少变，缓存能省流量。
 *
 * 只在 HTTPS 下注册：http 页面里 navigator.serviceWorker 是不可用的，
 * 所以 app.js 会先判断协议。
 */

const VERSION = 'schedulekit-v1';
const SHELL = [
  '/static/css/app.css',
  '/static/js/app.js',
  '/static/js/tasks.js',
  '/static/icons/icon-192.png',
  '/static/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      // 单个资源失败不该让整个安装失败（例如某个页面还没部署到）
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // 规矩 1：接口一律直连网络，不缓存、不兜底
  if (url.pathname.startsWith('/api/') || url.pathname === '/healthz') return;

  // 规矩 2：页面导航 network-first，断网退回外壳
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match(request).then((hit) => hit || offline()))
    );
    return;
  }

  // 规矩 3：静态资源 cache-first
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(request, copy));
        }
        return response;
      }))
    );
  }
});

function offline() {
  return new Response(
    '<!DOCTYPE html><meta charset="utf-8"><title>离线</title>' +
    '<body style="font:15px -apple-system,sans-serif;padding:40px;text-align:center;color:#6b7280">' +
    '<h1 style="font-size:17px;color:#1c1e21">目前离线</h1>' +
    '<p>任务数据需要联网获取。</p></body>',
    { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
  );
}
