#!/usr/bin/env node
/* ScheduleKit 小组件预览器
 * ============================================================================
 *
 * 在电脑上把 ScheduleKitWidget.js 跑一遍，把渲染结果打成终端文本。
 *
 * 存在的理由：小组件在手机上出错时几乎看不到原因（方块空白或干脆不渲染），
 * 而这支脚本里任何一处语法错误、字段名写错、空数组没兜住，都会是那个下场。
 * 这个预览器用 stub 顶掉 Scriptable 的 API，让同一份脚本在 Node 里真跑一次。
 *
 * ## 用法
 *
 *   # 用 fixture 渲染（不需要服务端，也不需要密钥）—— 测试走的就是这条路
 *   node clients/ios/preview.mjs --fixtures <目录> --family medium
 *
 *   # 打真实服务器（需要脚本里已填好只读密钥）
 *   node clients/ios/preview.mjs --live --family accessoryRectangular
 *
 * ## fixture 目录约定
 *
 *   ordered.json    GET /api/tasks?view=ordered&status=open&...  的响应体
 *   unordered.json  GET /api/tasks?view=unordered&status=open&... 的响应体
 *   drafts.json     GET /api/ingest?status=pending&limit=1        的响应体
 *   可选：_status.json  {"tasks": 401, "drafts": 500}
 *         把某类请求模拟成失败；再可选 _offline.json 让请求直接抛错
 */

import fs from "node:fs";
import path from "node:path";

const SCRIPT = path.join(import.meta.dirname, "ScheduleKitWidget.js");

// ---------------------------------------------------------------------------
// 参数
// ---------------------------------------------------------------------------
function parseArgs(argv) {
  const args = { family: "medium", fixtures: null, live: false, json: false, seedCache: false };
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === "--family") args.family = argv[++i];
    else if (token === "--fixtures") args.fixtures = argv[++i];
    else if (token === "--live") args.live = true;
    else if (token === "--json") args.json = true;
    else if (token === "--seed-cache") args.seedCache = true;
    else if (token === "--help" || token === "-h") args.help = true;
  }
  return args;
}

const USAGE = [
  "用法：",
  "  node preview.mjs --fixtures <目录> [--family medium|small|large|accessoryRectangular|accessoryCircular|accessoryInline]",
  "  node preview.mjs --live [--family ...]",
  "",
  "fixture 目录需要 ordered.json / unordered.json / drafts.json，",
  "可选 _status.json（模拟 HTTP 错误）与 _offline.json（模拟断网）。",
  "",
  "选项：",
  "  --seed-cache   先用 fixture 里的数据预置一份缓存，再跑脚本。",
  "                 配合 _offline.json 就能验证“断网时显示上次数据”这条降级路径。",
  "  --json         打印渲染元素树而不是终端排版。",
  "",
  "联调真实服务器时用环境变量给密钥，不要写进脚本：",
  "  SK_API_KEY=sk_xxx node preview.mjs --live",
].join("\n");

// ---------------------------------------------------------------------------
// Scriptable stub
// ---------------------------------------------------------------------------
/* 渲染元素树。preview 只关心"有哪些元素、什么文字、多宽"，不看像素。 */
function makeElement(type, props) {
  const element = Object.assign(
    {
      type,
      children: [],
      text: "",
      url: null,
      lineLimit: 0,
      minimumScaleFactor: 1,
      font: null,
      textColor: null,
      dateStyle: null,
      date: null,
    },
    props || {}
  );
  element.addText = function (text) {
    const child = makeElement("text", { text: String(text) });
    element.children.push(child);
    return child;
  };
  element.addDate = function (date) {
    const child = makeElement("date", { date });
    element.children.push(child);
    return child;
  };
  element.addStack = function () {
    const child = makeElement("stack");
    element.children.push(child);
    return child;
  };
  element.addSpacer = function () {
    const child = makeElement("spacer");
    element.children.push(child);
    return child;
  };
  element.layoutHorizontally = function () {
    element.horizontal = true;
  };
  element.layoutVertically = function () {
    element.horizontal = false;
  };
  element.centerAlignContent = function () {
    element.centered = true;
  };
  element.setPadding = function () {};
  element.useDefaultPadding = function () {};
  element.addAccessoryWidgetBackground = false;
  element.applyTimeStyle = function () {
    element.dateStyle = "time";
  };
  element.applyDateStyle = function () {
    element.dateStyle = "date";
  };
  element.applyRelativeStyle = function () {
    element.dateStyle = "relative";
  };
  element.applyOffsetStyle = function () {
    element.dateStyle = "offset";
  };
  element.applyTimerStyle = function () {
    element.dateStyle = "timer";
  };
  element.leftAlignText = function () {};
  element.centerAlignText = function () {
    element.centered = true;
  };
  element.rightAlignText = function () {};
  element.presentSmall = async () => {};
  element.presentMedium = async () => {};
  element.presentLarge = async () => {};
  element.presentAccessoryRectangular = async () => {};
  return element;
}

