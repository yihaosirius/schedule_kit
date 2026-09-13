/* 课表页：保存文本并回显解析结果。
 *
 * 服务端对整份课表做"要么全成功、要么全拒绝"，所以这里要把失败的行
 * 清楚地列出来——否则用户只知道"保存失败"，不知道该改哪一行。
 */
(function () {
  "use strict";

  const { api, reportError, toast } = window.sk;

  const form = document.querySelector("[data-courses-form]");
  if (!form) return;

  const status = document.querySelector("[data-courses-status]");
  const errorBox = document.querySelector("[data-courses-errors]");

  function clearErrors() {
    errorBox.hidden = true;
    errorBox.replaceChildren();
  }

  function showErrors(payload) {
    const detail = payload && payload.detail ? payload.detail : payload;
    errorBox.replaceChildren();
    errorBox.hidden = false;

    const title = document.createElement("p");
    title.className = "courses-errors__title";
    title.textContent = (detail && detail.message) || "保存失败";
    errorBox.appendChild(title);

    const list = document.createElement("ul");
    for (const item of (detail && detail.errors) || []) {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = `第 ${item.line} 行`;
      li.append(code, document.createTextNode(` ${item.text} —— ${item.reason}`));
      list.appendChild(li);
    }
    errorBox.appendChild(list);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearErrors();
    status.textContent = "保存中…";

    const text = form.querySelector("textarea[name=text]").value;
    try {
      const result = await api("PUT", "/api/courses", { text });
      status.textContent = "";
      toast(`已保存 ${result.saved_courses} 门课 / ${result.saved_sessions} 个时段`);
      // 重新载入以刷新下方表格与周次显示
      window.location.reload();
    } catch (error) {
      status.textContent = "";
      if (error.status === 422) {
        // 解析失败时错误体里带逐行原因，单独渲染比 toast 有用得多
        try {
          showErrors(JSON.parse(error.message));
        } catch (_) {
          showErrors({ message: error.message });
        }
      } else {
        reportError(error);
      }
    }
  });
})();
