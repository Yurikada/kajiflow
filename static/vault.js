// vault.html: Vault タスク連携画面（AI/自分/未分類の3グループ表示）
// 期限切れ・滞納の色付けはしない（設計原則2）。期限は事実として表示するのみ。

"use strict";

const $ = (id) => document.getElementById(id);

const GROUPS = [
  { key: "ai", listId: "list-ai", emptyId: "empty-ai", countId: "count-ai" },
  { key: "user", listId: "list-user", emptyId: "empty-user", countId: "count-user" },
  { key: "none", listId: "list-none", emptyId: "empty-none", countId: "count-none" },
];

const CLS_LABELS = { ai: "AI", user: "自分" };

// ---------------------------------------------------------------- API

/** detail がオブジェクト（message / command）の 409 も扱う vault 用 API 呼び出し */
class VaultApiError extends Error {
  constructor(message, status, command) {
    super(message);
    this.status = status;
    this.command = command || null;
  }
}

async function vaultApi(path, options = {}) {
  const opts = { ...options };
  if (opts.body !== undefined && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new VaultApiError("サーバーに接続できません", 0, null);
  }
  if (!res.ok) {
    let message = "エラーが発生しました";
    let command = null;
    try {
      const data = await res.json();
      if (data && data.detail) {
        if (typeof data.detail === "string") {
          message = data.detail;
        } else {
          if (data.detail.message) message = data.detail.message;
          if (data.detail.command) command = data.detail.command;
        }
      }
    } catch (e) { /* JSON でなければ既定文言 */ }
    throw new VaultApiError(message, res.status, command);
  }
  return res.json();
}

// ---------------------------------------------------------------- クリップボード

/** navigator.clipboard、失敗時はテキスト選択フォールバック。成功可否を返す。 */
async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) { /* フォールバックへ */ }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, ta.value.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (e) { /* コピー不可 */ }
  document.body.removeChild(ta);
  return ok;
}

// ---------------------------------------------------------------- 操作

async function classify(uid, classification) {
  try {
    await vaultApi(`/api/vault/tasks/${encodeURIComponent(uid)}/classify`, {
      method: "PUT",
      body: { classification },
    });
    showToast(
      classification ? `「${CLS_LABELS[classification]}」に分類しました` : "分類を解除しました"
    );
    await loadTasks();
  } catch (e) {
    showToast(e.message);
  }
}

async function completeTask(task) {
  const { ok, note } = await appConfirm({
    title: `「${task.title}」を完了しますか？`,
    message: "Vault のタスク正本に完了として書き戻します。",
    showNote: true,
    confirmLabel: "完了する",
  });
  if (!ok) return;
  try {
    await vaultApi(`/api/vault/tasks/${encodeURIComponent(task.uid)}/complete`, {
      method: "POST",
      body: { note },
    });
    showToast("Vault に完了を書き戻しました");
    await loadTasks();
  } catch (e) {
    showToast(e.message);
  }
}

async function copyPrompt(task) {
  let text;
  try {
    const res = await fetch(`/api/vault/tasks/${encodeURIComponent(task.uid)}/prompt`);
    if (!res.ok) throw new Error();
    text = await res.text();
  } catch (e) {
    showToast("指示文を取得できませんでした");
    return;
  }
  const ok = await copyText(text);
  showToast(ok ? "指示文をコピーしました" : "コピーできませんでした");
}

/** ハンドオフチケット: complete API は必ず 409 で agent_handoff.py コマンドを返す */
async function copyHandoffCommand(task) {
  try {
    await vaultApi(`/api/vault/tasks/${encodeURIComponent(task.uid)}/complete`, {
      method: "POST",
      body: {},
    });
    showToast("ハンドオフの完了は agent_handoff.py で行ってください");
  } catch (e) {
    if (e.status === 409 && e.command) {
      const ok = await copyText(e.command);
      showToast(ok ? "完了コマンドをコピーしました" : "コピーできませんでした");
    } else {
      showToast(e.message);
    }
  }
}

// ---------------------------------------------------------------- 描画

function groupOf(task) {
  return task.classification === "ai" || task.classification === "user"
    ? task.classification
    : "none";
}

