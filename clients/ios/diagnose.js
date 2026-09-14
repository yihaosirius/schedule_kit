/* ScheduleKit 小组件 —— 最小诊断脚本
 * ============================================================================
 *
 * 当主脚本"跑了但桌面上啥都没有"时，用这个逐步定位到底卡在哪一层。
 * 它只有两屏，不依赖主脚本的任何代码。
 *
 * ## 用法
 *
 *   在 Scriptable 里**新建一个脚本**（不要覆盖主脚本），把本文件粘进去，
 *   点 ▶ 运行。按提示走两步。
 *
 * ## 它回答三个问题
 *
 *   1. Scriptable 能不能弹出对话框？ → 不能的话是 App 层面的问题
 *   2. JavaScript 能不能发 HTTPS 请求？ → 不能的话是网络/证书层面
 *   3. 你的只读密钥能不能过鉴权？ → 不能的话是密钥/服务端层面
 *
 *   三样都过了，说明问题在渲染或小组件本身，不在数据链路上。
 */

const BASE_URL = "https://canisa1ph.duckdns.org:8443";

// 把你的只读密钥填在这里（只为这次诊断，不用改主脚本）
const API_KEY = "sk_在这里粘贴只读密钥";

async function show(title, message) {
  const alert = new Alert();
  alert.title = title;
  alert.message = message;
  alert.addAction("好");
  await alert.presentAlert();
}

async function main() {
  // ── 第 1 关：对话框能不能弹出来 ──
  const first = new Alert();
  first.title = "① Scriptable 正常";
  first.message =
    "你能看到这一屏，说明 Scriptable 装好了、脚本也能跑。\n\n" +
    "下一步测网络连通性。";
  first.addAction("测网络");
  first.addCancelAction("到此为止");
  const choice = await first.presentAlert();
  if (choice !== 0) {
    await show("已停止", "那问题在脚本内容或小组件配置，不在 Scriptable 本身。");
    return;
  }

  // ── 第 2 关：HTTPS 能不能通 ──
  let health = null;
  try {
    const req = new Request(BASE_URL + "/healthz");
    req.timeoutInterval = 10;
    health = await req.loadJSON();
    const code = req.response ? req.response.statusCode : "(无)";
    if (!health || health.status !== "ok") {
      await show("② 网络通了但响应不对", "HTTP " + code + "\n\n" + JSON.stringify(health));
      return;
    }
  } catch (err) {
    await show(
      "② 连不上服务器",
      "错误：" + (err && err.message ? err.message : String(err)) + "\n\n" +
        "服务器地址：" + BASE_URL + "\n\n" +
        "可能原因：手机网络问题、或服务器没在跑。\n" +
        "在手机浏览器里打开 " + BASE_URL + "/healthz 对照一下。"
    );
    return;
  }

  // ── 第 3 关：密钥能不能过鉴权 ──
  const keyLooksReal = /^sk_/.test(API_KEY) && API_KEY.indexOf("在这里") === -1;
  if (!keyLooksReal) {
    await show(
      "③ 还没填密钥",
      "本脚本顶部的 API_KEY 还是占位符。\n\n" +
        "服务器已经通了（版本 " + health.version + "，时区 " + health.timezone + "）。"
    );
    return;
  }

  try {
    const req = new Request(BASE_URL + "/api/tasks?view=ordered&status=open&limit=2");
    req.headers = { Authorization: "Bearer " + API_KEY };
    req.timeoutInterval = 10;
    const tasks = await req.loadJSON();
    const code = req.response ? req.response.statusCode : "(无)";
    if (code >= 400) {
      await show(
        "③ 鉴权失败",
        "HTTP " + code + "\n\n" +
          "密钥不对或已被吊销 —— 去服务端 /settings 重新创建一个**只读**密钥。"
      );
      return;
    }
    await show(
      "✓ 三关全过",
      "服务器版本 " + health.version + "\n" +
        "鉴权正常，有序表取到 " + (tasks ? tasks.length : 0) + " 条。\n\n" +
        "数据链路完全没问题。如果主脚本在桌面上还是空白，那是**渲染或小组件配置**的问题：\n" +
        "1. 先确认主脚本在 App 里跑一次能弹出「自检报告」\n" +
        "2. 长按小组件 → 编辑小组件 → Script 选中主脚本\n" +
        "3. 脚本若存在 iCloud 里，确认已经下载到本机（文件 App 里能看到内容）"
    );
  } catch (err) {
    await show(
      "③ 请求失败",
      "错误：" + (err && err.message ? err.message : String(err)) + "\n\n" +
        "服务器本身是通的（上一关过了），所以问题多半在密钥这一条链路上。"
    );
  }
}

try {
  await main();
} catch (err) {
  await show("诊断脚本自己出错了", String(err && err.message ? err.message : err));
}
