/* 草稿确认页的交互层。
 *
 * 条目本身由服务端渲染（无 JS 也能看到全部识别结果），
 * 这里只负责：二选一控件的联动、删除条目、收集用户改过的内容并确认。
 *
 * 提交时把**用户改过的** items 一起发给 confirm 接口——修改与入库在一次
 * 事务里完成，不会出现"改完了但没确认"的中间态。
 */
(function () {
  "use strict";

  const { api, reportError, toast } = window.sk;

  const page = document.querySelector("[data-draft-page]");
  if (!page || page.dataset.status !== "pending") return;

  const draftId = page.dataset.draftId;
  const list = document.querySelector("[data-draft-items]");
  const form = document.querySelector("[data-draft-form]");

  /* ── 每个条目的二选一联动 ───────────────────────────────────────── */
  function syncMode(item) {
    const isDue = item.querySelector('[data-field="mode"]').value === "due";
    item.querySelector('[data-field="due_at"]').hidden = !isDue;
    item.querySelector('[data-field="priority"]').hidden = isDue;
    if (isDue) {
      const due = item.querySelector('[data-field="due_at"]');
      if (!due.value) {
        const now = new Date();
        const pad = (n) => String(n).padStart(2, "0");
        due.value = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T23:59`;
      }
    }
  }

  function syncAll() {
    list.querySelectorAll(".draft-item").forEach(syncMode);
  }

  list.addEventListener("change", (event) => {
    const mode = event.target.closest('[data-field="mode"]');
    if (mode) syncMode(mode.closest(".draft-item"));
  });

  list.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-action=remove]");
    if (!remove) return;
    remove.closest(".draft-item").remove();
    if (!list.children.length) {
      toast("至少要留一条，全部不要请点「丢弃」", "error");
    }
  });

  syncAll();

  /* ── 收集与提交 ─────────────────────────────────────────────────── */
  function collect() {
    return [...list.querySelectorAll(".draft-item")].map((item) => {
      const get = (name) => item.querySelector(`[data-field="${name}"]`);
      const mode = get("mode").value;
      const entry = {
        title: get("title").value.trim(),
        category: get("category").value,
      };
      if (mode === "due") {
        const raw = get("due_at").value;
        // datetime-local 没有时区，按浏览器本地时区转成带偏移的 ISO
        entry.due_at = raw ? new Date(raw).toISOString() : null;
        entry.priority = null;
      } else {
        entry.due_at = null;
        entry.priority = Number(get("priority").value);
      }
      return entry;
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = collect();
    if (!payload.length) {
      toast("至少要有一条事项", "error");
      return;
    }
    if (payload.some((entry) => !entry.title)) {
      toast("有事项标题为空", "error");
      return;
    }
    try {
      const result = await api("POST", `/api/ingest/${draftId}/confirm`, { items: payload });
      toast(`已入库 ${result.created_item_ids.length} 条`);
      window.location.href = "/";
    } catch (error) {
      reportError(error);
    }
  });

  document.addEventListener("click", async (event) => {
    if (!event.target.closest("[data-action=discard]")) return;
    if (!window.confirm("丢弃这条草稿？不会创建任何任务。")) return;
    try {
      await api("POST", `/api/ingest/${draftId}/discard`);
      toast("已丢弃");
      window.location.href = "/";
    } catch (error) {
      reportError(error);
    }
  });
})();
