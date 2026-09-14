/* ScheduleKit 小组件（Scriptable）
 * ============================================================================
 *
 * 把服务端的任务概览放到 iPhone 桌面与锁屏上。
 *
 * ## 用法
 *
 *   1. App Store 装 Scriptable（免费）
 *   2. 服务端 /settings 创建一个**只读** API Key
 *   3. 新建脚本 → 粘贴本文件 → 填下面的 CONFIG
 *   4. **先在 App 里跑一次**（不要直接加小组件）—— 会弹出自检报告
 *   5. 长按桌面 → 添加小组件 → Scriptable → 选本脚本 → 中号
 *      （锁屏：长按锁屏 → 自定 → 添加小组件 → Scriptable）
 *
 * ## 只读，这是平台限制不是没做
 *
 * iOS 的交互式小组件需要 App Intents，Scriptable 不提供；它的渲染 API 里
 * 根本没有按钮类型（官方文档的 ListWidget 一节可以核对）。所以**不能**在
 * 小组件里勾选完成。点击只能跳转 —— 这里跳到 WebUI，进去勾。
 *
 * ## 倒计时反而是实时的
 *
 * 任务列表本身要靠 iOS 决定何时刷新（实测 15–60 分钟一次，平台限制），
 * 但 `WidgetDate` 是 iOS 自己维护的，`applyTimerStyle()` /
 * `applyRelativeStyle()` 渲染出来的时间**会自己走**，不需要重新拉数据。
 * 所以「还剩多久」永远是准的，只有列表内容可能滞后。
 *
 * ## 只用 ES6 语法
 *
 * Scriptable 文档写的是 ECMAScript 6。`?.` / `??` / `Promise.allSettled` /
 * `Object.fromEntries` 都是更晚的语法，这里一律不用 —— 在小组件里语法错误
 * 的代价是整个方块渲染不出来，而你在手机上很难看出原因。
 */

// ============================================================================
// 配置：只改这一段
// ============================================================================
const CONFIG = {
  // 服务端地址。末尾斜杠可带可不带。
  BASE_URL: "https://canisa1ph.duckdns.org:8443",

  // 只读 API Key（服务端 /settings → 创建 → 勾「只读」）。
  // 明文放在这里是有意的：脚本文件本身就在 Scriptable 的沙盒里，
  // 与放进 Keychain 是同一层保护，但省掉了「小组件进程里能不能读到
  // Keychain」这个不确定性。细节见 clients/ios/README.md。
  API_KEY: "sk_在这里粘贴只读密钥",

  // 与服务端 [server].timezone 保持一致。deadline 存的是 UTC，
  // 展示时统一按这个时区换算 —— 这样即使你人在国外，
  // 「6/1 10:00 交」显示的仍是北京时间那个真实含义。
  TIMEZONE: "Asia/Shanghai",

  // 各表取几条。中号放得下这么多；大号会各多取几条。
  ORDERED_LIMIT: 2,
  UNORDERED_LIMIT: 2,

  // 单次请求超时。**不要用默认的 60 秒。**
  // iOS 给小组件构建时间线的预算很紧（原生 WidgetKit 约 5 秒），
  // 卡满 60 秒的话小组件会被系统掐死，连缓存的兜底都来不及渲染。
  // 实测 DuckDNS 未缓存解析 200–780ms + TLS + 请求通常 1–2 秒，
  // 所以 4 秒对正常路径很宽裕，对失败路径又有界。
  TIMEOUT_SECONDS: 4,

  // 请求 iOS 在 N 分钟后允许刷新。这只是**下限提示**，不是保证。
  REFRESH_MINUTES: 15,
};

// ============================================================================
// 取数
// ============================================================================
function baseUrl() {
  var url = CONFIG.BASE_URL || "";
  while (url.length > 1 && url.charAt(url.length - 1) === "/") {
    url = url.substring(0, url.length - 1);
  }
  return url;
}