function makeColor(value) {
  return { value: String(value) };
}

function makeFont(size, weight) {
  return { size, weight };
}

function makeDateFormatter() {
  const formatter = {
    dateFormat: "yyyy-MM-dd",
    timeZone: null,
    string(date) {
      return formatDate(date, formatter.dateFormat, formatter.timeZone);
    },
  };
  return formatter;
}

function formatDate(date, pattern, timeZone) {
  const options = { timeZone: timeZone || undefined, hour12: false };
  const map = {
    "MM-dd HH:mm": { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" },
    "yyyy-MM-dd": { year: "numeric", month: "2-digit", day: "2-digit" },
  };
  const parts = new Intl.DateTimeFormat("en-CA", Object.assign(options, map[pattern] || map["yyyy-MM-dd"]))
    .formatToParts(date)
    .reduce((acc, part) => Object.assign(acc, { [part.type]: part.value }), {});
  if (pattern === "MM-dd HH:mm") {
    return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  }
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function makeFileManager() {
  /* 内存文件系统：预览器不碰真实磁盘，也顺便证明脚本不依赖缓存的持久性 */
  const files = makeFileManager.store;
  const fm = {
    documentsDirectory: () => "/preview-documents",
    joinPath: (...bits) => bits.join("/"),
    fileExists: (p) => Object.prototype.hasOwnProperty.call(files, p),
    readString: (p) => {
      if (!Object.prototype.hasOwnProperty.call(files, p)) throw new Error("no such file");
      return files[p];
    },
    writeString: (p, text) => {
      files[p] = text;
    },
  };
  return fm;
}
makeFileManager.store = {};

function buildStubs(options) {
  const captured = { widget: null, completed: false, logs: [] };

  class Request {
    constructor(url) {
      this.url = url;
      this.headers = {};
      this.method = "GET";
      this.timeoutInterval = 60;
      this.response = null;
    }

    async loadJSON() {
      // --live：交给真实 fetch，并**尊重脚本设置的 timeoutInterval**
      if (options.fetchJson) {
        try {
          const body = await options.fetchJson(this.url, this.timeoutInterval);
          this.response = { statusCode: 200, headers: {}, url: this.url };
          return body;
        } catch (error) {
          this.response = error && error.status ? { statusCode: error.status } : null;
          throw error;
        }
      }

      if (options.offline) {
        this.response = null;
        throw new Error("The request timed out.");
      }
      const status = options.statusFor(this.url);
      this.response = { statusCode: status, headers: {}, url: this.url };
      const body = options.bodyFor(this.url);
      if (status >= 400) {
        // 服务端 4xx/5xx 返回的是 JSON 错误体；网关错误页则不是 JSON
        if (options.htmlErrors) {
          throw new Error("The data couldn't be read because it isn't in the correct format.");
        }
        return body;
      }
      if (body === undefined) {
        throw new Error("The data couldn't be read because it isn't in the correct format.");
      }
      return body;
    }
  }

  const stubs = {
    config: { widgetFamily: options.family, runsInWidget: true, runsInApp: false },
    Request,
    ListWidget: function () {
      return makeElement("widget");
    },
    Color: Object.assign(makeColor, {
      dynamic: (light, dark) => makeColor(`dynamic(${light.value}|${dark.value})`),
      gray: () => makeColor("gray"),
      red: () => makeColor("red"),
      white: () => makeColor("white"),
      black: () => makeColor("black"),
    }),
    Font: {
      systemFont: (size) => makeFont(size, "regular"),
      boldSystemFont: (size) => makeFont(size, "bold"),
      semiboldSystemFont: (size) => makeFont(size, "semibold"),
      mediumSystemFont: (size) => makeFont(size, "medium"),
      mediumMonospacedSystemFont: (size) => makeFont(size, "mono-medium"),
      monospacedSystemFont: (size) => makeFont(size, "mono"),
    },
    DateFormatter: function () {
      return makeDateFormatter();
    },
    FileManager: Object.assign(
      function () {
        return makeFileManager();
      },
      { local: () => makeFileManager() }
    ),
    Alert: function () {
      return {
        title: "",
        message: "",
        addAction() {},
        addCancelAction() {},
        async presentAlert() {
          return -1;
        },
        async presentSheet() {
          return -1;
        },
      };
    },
    UITable: function () {
      return { addRow() {}, async present() {} };
    },
    Script: {
      setWidget(widget) {
        captured.widget = widget;
      },
      complete() {
        captured.completed = true;
      },
    },
    console: {
      log: (...args) => captured.logs.push(args.join(" ")),
    },
  };

  return { stubs, captured };
}

// ---------------------------------------------------------------------------
// 执行脚本
// ---------------------------------------------------------------------------
async function runScript(stubs) {
  const source = fs.readFileSync(SCRIPT, "utf8");
  // 把脚本包进一个异步函数，Scriptable 的全局对象作为参数注入。
  // 脚本本身不含 import/export，所以这样加载是安全的。
  const names = Object.keys(stubs);
  const values = names.map((name) => stubs[name]);
  const factory = new Function(
    ...names,
    `return (async () => {\n${source}\n})();`
  );
  return factory(...values);
}

// ---------------------------------------------------------------------------
// fixture 载入
// ---------------------------------------------------------------------------
function loadFixtures(dir) {
  const read = (name) => {
    const file = path.join(dir, name);
    if (!fs.existsSync(file)) return undefined;
    return JSON.parse(fs.readFileSync(file, "utf8"));
  };

  const ordered = read("ordered.json") || [];
  const unordered = read("unordered.json") || [];
  const drafts = read("drafts.json") || { drafts: [], total: 0, counts: {} };
  const statuses = read("_status.json") || {};
  const offline = fs.existsSync(path.join(dir, "_offline.json"));
  const htmlErrors = fs.existsSync(path.join(dir, "_html_error.json"));

  return {
    offline,
    htmlErrors,
    statusFor(url) {
      if (url.indexOf("/api/ingest") !== -1) return statuses.drafts || 200;
      return statuses.tasks || 200;
    },
    bodyFor(url) {
      if (url.indexOf("/healthz") !== -1) {
        return { status: "ok", version: "0.1.0", timezone: "Asia/Shanghai" };
      }
      if (url.indexOf("/api/ingest") !== -1) {
        if ((statuses.drafts || 200) >= 400) return { detail: "服务端错误" };
        return drafts;
      }
      if (url.indexOf("view=ordered") !== -1) {
        if ((statuses.tasks || 200) >= 400) return { detail: "缺少或无效的凭据" };
        return ordered;
      }
      if (url.indexOf("view=unordered") !== -1) {
        if ((statuses.tasks || 200) >= 400) return { detail: "缺少或无效的凭据" };
        return unordered;
      }
      return undefined;
    },
  };
}

function loadLive() {
  const source = fs.readFileSync(SCRIPT, "utf8");
  const base = /BASE_URL:\s*"([^"]+)"/.exec(source);
  const key = /API_KEY:\s*"([^"]+)"/.exec(source);
  if (!base || !key) throw new Error("脚本里读不到 CONFIG.BASE_URL / CONFIG.API_KEY");

  // 环境变量优先：联调时不必把真密钥写进脚本（那是会被提交的文件）
  const baseUrl = (process.env.SK_BASE_URL || base[1]).replace(/\/+$/, "");
  const apiKey = process.env.SK_API_KEY || key[1];

  if (/在这里/.test(apiKey)) {
    throw new Error(
      "没有可用的 API Key：CONFIG.API_KEY 还是占位符，且未设置环境变量 SK_API_KEY"
    );
  }

  return {
    offline: false,
    htmlErrors: false,
    statusFor: () => 200,
    bodyFor: () => undefined,
    apiKey,
    baseUrl,
  };
}

