# Scriptable 小组件设计（v2）

**已实现** —— 代码在 [`clients/ios/`](../clients/ios/)，安装步骤看那里的 README。
这份文档记录**已查实的机制与限制**，以及为什么最后是这么做的。

---

## 一、为什么是 Scriptable

iOS 不允许第三方代码常驻内存，桌面小组件只能由「App 的 Widget Extension」提供。
自己写原生 App 需要 Mac + Xcode + 99 美元/年开发者账号 —— 对一个个人任务应用太重。

[Scriptable](https://scriptable.app) 是一个免费 App Store 应用，它自带
Widget Extension，并允许用户用 **JavaScript** 驱动它。

**其他路径为什么不行**：

- 快捷指令当小组件只能显示静态名称，无法显示动态内容
- iOS 上 PWA **不能**添加桌面小组件
- Live Activity / 灵动岛必须原生 App

---

## 二、机制（来自官方文档，已核实）

| 能力 | API | 说明 |
|---|---|---|
| 取数 | `Request` | `headers` 可自定义，所以能用 `Authorization: Bearer` |
| 超时 | `Request.timeoutInterval` | **默认 60 秒**，对小组件太长了 |
| 证书校验 | `Request.allowInsecureRequest` | 默认拒绝无效证书 —— 我们的证书有效，不需要打开 |
| 渲染 | `new ListWidget()` + `addText` / `addStack` / `addSpacer` / `addDate` | |
| 尺寸 | `config.widgetFamily` | `small` / `medium` / `large` / `extraLarge` / `accessoryRectangular` / `accessoryInline` / `accessoryCircular` / `null` |
| 锁屏 | `config.runsInAccessoryWidget` | 区分锁屏与桌面 |
| 点击跳转 | `widget.url`；`WidgetText.url` | 逐元素 URL **仅中号/大号**支持 |
| 刷新提示 | `widget.refreshAfterDate` | **只是下限提示**，见下 |
| 存密钥 | `Keychain` / 脚本常量 | 本项目选了脚本常量，理由见第五节 |

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

**刚录入完任务，小组件不会立刻更新。** Scriptable 没有强制刷新 API。
急用时在 Scriptable 里跑一次脚本预览即可。

### 2. 小组件里的按钮不能执行代码

iOS 17 的交互式小组件需要 App Intents，Scriptable 不提供 ——
它的渲染 API 里根本没有按钮类型（官方文档的 ListWidget 一节可以核对）。
所以勾选完成这类操作做不到，点击只能跳转。

这是本项目里小组件与 Windows 悬浮窗最大的差别：悬浮窗有读写密钥、能勾选；
小组件只能看。

### 3. 时间预算很紧

iOS 给小组件构建时间线的预算很紧（原生 WidgetKit 约 5 秒）。
`Request` 默认超时 60 秒，卡满的话小组件会被系统掐死 ——
**连缓存的兜底都来不及渲染**。所以脚本把超时压到 4 秒。

实测从国内到本项目的 VPS（含 DuckDNS 未缓存解析 200–780ms），
三个并行请求约 1.2–1.7 秒，4 秒有充足余量。

### 4. 内存有限

文档明确写着 widget 上下文有内存限制，超了会崩溃不渲染。不要塞长列表。

### 5. 倒计时反而是实时的

`WidgetDate`（`widget.addDate()`）渲染出来的时间由 iOS 自己维护：

| 方法 | 效果 |
|---|---|
| `applyTimerStyle()` | 走秒计时，如 `2:32` / `36:59:01` |
| `applyRelativeStyle()` | 如 `6 days` / `1 month` |

**不需要重新拉数据就会自己更新。** 所以「还剩多久」永远准，
只有任务列表本身可能滞后。实现里距截止 48 小时内用计时器、更远的用相对时间。

---

## 四、服务端：零改动

最初计划加一个 `/api/widget/summary`，最后否掉了 —— 现有接口够用：

```
GET /api/tasks?view=ordered&status=open&limit=2
GET /api/tasks?view=unordered&status=open&limit=2
GET /api/ingest?status=pending&limit=1        → counts.pending
```

三个请求**并行**发（约 1 个 RTT）。用**只读密钥**即可 —— 三个端点都只要求
`AuthDep`。少一个接口就少一处要维护的东西。

> `limit=1` 只是省流量：`counts` 是对全表算的，不受 `limit` 影响。

---

## 五、两个实现上的选择

### 密钥放在脚本常量里，而不是 Keychain

- 脚本文件本身就在 Scriptable 的沙盒里，与放进 Keychain 是**同一层保护**
  （Scriptable 的 Keychain 不是 iCloud 钥匙串，只在 App 内部）
- 而"小组件进程里能不能读到 Keychain"没有实机验证过
- 少一个不确定性，就少一种"桌面上莫名空白"的可能

代价是打开脚本能看到密钥。用**只读**密钥把这个代价压到最低。

### 缓存读写全部吞异常

小组件进程里能否读写 Scriptable 的本地文件同样没实机验证过。所以
`readCache` / `writeCache` 都包在 try/catch 里：拿不到就没有"上次数据"
这一层降级，但**主路径照常工作**，不会因为缓存问题白屏。

---

## 六、渲染与失败模式

| 尺寸 | 内容 |
|---|---|
| **中号** | 有序 2 条（时间 + 标题 + 走动的倒计时）+ 无序 2 条（档位 + 标题）+ 待确认角标 |
| **锁屏矩形** | 最近一条 deadline + 倒计时 + 绝对时间 |
| 小号 | 最近一条 + 待确认角标 |
| 大号 / 超大 | 复用中号排版（元素自然留白，不去猜像素高度） |
| 圆形 / 内联 | 极简：天数或条数 |

角标单独链到 `/drafts`（逐元素 URL），点其他地方进任务列表。

失败一律给**明确的话**，不留空白：

| 情况 | 显示 |
|---|---|
| 密钥无效 | `密钥无效或已吊销 —— 去服务端 /settings 重新创建一个只读密钥` |
| 连不上 | `连不上服务器：…` |
| 5xx | `服务器返回 HTTP 502` |
| 断网但有缓存 | 上次数据 + `⚠ 离线 · 上次更新：42 分钟前` |
| 真的没待办 | `没有待办 🎉` |

**`没有待办` 与 `密钥无效` 必须分开。** 开发时实测抓到过一个 bug：
401 时草稿箱那个请求恰好返回 200，于是旧的"至少一个成功就算成功"规则
让小组件显示成「没有待办 🎉」，用户会以为自己真的没事可做。
现在鉴权错误优先判定，两个任务列表都失败也走错误态，都有回归用例钉住。

---

## 七、怎么验证（没有 iPhone 也能做）

`clients/ios/preview.mjs` 用 stub 顶掉 Scriptable 的 API，让**同一份脚本**
在 Node 里真跑一遍，把渲染结果打成终端文本。

```bash
node clients/ios/preview.mjs --fixtures <目录> --family medium
SK_API_KEY=sk_xxx node clients/ios/preview.mjs --live     # 打真实服务器
```

`tests/test_ios_widget.py` 的端到端用例走的就是这条路：用**进程内应用**生成
真实的接口响应当 fixture，再跑脚本断言输出。语法错误、字段名写错、
空数组没兜住，都会在这里现形，而不是等到手机上变成一块空白。

真机上仍有一件事只能靠自检：小组件进程里的 Keychain / 文件访问行为。
所以脚本里的自检报告把"缓存可读写"单列一项，跑一次就知道。

---

## 八、你需要做的

见 [`clients/ios/README.md`](../clients/ios/README.md)。摘要：

1. App Store 装 Scriptable（免费）
2. 服务端 `/settings` 创建一个**只读** API Key
3. 脚本粘进 Scriptable，填 `BASE_URL` 与密钥
4. **先在 App 里跑一次自检**，全绿了再加小组件
5. 长按桌面 → 添加小组件 → Scriptable → 中号