function ApiError(kind, message, status) {
  this.kind = kind;
  this.message = message;
  this.status = status || 0;
}
ApiError.prototype = Object.create(Error.prototype);

/* 把 HTTP 状态码翻译成"我该去干什么"，而不是丢一个数字给用户。 */
function describeHttp(status, body) {
  if (status === 401) {
    return "密钥无效或已吊销 —— 去服务端 /settings 重新创建一个只读密钥";
  }
  if (status === 403) {
    return "密钥权限不足（小组件只需只读密钥，不该出现 403）";
  }
  if (status === 503) {
    return "服务端尚未就绪（LLM 未配置不影响本小组件，请查看服务端日志）";
  }
  var detail = body && typeof body.detail === "string" ? body.detail : "";
  return "服务器返回 HTTP " + status + (detail ? "：" + detail : "");
}

function api(path) {
  var req = new Request(baseUrl() + path);
  req.headers = { Authorization: "Bearer " + CONFIG.API_KEY };
  req.timeoutInterval = CONFIG.TIMEOUT_SECONDS;

  return req.loadJSON().then(
    function (body) {
      var status = statusOf(req);
      if (status >= 400) {
        throw new ApiError("http", describeHttp(status, body), status);
      }
      return body;
    },
    function (err) {
      var status = statusOf(req);
      if (status >= 400) {
        // 4xx/5xx 且响应体不是 JSON（例如网关的 HTML 错误页）
        throw new ApiError("http", describeHttp(status, null), status);
      }
      throw new ApiError("network", "连不上服务器：" + errorText(err), 0);
    }
  );
}

function statusOf(req) {
  // response 在请求完成前是空的，失败时可能是 null —— 不能直接点属性
  if (!req || !req.response) return 0;
  var code = req.response.statusCode;
  return typeof code === "number" ? code : 0;
}

function errorText(err) {
  if (err && err.message) return err.message;
  return String(err);
}

/* 把 promise 的成败都收成一个值，避免一处失败拖垮整个小组件。
   刻意不用 Promise.allSettled（ES2020，不在 ES6 范围内）。 */
function settled(promise) {
  return promise.then(
    function (value) {
      return { ok: true, value: value };
    },
    function (error) {
      return { ok: false, error: error };
    }
  );
}

async function fetchTasks() {
  var orderedPath = "/api/tasks?view=ordered&status=open&limit=" + CONFIG.ORDERED_LIMIT;
  var unorderedPath = "/api/tasks?view=unordered&status=open&limit=" + CONFIG.UNORDERED_LIMIT;
  // limit=1 只是省流量：counts 是对全表算的，不受 limit 影响
  var draftsPath = "/api/ingest?status=pending&limit=1";

  var results = await Promise.all([
    settled(api(orderedPath)),
    settled(api(unorderedPath)),
    settled(api(draftsPath)),
  ]);

  var firstError = null;
  var okCount = 0;
  for (var i = 0; i < results.length; i++) {
    if (results[i].ok) okCount++;
    else if (!firstError) firstError = results[i].error;
  }

  // 鉴权错误是**全局**的：一个 401 说明整把密钥都不对。
  // 不能因为草稿箱恰好返回了 200，就渲染成"没有待办 🎉" ——
  // 那是在撒谎，而且用户会以为自己真的没事可做。
  for (var a = 0; a < results.length; a++) {
    if (!results[a].ok && isAuthError(results[a].error)) {
      return { ok: false, error: results[a].error };
    }
  }

  // 两个任务列表都拿不到 = 小组件的主体内容没了。
  // 同样不能显示"没有待办"，要走缓存或错误态。
  var tasksOk = results[0].ok || results[1].ok;
  if (!tasksOk) {
    return { ok: false, error: firstError };
  }

  var drafts = results[2].ok ? results[2].value : null;
  var counts = drafts && drafts.counts ? drafts.counts : {};

  return {
    ok: true,
    partial: okCount < 3,
    ordered: results[0].ok ? asArray(results[0].value) : [],
    unordered: results[1].ok ? asArray(results[1].value) : [],
    pending: typeof counts.pending === "number" ? counts.pending : 0,
    fetchedAt: new Date(),
  };
}

