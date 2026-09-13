# Scriptable 小组件设计（v2，本期未实现）

本期不做，但架构已经为它留好了位置。这份文档记录**已查实的机制与限制**，
避免将来重新调研，也避免对它抱有不切实际的期待。

---

## 一、为什么是 Scriptable

iOS 不允许第三方代码常驻内存，桌面小组件只能由「App 的 Widget Extension」提供。
自己写原生 App 需要 Mac + Xcode + 99$/年开发者账号 —— 对一个个人任务应用太重。

[Scriptable](https://scriptable.app) 是一个免费 App Store 应用，它自带
Widget Extension，并允许用户用 **JavaScript** 驱动它。于是链路是：

```
服务端 GET /api/tasks  (只读密钥)
        ↑ HTTPS
Scriptable 脚本（JS，约 100 行）
        ↓ Script.setWidget()
iOS 桌面 / 锁屏小组件
```

**其他路径为什么不行**：

- 快捷指令当小组件只能显示静态名称，无法显示动态内容
- iOS 上 PWA **不能**添加桌面小组件
- Live Activity / 灵动岛必须原生 App
- 桌面大字号小组件同理

---

## 二、机制（来自官方文档，已核实）

| 能力 | API | 说明 |
|---|---|---|
| 取数 | `Request` / `fetch` | 可带自定义请求头，所以能用 `X-API-Key` |
| 存密钥 | `Keychain.set/get` | 不要把密钥硬编码进脚本 |
| 渲染 | `new ListWidget()` + `addText` / `addStack` / `addSpacer` | 支持小/中/大三种尺寸 |
| 提交 | `Script.setWidget(w)` | |
| 点击跳转 | `widget.url` | 可指向 PWA，或 `shortcuts://run-shortcut?name=…` |
| 锁屏 | `presentAccessoryRectangular()` 等 | iOS 16+ |
| 刷新提示 | `widget.refreshAfterDate` | **只是提示**，见下 |

---

## 三、必须知道的限制

### 1. 不是实时的

官方文档原文：

> the rate at which the widget refreshes is largely determined by the operating system
>
> The refresh rate of a widget is partly up to iOS/iPadOS. For example, a widget may not
> refresh if the device is low on battery or the user is rarely looking at the widget.

`refreshAfterDate` 是「最早可再刷新时间」的**下限提示**，不是保证。
实际经验约 **15–60 分钟**一次。所以小组件适合看「今天要交什么」，
**不适合**当作精确到分钟的提醒。

### 2. 小组件里的按钮不能执行代码

iOS 17 的交互式小组件需要 App Intents，Scriptable 不提供。
所以勾选完成这类操作做不到 —— 点击只能跳转（打开 WebUI 或跑一个快捷指令）。

### 3. 内存有限

文档明确写着 widget 上下文有内存限制，超了会崩溃不渲染。不要塞长列表。

---

## 四、实现计划

### 服务端：不需要新接口

最初计划加一个 `/api/widget/summary`，后来否掉了 —— 现有接口够用：

```
GET /api/tasks?view=ordered&status=open&limit=3
```

用**只读密钥**（`read_only: true`）即可。少一个接口就少一处要维护的东西。

### 脚本结构（约 100 行）

```js
// 1. 取密钥（不硬编码）
const key = Keychain.get("schedulekit.key")

// 2. 拉数据
const req = new Request(`${BASE}/api/tasks?view=ordered&status=open&limit=3`)
req.headers = { "X-API-Key": key }
const tasks = await req.loadJSON()

// 3. 渲染
const w = new ListWidget()
w.url = BASE                       // 点击打开 WebUI
const head = w.addText("接下来")
head.font = Font.boldSystemFont(11)
head.textColor = Color.gray()
for (const t of tasks) {
  const line = w.addText(`· ${t.title}`)
  line.font = Font.systemFont(12)
  line.lineLimit = 1
}
if (!tasks.length) w.addText("没有带截止时间的任务")

// 4. 提交，并请求 15 分钟后可再刷新
Script.setWidget(w)
w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000)
Script.complete()
```

锁屏版本把最后两行换成 `w.presentAccessoryRectangular()`，内容精简到
「下一个 deadline + 倒计时」最合适（锁屏空间小，且这恰好是最高价值的信息）。

### 键盘/点击的增强

`widget.url` 可以填 `shortcuts://run-shortcut?name=拍照记任务`，
点小组件直接进拍照上传流程 —— 这是 iOS 上最接近「随手记」的路径。

---

## 五、你需要做的

1. App Store 装 Scriptable（免费）
2. 服务端 `/settings` 创建一个**只读** API Key
3. 把脚本粘进 Scriptable，填入 `BASE` 与密钥（密钥走 `Keychain.set` 存一次）
4. 长按桌面 → 添加小组件 → 选 Scriptable → 选择该脚本

脚本我会在 v2 一并提供，含锁屏版本。
