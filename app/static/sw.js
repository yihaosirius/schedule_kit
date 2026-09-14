/* ScheduleKit Service Worker —— 只做"应用外壳"缓存。
 *
 * 刻意保守，三条规矩：
 *   1. **绝不缓存任何 /api/ 响应**。任务列表、草稿这类数据必须实时，
 *      缓存了会出现"勾选完刷新又变回来"这种极难排查的错觉。
 *   2. 页面导航走 network-first —— 服务端是渲染数据的，拿到旧 HTML
 *      就等于看到旧任务。断网时才退回缓存的外壳。
 *   3. 静态资源也走 network-first，断网才退回缓存。
 *
 * 规矩 3 原本是 cache-first，理由是"它们有版本号或很少变"。**这个前提不成立**：
 * 项目没有构建步骤，就没有文件名指纹；下面的 VERSION 是手写常量，没人会记得改。
 * 结果是任何 CSS / JS 改动都到不了已经装过 SW 的浏览器——实测踩到：新加的备注
 * 组件样式整整一轮都没生效，看到的始终是旧样式，还误以为是 CSS 写错了。
 *
 * 代价是每个静态资源多一次条件请求（有 ETag / 304 兜着，通常是空响应），
 * 换来的是"改了就能看到"。对一个个人应用来说这个交换明显划算。
 *
 * 只在 HTTPS 下注册：http 页面里 navigator.serviceWorker 是不可用的，
 * 所以 app.js 会先判断协议。
 */

// 换版本号会让 activate 清掉旧缓存。规矩 3 改成 network-first 之后，
// 它不再承担"让改动生效"的责任，只负责别让废弃缓存永远堆着。
const VERSION = 'schedulekit-v2';
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

/* network-first：先要网络，失败才用缓存。命中后顺手刷新缓存副本，
   这样离线时拿到的也是最后一次成功加载的版本。 */
async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const copy = response.clone();
      caches.open(VERSION).then((cache) => cache.put(request, copy));
    }
    return response;
  } catch (error) {
    const hit = await caches.match(request);
    if (hit) return hit;
    // 缓存里也没有：回一个明确的失败，而不是让 respondWith(undefined) 抛错
    return new Response('', { status: 504, statusText: 'Gateway Timeout' });
  }
}

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

  // 规矩 3：静态资源 network-first（原因见文件头）
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(networkFirst(request));
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