function isAuthError(error) {
  if (!error || error.kind !== "http") return false;
  return error.status === 401 || error.status === 403;
}

function asArray(value) {
  return Object.prototype.toString.call(value) === "[object Array]" ? value : [];
}

// ============================================================================
// 缓存：请求失败时至少还能显示上次的数据
// ============================================================================
/* 小组件进程里能不能读写 Scriptable 的本地文件**没有实机验证过**，
   所以这里读写都吞掉异常：拿不到就没有"上次数据"这一层降级，
   但主路径照常工作，绝不会因为缓存出问题而白屏。 */
function cacheFile() {
  try {
    var fm = FileManager.local();
    return fm.joinPath(fm.documentsDirectory(), "schedulekit-widget.json");
  } catch (err) {
    return null;
  }
}

function readCache() {
  try {
    var path = cacheFile();
    if (!path) return null;
    var fm = FileManager.local();
    if (!fm.fileExists(path)) return null;
    var raw = fm.readString(path);
    var data = JSON.parse(raw);
    if (!data || !data.fetchedAt) return null;
    data.fetchedAt = new Date(data.fetchedAt);
    return data;
  } catch (err) {
    return null;
  }
}

function writeCache(data) {
  try {
    var path = cacheFile();
    if (!path) return false;
    FileManager.local().writeString(path, JSON.stringify(data));
    return true;
  } catch (err) {
    return false;
  }
}

// ============================================================================
// 格式化
// ============================================================================
function formatter(dateFormat) {
  var f = new DateFormatter();
  f.dateFormat = dateFormat;
  try {
    f.timeZone = CONFIG.TIMEZONE;
  } catch (err) {
    /* 时区名无效就退回设备本地时区，不因为一个展示细节把小组件搞崩 */
  }
  return f;
}

function dueText(iso) {
  if (!iso) return "";
  var date = new Date(iso);
  if (isNaN(date.getTime())) return "";
  return formatter("MM-dd HH:mm").string(date);
}

function isOverdue(iso) {
  if (!iso) return false;
  var date = new Date(iso);
  return !isNaN(date.getTime()) && date.getTime() < Date.now();
}

var PRIORITY_LABELS = { 1: "Ⅰ", 2: "Ⅱ", 3: "Ⅲ", 4: "Ⅳ", 5: "Ⅴ" };

function priorityLabel(value) {
  return PRIORITY_LABELS[value] || String(value === null ? "" : value);
}

