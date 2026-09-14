/* 草稿箱：批量永久删除。
 *
 * 列表本身由服务端渲染（无 JS 也能看、能翻页、能筛选），
 * 这里只负责一件事：把勾选的草稿一次性删掉。
 *
 * 删除是不可恢复的，所以：
 *  1. 提交前必须确认，确认文案里写明"任务不受影响、图片不会被删"——
 *     这两点是用户最容易担心的，也是实际行为；
 *  2. 复选框没勾任何一条时按钮禁用，避免发出一个必然 422 的请求。
 */
(function () {
  "use strict";

  const { api, reportError, toast } = window.sk;
  const page = document.querySelector("[data-drafts-page]");
  if (!page) return;

  const form = document.querySelector("[data-drafts-form]");
  if (!form) return;

  const toggle = form.querySelector("[data-drafts-toggle]");
  const submit = form.querySelector('button[type="submit"]');
  const status = document.querySelector("[data-drafts-status]");

  const boxes = () => Array.from(form.querySelectorAll('input[name="ids"]'));
  const checked = () => boxes().filter((box) => box.checked);

  function sync() {
    const count = checked().length;
    submit.disabled = count === 0;
    submit.textContent = count ? `删除选中（${count}）` : "删除选中";
    status.textContent = count
      ? "永久删除，不可恢复"
      : "勾选左侧复选框后可以批量删除";
  }

  toggle.addEventListener("change", () => {
    boxes().forEach((box) => { box.checked = toggle.checked; });
    sync();
  });

  form.addEventListener("change", (event) => {
    if (event.target.name === "ids") sync();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ids = checked().map((box) => Number(box.value));
    if (!ids.length) return;

    const ok = window.confirm(
      `永久删除 ${ids.length} 条草稿？\n\n` +
      "此操作不可恢复。\n" +
      "已经入库的任务不受影响；图片文件也不会被删掉（按保留策略单独清理）。"
    );
    if (!ok) return;

    submit.disabled = true;
    try {
      const result = await api("POST", "/api/ingest/purge", { ids });
      const extra = result.missing.length ? `，另有 ${result.missing.length} 条已不存在` : "";
      toast(`已删除 ${result.deleted} 条${extra}`);
      window.location.reload();
    } catch (error) {
      reportError(error);
      sync();
    }
  });

  sync();
})();