// ---------------------------------------------------------------------------
// 渲染成终端文本
// ---------------------------------------------------------------------------
const ANSI = {
  reset: "\u001b[0m",
  dim: "\u001b[2m",
  bold: "\u001b[1m",
  red: "\u001b[31m",
  blue: "\u001b[34m",
};

function describeDate(element) {
  const iso = element.date instanceof Date ? element.date.toISOString() : String(element.date);
  const label = iso.replace("T", " ").slice(0, 16) + "Z";
  switch (element.dateStyle) {
    case "timer":
      return `«走秒计时器 → ${label}»`;
    case "relative":
      return `«相对时间 → ${label}»`;
    case "time":
      return `«时间 → ${label}»`;
    default:
      return `«${label}»`;
  }
}

function flatten(element, depth, out, horizontal) {
  for (const child of element.children) {
    if (child.type === "spacer") {
      if (horizontal) out.push({ kind: "gap" });
      else out.push({ kind: "line", text: "" });
      continue;
    }
    if (child.type === "stack") {
      if (child.horizontal) {
        const parts = [];
        flattenInline(child, parts);
        out.push({ kind: "line", text: parts.join("  "), badge: child.children.some((c) => c.url) });
      } else {
        flatten(child, depth + 1, out, false);
      }
      continue;
    }
    if (child.type === "date") {
      out.push({ kind: "line", text: describeDate(child), dim: true });
      continue;
    }
    out.push({ kind: "line", text: child.text, dim: false });
  }
  return out;
}