function renderTask(task) {
  const el = document.createElement("div");
  el.className = "vault-item";

  const chips = [];
  if (task.project) chips.push(`<span class="chip">📁 ${escapeHtml(task.project)}</span>`);
  if (task.due) chips.push(`<span class="chip">期限: ${escapeHtml(task.due)}</span>`);
  if (task.is_handoff) {
    chips.push('<span class="chip badge-handoff">🤝 ハンドオフ</span>');
    if (task.handoff_owner) {
      chips.push(`<span class="chip badge-handoff">担当: ${escapeHtml(task.handoff_owner)}</span>`);
    }
    if (task.handoff_status) {
      chips.push(`<span class="chip badge-handoff">状態: ${escapeHtml(task.handoff_status)}</span>`);
    }
  }

  el.innerHTML = `
    <div class="v-title">${escapeHtml(task.title)}</div>
    ${task.purpose ? `<div class="v-purpose">${escapeHtml(task.purpose)}</div>` : ""}
    ${chips.length ? `<div class="v-chips">${chips.join("")}</div>` : ""}
    <div class="v-actions"></div>
  `;
  const actions = el.querySelector(".v-actions");

  // 未分類 + 提案あり → 提案チップ（タップで採用）
  if (!task.classification && task.suggested) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "suggest-chip";
    chip.textContent = `提案: ${CLS_LABELS[task.suggested] || task.suggested}`;
    chip.addEventListener("click", () => classify(task.uid, task.suggested));
    actions.appendChild(chip);
  }

  // 分類切替（AI / 自分 / 解除）
  const seg = document.createElement("div");
  seg.className = "seg";
  for (const [value, label] of [["ai", "AI"], ["user", "自分"], [null, "解除"]]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    if (value !== null && task.classification === value) btn.classList.add("active");
    btn.disabled = task.classification === value || (value === null && !task.classification);
    btn.addEventListener("click", () => classify(task.uid, value));
    seg.appendChild(btn);
  }
  actions.appendChild(seg);

  // AI グループ → 指示文コピー
  if (task.classification === "ai") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "icon-btn";
    btn.textContent = "📋 指示文コピー";
    btn.addEventListener("click", () => copyPrompt(task));
    actions.appendChild(btn);
  }

  // 完了 / ハンドオフはコマンドコピー
  const doneBtn = document.createElement("button");
  doneBtn.type = "button";
  if (task.is_handoff) {
    doneBtn.className = "icon-btn";
    doneBtn.textContent = "💻 完了コマンドをコピー";
    doneBtn.addEventListener("click", () => copyHandoffCommand(task));
  } else {
    doneBtn.className = "icon-btn primary";
    doneBtn.textContent = "✅ 完了";
    doneBtn.addEventListener("click", () => completeTask(task));
  }
  actions.appendChild(doneBtn);

  return el;
}

async function loadTasks() {
  const tasks = await api("/api/vault/tasks");
  const grouped = { ai: [], user: [], none: [] };
  for (const t of tasks) grouped[groupOf(t)].push(t);

  for (const g of GROUPS) {
    const list = $(g.listId);
    list.innerHTML = "";
    const items = grouped[g.key];
    $(g.emptyId).hidden = items.length !== 0;
    $(g.countId).textContent = items.length ? ` (${items.length})` : "";
    for (const t of items) list.appendChild(renderTask(t));
  }
}

// ---------------------------------------------------------------- 同期

/** 同期を実行。失敗してもキャッシュ済み DB 内容の表示は続ける。成功可否を返す。 */
async function syncVault() {
  try {
    await vaultApi("/api/vault/sync", { method: "POST" });
    return true;
  } catch (e) {
    showToast(e.message);
    return false;
  }
}

$("btn-sync").addEventListener("click", async () => {
  const btn = $("btn-sync");
  btn.disabled = true;
  try {
    if (await syncVault()) showToast("同期しました");
    await loadTasks();
  } catch (e) {
    showToast(e.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- init

(async function init() {
  await syncVault(); // 画面表示時に自動同期（失敗時はトースト表示のみ）
  try {
    await loadTasks();
  } catch (e) {
    showToast(e.message);
  }
})();
