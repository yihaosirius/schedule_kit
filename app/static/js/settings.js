/* 控制台：LLM 配置与 API 密钥。
 *
 * 两条安全约定在前端也要守住：
 *  1. API Key 明文只在创建响应里出现一次，刷新后不可能再拿到；
 *  2. 保存 LLM 配置时，如果用户没动密钥输入框，就**不发送** api_key 字段，
 *     让服务端保持原值——避免把密钥在浏览器与服务器之间来回搬运。
 */
(function () {
  "use strict";

  const { api, reportError, toast } = window.sk;
  const page = document.querySelector("[data-settings-page]");
  if (!page) return;

  /* ── LLM 配置 ───────────────────────────────────────────────────── */
  const llmForm = document.querySelector("[data-llm-form]");
  const llmStatus = document.querySelector("[data-llm-status]");
  const keyInput = llmForm.querySelector("[name=api_key]");

  document.querySelector("[data-action=clear-key]").addEventListener("click", () => {
    if (!window.confirm("清空已保存的 API Key？录入功能将不可用，直到重新填入。")) return;
    keyInput.value = "";
    keyInput.dataset.cleared = "1";
    keyInput.placeholder = "将被清空（保存后生效）";
    toast("保存后密钥会被清空");
  });

  llmForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(llmForm);
    const payload = {
      provider: data.get("provider"),
      base_url: data.get("base_url") || "",
      model: (data.get("model") || "").trim(),
      temperature: Number(data.get("temperature") || 0),
      timeout_seconds: Number(data.get("timeout_seconds") || 60),
      max_tokens: Number(data.get("max_tokens") || 1024),
      max_image_bytes: Math.max(1, Number(data.get("max_image_mb") || 8)) * 1048576,
      system_prompt: data.get("system_prompt") || "",
    };

    // 只有用户真的动过密钥框才提交该字段
    const typed = keyInput.value.trim();
    if (keyInput.dataset.cleared === "1") {
      payload.api_key = "";
    } else if (typed) {
      payload.api_key = typed;
    }

    llmStatus.textContent = "保存中…";
    try {
      const result = await api("PUT", "/api/settings", payload);
      llmStatus.textContent = "";
      keyInput.value = "";
      delete keyInput.dataset.cleared;
      keyInput.placeholder = result.api_key_set ? "已设置（留空则保持不变）" : "尚未设置";
      updateLlmState(result);
      toast(result.ready ? "已保存，立即生效" : "已保存，但配置尚不完整");
    } catch (error) {
      llmStatus.textContent = "";
      reportError(error);
    }
  });

  function updateLlmState(result) {
    const badge = document.querySelector("[data-llm-state]");
    badge.textContent = result.ready ? "已就绪" : "未就绪";
    let error = document.querySelector("[data-llm-error]");
    if (result.error) {
      if (!error) {
        error = document.createElement("p");
        error.className = "callout callout--warn";
        error.dataset.llmError = "";
        llmForm.before(error);
      }
      error.textContent = result.error;
    } else if (error) {
      error.remove();
    }
  }

  /* ── API 密钥 ───────────────────────────────────────────────────── */
  const keyForm = document.querySelector("[data-key-form]");
  const reveal = document.querySelector("[data-key-reveal]");
  const plaintextNode = document.querySelector("[data-key-plaintext]");

  keyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(keyForm);
    try {
      const result = await api("POST", "/api/keys", {
        name: (data.get("name") || "").trim(),
        read_only: data.get("read_only") === "on",
      });
      plaintextNode.textContent = result.key;
      reveal.hidden = false;
      reveal.scrollIntoView({ behavior: "smooth", block: "center" });
      keyForm.reset();
      toast("密钥已创建，请立即复制");
    } catch (error) {
      reportError(error);
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action=revoke-key]");
    if (!button) return;
    const row = button.closest("[data-key-id]");
    const name = row.querySelector("td").textContent.trim();
    if (!window.confirm(`吊销密钥「${name}」？使用它的客户端会立即失效。`)) return;
    try {
      await api("DELETE", `/api/keys/${row.dataset.keyId}`);
      row.remove();
      toast("已吊销");
    } catch (error) {
      reportError(error);
    }
  });
})();