function flattenInline(element, parts) {
  for (const child of element.children) {
    if (child.type === "spacer") continue;
    if (child.type === "date") {
      parts.push(describeDate(child));
      continue;
    }
    if (child.type === "stack") {
      flattenInline(child, parts);
      continue;
    }
    if (child.text) parts.push(child.text);
  }
}

function render(element, family) {
  const lines = [];
  lines.push(`${ANSI.dim}┌─ ${family} ─ widget.url = ${element.url || "(未设置)"}${ANSI.reset}`);

  if (element.url) lines.push(`${ANSI.dim}│${ANSI.reset}`);

  const out = flatten(element, 0, [], false);
  for (const item of out) {
    if (item.kind === "gap") {
      lines.push(`${ANSI.dim}│${ANSI.reset}`);
      continue;
    }
    if (!item.text) {
      lines.push(`${ANSI.dim}│${ANSI.reset}`);
      continue;
    }
    const paint = item.dim ? ANSI.dim : "";
    const badge = item.badge ? `  ${ANSI.blue}[此处可点 → /drafts]${ANSI.reset}` : "";
    lines.push(`${ANSI.dim}│${ANSI.reset} ${paint}${item.text}${ANSI.reset}${badge}`);
  }

  lines.push(`${ANSI.dim}└─ refreshAfterDate = ${
    element.refreshAfterDate ? element.refreshAfterDate.toISOString() : "(未设置)"
  }${ANSI.reset}`);
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    console.log(USAGE);
    return 0;
  }
  if (!args.fixtures && !args.live) {
    console.log(USAGE);
    return 2;
  }

  let options;
  let live = null;
  if (args.live) {
    live = loadLive();
    options = {
      offline: false,
      htmlErrors: false,
      statusFor: () => 200,
      bodyFor: () => undefined,
      // 真实网络：按脚本设的 timeoutInterval 加超时，行为与小组件一致
      async fetchJson(url, timeoutSeconds) {
        const response = await fetch(url, {
          headers: { Authorization: `Bearer ${live.apiKey}` },
          signal: AbortSignal.timeout(Math.max(1, timeoutSeconds) * 1000),
        });
        if (!response.ok) {
          const error = new Error(`HTTP ${response.status}`);
          error.status = response.status;
          throw error;
        }
        return response.json();
      },
    };
  } else {
    options = loadFixtures(args.fixtures);
  }

  const { stubs, captured } = buildStubs(Object.assign({ family: args.family }, options));

  if (args.seedCache && !args.live) {
    // 预置缓存：让"请求失败 → 回退到上次数据"这条路径可以被验证。
    // 缓存文件名与路径必须和脚本里 cacheFile() 的算法一致。
    const counts = options.bodyFor("https://x/api/ingest").counts || {};
    makeFileManager.store["/preview-documents/schedulekit-widget.json"] = JSON.stringify({
      ordered: options.bodyFor("https://x/api/tasks?view=ordered") || [],
      unordered: options.bodyFor("https://x/api/tasks?view=unordered") || [],
      pending: counts.pending || 0,
      // 固定 42 分钟前，这样 stale 文案是可断言的
      fetchedAt: new Date(Date.now() - 42 * 60000).toISOString(),
    });
  }

  try {
    await runScript(stubs);
  } catch (error) {
    console.error(`${ANSI.red}脚本抛异常：${error && error.message}${ANSI.reset}`);
    console.error(error && error.stack);
    return 1;
  }

  if (!captured.widget) {
    console.error(`${ANSI.red}脚本没有调用 Script.setWidget()${ANSI.reset}`);
    return 1;
  }

  if (args.json) {
    console.log(JSON.stringify(captured.widget, (key, value) => (key === "date" ? String(value) : value), 2));
  } else {
    console.log(render(captured.widget, args.family));
  }
  return 0;
}

main().then(
  (code) => {
    // 用 exitCode 而不是 process.exit()：后者会在 undici 的 keep-alive
    // 连接还没关掉时硬切，Node 24 上会打出一行 libuv 断言
    // （Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)）并给个假失败码。
    process.exitCode = code;
  },
  (error) => {
    console.error(error);
    process.exitCode = 1;
  }
);
