/* 首页交互：添加、勾选完成、删除。
 *
 * 采用"局部刷新 + 重新排序"的策略：任何写操作成功后重新拉取两个视图，
 * 避免在客户端复刻一遍排序规则（有序表按时间、无序表按 Ⅰ→Ⅴ），
 * 那样迟早会和服务端不一致。
 *
 * ⚠️ renderRow() 是 app/templates/index.html 里 task_row 宏的**镜像**。
 * 两边必须一起改。踩过的坑：备注标记当初只加在了服务端那一份上，于是
 * 任何一次勾选完成（触发 refresh 重渲染）之后，备注就不见了。
 */
(function () {
  "use strict";

  const { api, reportError } = window.sk;

  const form = document.querySelector("[data-add-form]");
  const modeSelect = document.querySelector("[data-mode]");
  const dueInput = document.querySelector("[data-due]");
  const prioritySelect = document.querySelector("[data-priority]");

  /* ── 添加表单：deadline 与优先级二选一 ───────────────────────────── */
  function syncMode() {
    const isDue = modeSelect.value === "due";
    dueInput.hidden = !isDue;
    dueInput.required = isDue;
    prioritySelect.hidden = isDue;

    const hint = document.querySelector("[data-hint]");
    if (isDue) {
      if (!dueInput.value) dueInput.value = defaultDueValue();
      hint.textContent = "二选一：给了截止时间就不用选优先级";
    } else {
      hint.textContent = "二选一：不设截止时间时才按优先级排";
    }
  }

  /* datetime-local 需要本地时间字符串，取「今天 23:59」作为默认值 */
  function defaultDueValue() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T23:59`;
  }

  if (modeSelect) {
    modeSelect.addEventListener("change", syncMode);
    syncMode();
  }

  /* ── 渲染 ────────────────────────────────────────────────────────── */
  const CATEGORY_LABELS = { homework: "作业", practice: "练习", exam: "考试", appointment: "要约", other: "其他" };
  const PRIORITY_LABELS = { 1: "Ⅰ", 2: "Ⅱ", 3: "Ⅲ", 4: "Ⅳ", 5: "Ⅴ" };
  const SOURCE_LABELS = { web: "网页", shortcut: "快捷指令", llm: "智能录入", api: "接口" };

  function relativeTime(iso) {
    const diff = (new Date(iso).getTime() - Date.now()) / 1000;
    const abs = Math.abs(diff);
    const suffix = diff < 0 ? "前" : "后";
    if (abs < 3600) return `${Math.max(1, Math.round(abs / 60))} 分钟${suffix}`;
    if (abs < 86400) return `${Math.round(abs / 3600)} 小时${suffix}`;
    return `${Math.round(abs / 86400)} 天${suffix}`;
  }

  function dueState(iso) {
    const diff = (new Date(iso).getTime() - Date.now()) / 1000;
    if (diff < 0) return "is-overdue";
    if (diff < 86400) return "is-soon";
    return "";
  }

  function localLabel(iso) {
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function facts(item) {
    const parts = [`来源 ${SOURCE_LABELS[item.source] || item.source}`];
    parts.push(`创建 ${localLabel(item.created_at)}`);
    if (item.updated_at && item.updated_at !== item.created_at) {
      parts.push(`更新 ${localLabel(item.updated_at)}`);
    }
    if (item.completed_at) parts.push(`完成 ${localLabel(item.completed_at)}`);
    return parts.join(" · ");
  }

  /* 备注与详情。结构与 index.html 的 task_notes 宏一一对应，改一个要改两个。
     用 <details> 而不是自己写开合：无 JS 也能展开，触屏和键盘都能用。

     文本一律走 textContent —— 备注可能来自图片识别，是外部内容。 */
  function notesBlock(item) {
    const details = document.createElement("details");
    details.className = "task__notes";

    const summary = document.createElement("summary");
    const peek = document.createElement("span");
    peek.className = "task__notes-peek";
    peek.textContent = item.notes;
    const open = document.createElement("span");
    open.className = "task__notes-open";
    open.textContent = "备注 · 收起";
    summary.append(peek, open);

    const full = document.createElement("p");
    full.className = "task__notes-full";
    full.textContent = item.notes;

    const meta = document.createElement("p");
    meta.className = "task__facts";
    meta.textContent = facts(item);

    details.append(summary, full, meta);
    return details;
  }

  function renderRow(item) {
    const li = document.createElement("li");
    li.className = "task" + (item.status === "done" ? " is-done" : "");
    li.dataset.id = item.id;
    li.dataset.view = item.due_at ? "ordered" : "unordered";

    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "task__check";
    check.dataset.action = "toggle";
    check.checked = item.status === "done";
    check.setAttribute("aria-label", "标记完成");

    const body = document.createElement("div");
    body.className = "task__body";

    const title = document.createElement("div");
    title.className = "task__title";
    title.textContent = item.title;

    const meta = document.createElement("div");
    meta.className = "task__meta";

    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = CATEGORY_LABELS[item.category] || item.category;
    meta.appendChild(chip);

    if (item.due_at) {
      const due = document.createElement("span");
      due.className = "due " + dueState(item.due_at);
      due.textContent = `${localLabel(item.due_at)} · ${relativeTime(item.due_at)}`;
      meta.appendChild(due);
    } else {
      const badge = document.createElement("span");
      badge.className = "badge-priority";
      badge.dataset.p = item.priority;
      badge.textContent = PRIORITY_LABELS[item.priority] || item.priority;
      meta.appendChild(badge);
    }

    body.append(title, meta);
    if (item.notes) body.appendChild(notesBlock(item));

    const actions = document.createElement("div");
    actions.className = "task__actions";
    const del = document.createElement("button");
    del.type = "button";
    del.dataset.action = "delete";
    del.title = "删除";
    del.textContent = "✕";
    actions.appendChild(del);

    li.append(check, body, actions);
    return li;
  }

  async function refresh() {
    const [ordered, unordered] = await Promise.all([
      api("GET", "/api/tasks?view=ordered&status=open"),
      api("GET", "/api/tasks?view=unordered&status=open"),
    ]);
    for (const [view, items] of [["ordered", ordered], ["unordered", unordered]]) {
      const list = document.querySelector(`[data-list="${view}"]`);
      list.replaceChildren(...items.map(renderRow));
      document.querySelector(`[data-count-for="${view}"]`).textContent = items.length;
      document.querySelector(`[data-empty-for="${view}"]`).hidden = items.length > 0;
    }
  }

  /* ── 事件 ────────────────────────────────────────────────────────── */
  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const title = (data.get("title") || "").toString().trim();
      if (!title) return;

      const payload = { title, category: data.get("category"), source: "web" };
      if (data.get("mode") === "due") {
        const raw = (data.get("due_at") || "").toString();
        if (!raw) return;
        // datetime-local 不带时区，按浏览器本地时区转成 ISO
        payload.due_at = new Date(raw).toISOString();
      } else {
        payload.priority = Number(data.get("priority") || 3);
      }

      try {
        await api("POST", "/api/tasks", payload);
        form.reset();
        syncMode();
        await refresh();
        form.querySelector("[name=title]").focus();
      } catch (error) {
        reportError(error);
      }
    });
  }

  document.addEventListener("change", async (event) => {
    const checkbox = event.target.closest("[data-action=toggle]");
    if (!checkbox) return;
    const li = checkbox.closest(".task");
    try {
      await api("PATCH", `/api/tasks/${li.dataset.id}`, {
        status: checkbox.checked ? "done" : "open",
      });
      await refresh();
    } catch (error) {
      checkbox.checked = !checkbox.checked;
      reportError(error);
    }
  });

  document.addEventListener("click", async (event) => {
    const removeButton = event.target.closest("[data-action=delete]");
    if (removeButton) {
      const li = removeButton.closest(".task");
      if (!window.confirm("删除这条任务？")) return;
      try {
        await api("DELETE", `/api/tasks/${li.dataset.id}`);
        await refresh();
      } catch (error) {
        reportError(error);
      }
      return;
    }

    if (event.target.closest("[data-action=focus-add]")) {
      const input = form && form.querySelector("[name=title]");
      if (input) {
        input.scrollIntoView({ behavior: "smooth", block: "center" });
        input.focus();
      }
    }
    /* 退出登录由 app.js 统一处理（顶栏在每个页面都有） */
  });

  /* 移动端把无序表折叠起来：它权重更小，不该占首屏。
     只在载入时按视口定一次初值，之后交给用户自己开合。 */
  const collapsible = document.querySelector("[data-collapsible]");
  if (collapsible) {
    collapsible.open = window.matchMedia("(min-width: 900px)").matches;
  }
})();