function staleText(fetchedAt) {
  var minutes = Math.max(0, Math.round((Date.now() - fetchedAt.getTime()) / 60000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return minutes + " 分钟前";
  var hours = Math.round(minutes / 60);
  if (hours < 24) return hours + " 小时前";
  return Math.round(hours / 24) + " 天前";
}

function textColor(light, dark) {
  try {
    return Color.dynamic(new Color(light), new Color(dark));
  } catch (err) {
    return Color.gray();
  }
}

var DIM = function () {
  return textColor("#6b7280", "#9aa1ac");
};
var FAINT = function () {
  return textColor("#9aa1ac", "#6b7280");
};
var STRONG = function () {
  return textColor("#1c1e21", "#f2f3f5");
};
var DANGER = function () {
  return textColor("#dc2626", "#f87171");
};
var ACCENT = function () {
  return textColor("#2563eb", "#60a5fa");
};

// ============================================================================
// 渲染
// ============================================================================
/* 距截止 <48h 用计时器（走秒），更远的用相对时间（"2 个月"）。
   两者都由 iOS 自己更新，所以不需要靠刷新来保持准确。 */
function styleCountdown(element, iso) {
  var date = new Date(iso);
  var hours = (date.getTime() - Date.now()) / 3600000;
  if (hours < 48) {
    element.applyTimerStyle();
  } else {
    element.applyRelativeStyle();
  }
  return element;
}

function addHeader(widget, data) {
  var header = widget.addStack();
  header.layoutHorizontally();
  header.centerAlignContent();

  var brand = header.addText("ScheduleKit");
  brand.font = Font.semiboldSystemFont(11);
  brand.textColor = DIM();

  header.addSpacer();

  if (data.pending > 0) {
    var badge = header.addText("● " + data.pending + " 条待确认");
    badge.font = Font.mediumSystemFont(11);
    badge.textColor = ACCENT();
    // 逐元素 URL 只有中号/大号支持；小号只有一个整体点击目标
    if (config.widgetFamily === "medium" || config.widgetFamily === "large") {
      badge.url = baseUrl() + "/drafts";
    }
  }
}

function addOrderedRow(widget, task) {
  var row = widget.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();

  var overdue = isOverdue(task.due_at);

  var when = row.addText(dueText(task.due_at));
  when.font = Font.mediumMonospacedSystemFont(11);
  when.textColor = overdue ? DANGER() : DIM();
  when.lineLimit = 1;

  row.addSpacer(6);

  var title = row.addText(String(task.title || "（无标题）"));
  title.font = Font.systemFont(12);
  title.textColor = STRONG();
  title.lineLimit = 1;
  title.minimumScaleFactor = 0.8;

  row.addSpacer(6);

  if (task.due_at) {
    var countdown = styleCountdown(row.addDate(new Date(task.due_at)), task.due_at);
    countdown.font = Font.mediumSystemFont(11);
    countdown.textColor = overdue ? DANGER() : DIM();
    countdown.lineLimit = 1;
  }
}

function addUnorderedRow(widget, task) {
  var row = widget.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();

  var badge = row.addText(priorityLabel(task.priority));
  badge.font = Font.boldSystemFont(12);
  badge.textColor = DIM();
  badge.lineLimit = 1;

  row.addSpacer(8);

  var title = row.addText(String(task.title || "（无标题）"));
  title.font = Font.systemFont(12.5);
  title.textColor = STRONG();
  title.lineLimit = 1;
  title.minimumScaleFactor = 0.8;
}

function addEmptyOrStale(widget, data, note) {
  if (note) {
    var warn = widget.addText(note);
    warn.font = Font.systemFont(10);
    warn.textColor = FAINT();
    warn.lineLimit = 1;
  }
  if (!data.ordered.length && !data.unordered.length) {
    var empty = widget.addText("没有待办 🎉");
    empty.font = Font.systemFont(12);
    empty.textColor = DIM();
  }
}

function addStaleFooter(widget, data) {
  // 注意"刚刚"要能接在"上次更新："后面读通，别再写成"数据是 刚刚的"
  var note = data.stale ? "⚠ 离线 · 上次更新：" + staleText(data.fetchedAt) : "⚠ 部分数据获取失败";
  var footer = widget.addText(note);
  footer.font = Font.systemFont(9.5);
  footer.textColor = FAINT();
  footer.lineLimit = 1;
}

function buildHomeWidget(family, data) {
  var widget = new ListWidget();
  widget.url = baseUrl() + "/";
  widget.setPadding(12, 14, 12, 14);
  widget.spacing = 5;

  addHeader(widget, data);

  if (data.ordered.length) {
    widget.addSpacer(1);
    for (var i = 0; i < data.ordered.length; i++) {
      addOrderedRow(widget, data.ordered[i]);
    }
  }

  if (data.unordered.length) {
    widget.addSpacer(4);
    for (var j = 0; j < data.unordered.length; j++) {
      addUnorderedRow(widget, data.unordered[j]);
    }
  }

  if (!data.ordered.length && !data.unordered.length) {
    widget.addSpacer(4);
    addEmptyOrStale(widget, data, null);
  }

  if (data.stale || data.partial) {
    widget.addSpacer();
    addStaleFooter(widget, data);
  }

  return widget;
}

function buildSmallWidget(data) {
  var widget = new ListWidget();
  widget.url = baseUrl() + "/";
  widget.setPadding(10, 12, 10, 12);
  widget.spacing = 4;

  if (data.pending > 0) {
    var badge = widget.addText("● " + data.pending + " 待确认");
    badge.font = Font.mediumSystemFont(10.5);
    badge.textColor = ACCENT();
    badge.lineLimit = 1;
  }

  var next = data.ordered.length ? data.ordered[0] : null;
  if (next) {
    var title = widget.addText(String(next.title || ""));
    title.font = Font.semiboldSystemFont(12.5);
    title.textColor = STRONG();
    title.lineLimit = 2;
    title.minimumScaleFactor = 0.75;

    var countdown = styleCountdown(widget.addDate(new Date(next.due_at)), next.due_at);
    countdown.font = Font.mediumSystemFont(11.5);
    countdown.textColor = isOverdue(next.due_at) ? DANGER() : DIM();
  } else if (data.unordered.length) {
    var first = data.unordered[0];
    var priority = widget.addText(priorityLabel(first.priority) + "  " + String(first.title || ""));
    priority.font = Font.semiboldSystemFont(12.5);
    priority.textColor = STRONG();
    priority.lineLimit = 3;
  } else {
    var none = widget.addText("没有待办");
    none.font = Font.systemFont(12);
    none.textColor = DIM();
  }

  if (data.stale || data.partial) {
    widget.addSpacer();
    addStaleFooter(widget, data);
  }

  return widget;
}

function buildAccessoryRectangular(data) {
  var widget = new ListWidget();
  widget.url = baseUrl() + "/";
  widget.spacing = 1;

  var next = data.ordered.length ? data.ordered[0] : null;

  if (next) {
    var title = widget.addText(String(next.title || ""));
    title.font = Font.semiboldSystemFont(13);
    title.lineLimit = 1;
    title.minimumScaleFactor = 0.8;

    var line = widget.addStack();
    line.layoutHorizontally();
    line.centerAlignContent();

    var countdown = styleCountdown(line.addDate(new Date(next.due_at)), next.due_at);
    countdown.font = Font.mediumSystemFont(12);
    countdown.lineLimit = 1;

    var absolute = line.addText(" · " + dueText(next.due_at));
    absolute.font = Font.systemFont(12);
    absolute.lineLimit = 1;
  } else if (data.unordered.length) {
    var first = data.unordered[0];
    var badge = widget.addText(priorityLabel(first.priority) + "  " + String(first.title || ""));
    badge.font = Font.semiboldSystemFont(13);
    badge.lineLimit = 2;

    var hint = widget.addText("没有带截止时间的任务");
    hint.font = Font.systemFont(11);
  } else {
    var none = widget.addText("没有待办");
    none.font = Font.systemFont(13);
  }

  if (data.pending > 0) {
    var pending = widget.addText("● " + data.pending + " 条待确认");
    pending.font = Font.systemFont(11);
    pending.lineLimit = 1;
  }

  return widget;
}

function buildAccessoryInline(data) {
  // 锁屏顶部那一行只能显示"一张图 + 一段文字"，多出来的元素会被系统丢掉
  var widget = new ListWidget();
  widget.url = baseUrl() + "/";
  var next = data.ordered.length ? data.ordered[0] : null;
  var label = widget.addText(
    next ? String(next.title || "").substring(0, 16) + " " + dueText(next.due_at) : "没有待办"
  );
  label.font = Font.systemFont(12);
  label.lineLimit = 1;
  return widget;
}

function buildAccessoryCircular(data) {
  // 圆形只能放极短的内容：有截止时间就放天数，否则放条数
  var widget = new ListWidget();
  widget.url = baseUrl() + "/";
  var label = widget.addText(circularLabel(data));
  label.font = Font.boldSystemFont(13);
  label.centerAlignText();
  return widget;
}

function circularLabel(data) {
  var next = data.ordered.length ? data.ordered[0] : null;
  if (next) {
    var days = Math.ceil((new Date(next.due_at).getTime() - Date.now()) / 86400000);
    if (days < 0) return "逾期";
    if (days === 0) return "今天";
    return days + "天";
  }
  var total = data.unordered.length;
  return total ? total + "条" : "空";
}

function buildErrorWidget(message) {
  var widget = new ListWidget();
  widget.url = baseUrl() + "/";
  widget.setPadding(12, 14, 12, 14);

  var title = widget.addText("ScheduleKit");
  title.font = Font.semiboldSystemFont(11);
  title.textColor = FAINT();

  widget.addSpacer(4);

  var text = widget.addText(message);
  text.font = Font.systemFont(11.5);
  text.textColor = DANGER();
  text.lineLimit = 4;
  text.minimumScaleFactor = 0.7;

  return widget;
}

function buildWidget(family, data) {
  if (family === "accessoryRectangular") return buildAccessoryRectangular(data);
  if (family === "accessoryInline") return buildAccessoryInline(data);
  if (family === "accessoryCircular") return buildAccessoryCircular(data);
  if (family === "small") return buildSmallWidget(data);
  // medium 是主场景；large / extraLarge 直接复用（元素会自然留白，
  // 不去猜像素高度 —— 猜错了比留白难看）
  return buildHomeWidget(family, data);
}

// ============================================================================
// 主流程
// ============================================================================
async function loadData() {
  var fresh = await fetchTasks();

  if (fresh.ok) {
    // 顺序视图只请求了 LIMIT 条，缓存里存的也是这些，够下次失败时展示
    var snapshot = {
      ordered: fresh.ordered,
      unordered: fresh.unordered,
      pending: fresh.pending,
      fetchedAt: fresh.fetchedAt.toISOString(),
    };
    writeCache(snapshot);
    return {
      ok: true,
      partial: fresh.partial,
      stale: false,
      ordered: fresh.ordered,
      unordered: fresh.unordered,
      pending: fresh.pending,
      fetchedAt: fresh.fetchedAt,
    };
  }

  var cached = readCache();
  if (cached) {
    return {
      ok: true,
      partial: false,
      stale: true,
      ordered: asArray(cached.ordered),
      unordered: asArray(cached.unordered),
      pending: typeof cached.pending === "number" ? cached.pending : 0,
      fetchedAt: cached.fetchedAt,
    };
  }

  return { ok: false, error: fresh.error };
}

async function widgetData() {
  var data = await loadData();
  if (!data.ok) {
    return { failed: true, message: data.error ? data.error.message : "未知错误" };
  }
  return data;
}

// ============================================================================
// 在 App 里运行：自检 + 预览
// ============================================================================
async function selfTest() {
  var lines = [];

  var urlOk = /^https:\/\//.test(CONFIG.BASE_URL || "");
  lines.push((urlOk ? "✓" : "✗") + " BASE_URL：" + (CONFIG.BASE_URL || "（空）"));

  var key = CONFIG.API_KEY || "";
  var keyOk = /^sk_/.test(key) && key.indexOf("在这里") === -1;
  lines.push(
    (keyOk ? "✓" : "✗") + " API_KEY：" + (keyOk ? "已填写（" + key.substring(0, 6) + "…）" : "还没填")
  );

  if (!urlOk || !keyOk) {
    lines.push("");
    lines.push("先把 CONFIG 填好再往下测。");
    return lines.join("\n");
  }

  // 1) 存活（不需要密钥）
  try {
    var health = new Request(baseUrl() + "/healthz");
    health.timeoutInterval = CONFIG.TIMEOUT_SECONDS;
    var info = await health.loadJSON();
    if (statusOf(health) === 200 && info && info.status === "ok") {
      lines.push("✓ /healthz 可达 · 版本 " + info.version + " · " + info.timezone);
    } else {
      lines.push("✗ /healthz 异常：HTTP " + statusOf(health));
    }
  } catch (err) {
    lines.push("✗ /healthz 失败：" + errorText(err));
  }

  // 2) 鉴权 + 三个数据源
  var ordered = await settled(api("/api/tasks?view=ordered&status=open&limit=5"));
  if (ordered.ok) {
    lines.push("✓ 鉴权通过 · 有序表 " + ordered.value.length + " 条");
  } else {
    lines.push("✗ 鉴权/取数失败：" + ordered.error.message);
  }

  var unordered = await settled(api("/api/tasks?view=unordered&status=open&limit=5"));
  if (unordered.ok) {
    lines.push("✓ 无序表 " + unordered.value.length + " 条");
  } else {
    lines.push("✗ 无序表失败：" + unordered.error.message);
  }

  var drafts = await settled(api("/api/ingest?status=pending&limit=1"));
  if (drafts.ok) {
    var counts = drafts.value && drafts.value.counts ? drafts.value.counts : {};
    lines.push("✓ 草稿箱 · 待确认 " + (counts.pending || 0) + " 条");
  } else {
    lines.push("✗ 草稿箱失败：" + drafts.error.message);
  }

  // 3) 缓存往返（小组件进程里能否读写本地文件尚未实机验证，这里只测 App 内）
  var probe = { ordered: [], unordered: [], pending: 0, fetchedAt: new Date().toISOString() };
  if (writeCache(probe) && readCache()) {
    lines.push("✓ 缓存可读写");
  } else {
    lines.push("· 缓存不可用（不影响主流程，只是失败时少一层降级）");
  }

  // 4) 各尺寸渲染不抛异常
  var families = ["small", "medium", "large", "accessoryRectangular", "accessoryCircular", "accessoryInline"];
  var renderFails = [];
  var sample = {
    ok: true,
    stale: false,
    partial: false,
    ordered: ordered.ok ? ordered.value : [],
    unordered: unordered.ok ? unordered.value : [],
    pending: drafts.ok && drafts.value && drafts.value.counts ? drafts.value.counts.pending || 0 : 0,
    fetchedAt: new Date(),
  };
  for (var i = 0; i < families.length; i++) {
    try {
      buildWidget(families[i], sample);
    } catch (err) {
      renderFails.push(families[i] + "(" + errorText(err) + ")");
    }
  }
  lines.push(renderFails.length ? "✗ 渲染异常：" + renderFails.join("、") : "✓ 六种尺寸渲染均通过");

  return lines.join("\n");
}

async function runInApp() {
  var report = await selfTest();

  var alert = new Alert();
  alert.title = "ScheduleKit 小组件自检";
  alert.message = report;
  alert.addAction("预览中号");
  alert.addAction("预览锁屏");
  alert.addCancelAction("关闭");

  var choice = await alert.presentAlert();

  if (choice === 0) {
    (await buildWidget("medium", await widgetData())).presentMedium();
  } else if (choice === 1) {
    (await buildWidget("accessoryRectangular", await widgetData())).presentAccessoryRectangular();
  }
}

// ============================================================================
// 入口
// ============================================================================
if (config.widgetFamily === null) {
  // 在 App 里手动运行 —— 走自检，不要直接当小组件渲染（那样什么都看不到）
  await runInApp();
} else {
  var data = await widgetData();
  var widget = data.failed ? buildErrorWidget(data.message) : buildWidget(config.widgetFamily, data);
  widget.refreshAfterDate = new Date(Date.now() + CONFIG.REFRESH_MINUTES * 60000);
  Script.setWidget(widget);
  Script.complete();
}
