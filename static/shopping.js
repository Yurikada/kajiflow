// shopping.html: 購買記録・買い物リスト画面（SPEC v5）
// 提案は事実の提示のみ。滞納・警告の色付けはしない（設計原則2）。

"use strict";

const $ = (id) => document.getElementById(id);

const STATUS_LABELS = {
  pending: "解析待ち",
  parsed: "読み取り済み",
  failed: "読めませんでした",
};

// ---------------------------------------------------------------- アップロード

async function uploadReceipt(file) {
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    showToast("画像が大きすぎます（10MBまで）");
    return;
  }
  try {
    const res = await fetch("/api/receipts/upload", {
      method: "POST",
      headers: { "Content-Type": file.type || "image/jpeg" },
      body: file,
    });
    if (!res.ok) {
      let detail = "アップロードできませんでした";
      try {
        const data = await res.json();
        if (data && typeof data.detail === "string") detail = data.detail;
      } catch (e) { /* JSON でなければ既定文言 */ }
      throw new Error(detail);
    }
    const body = await res.json();
    showToast(
      body.created
        ? "レシートを預かりました。次のエージェント起動時に読み取ります"
        : "同じレシートが登録済みです"
    );
    await loadAll();
  } catch (e) {
    showToast(e.message || "アップロードできませんでした");
  }
}

$("receipt-file").addEventListener("change", async (e) => {
  const file = e.target.files && e.target.files[0];
  await uploadReceipt(file);
  e.target.value = ""; // 同じファイルの再選択でも change を発火させる
});

// ---------------------------------------------------------------- レシート一覧

function renderReceipt(r) {
  const el = document.createElement("div");
  el.className = "vault-item";
  const chips = [`<span class="chip">${escapeHtml(STATUS_LABELS[r.status] || r.status)}</span>`];
  if (r.store) chips.push(`<span class="chip">🏪 ${escapeHtml(r.store)}</span>`);
  if (r.total_jpy != null) chips.push(`<span class="chip">${Math.round(r.total_jpy)}円</span>`);
  const when = (r.purchased_at || r.uploaded_at || "").slice(0, 10);
  if (when) chips.push(`<span class="chip">${escapeHtml(when)}</span>`);
  el.innerHTML = `
    <div class="receipt-row">
      <img class="receipt-thumb" src="/api/receipts/${r.id}/image" alt="" loading="lazy">
      <div class="receipt-main">
        <div class="v-chips">${chips.join("")}</div>
        ${r.status === "failed" && r.note ? `<div class="v-purpose">${escapeHtml(r.note)}</div>` : ""}
      </div>
      <div class="receipt-actions"></div>
    </div>
  `;
  const actions = el.querySelector(".receipt-actions");
  const del = document.createElement("button");
  del.type = "button";
  del.className = "icon-btn";
  del.textContent = "🗑";
  del.addEventListener("click", async () => {
    const { ok } = await appConfirm({
      title: "このレシートを削除しますか？",
      message: "画像と読み取り済みの明細も削除されます。",
      confirmLabel: "削除する",
      danger: true,
    });
    if (!ok) return;
    try {
      await api(`/api/receipts/${r.id}`, { method: "DELETE" });
      showToast("削除しました");
      await loadAll();
    } catch (e) {
      showToast(e.message);
    }
  });
  actions.appendChild(del);
  return el;
}

async function loadReceipts() {
  const receipts = await api("/api/receipts");
  // 解析待ち・失敗のみ表示（読み取り済みは「直近の購入」に反映されるため畳む）
  const active = receipts.filter((r) => r.status !== "parsed");
  const list = $("list-receipts");
  list.innerHTML = "";
  $("empty-receipts").hidden = active.length !== 0;
  $("count-receipts").textContent = active.length ? ` (${active.length})` : "";
  for (const r of active) list.appendChild(renderReceipt(r));
}

// ---------------------------------------------------------------- 買い物リスト

function renderSuggestion(s) {
  const el = document.createElement("div");
  el.className = "vault-item";
  const chips = [
    `<span class="chip">${escapeHtml(s.item.category)}</span>`,
    `<span class="chip">前回 ${escapeHtml((s.last_purchased_at || "").slice(0, 10))}</span>`,
    `<span class="chip">約${s.interval_days}日ごと</span>`,
  ];
  if (s.thin) chips.push('<span class="chip">目安薄い</span>');
  el.innerHTML = `
    <div class="v-title">${escapeHtml(s.item.name)}</div>
    <div class="v-chips">${chips.join("")}</div>
  `;
  return el;
}

async function loadSuggestions() {
  const body = await api("/api/shopping/list");
  const list = $("list-suggest");
  list.innerHTML = "";
  const items = body.suggestions || [];
  $("empty-suggest").hidden = items.length !== 0;
  $("count-suggest").textContent = items.length ? ` (${items.length})` : "";
  for (const s of items) list.appendChild(renderSuggestion(s));
}

// ---------------------------------------------------------------- 直近の購入

function renderPurchase(p) {
  const el = document.createElement("div");
  el.className = "vault-item";
  const chips = [
    `<span class="chip">${escapeHtml(p.category)}</span>`,
    `<span class="chip">${Math.round(p.amount_jpy)}円</span>`,
    `<span class="chip">${escapeHtml((p.purchased_at || "").slice(0, 10))}</span>`,
  ];
  if (p.store) chips.push(`<span class="chip">🏪 ${escapeHtml(p.store)}</span>`);
  el.innerHTML = `
    <div class="receipt-row">
      <div class="receipt-main">
        <div class="v-title">${escapeHtml(p.item_name)}</div>
        <div class="v-chips">${chips.join("")}</div>
      </div>
      <div class="receipt-actions"></div>
    </div>
  `;
  const del = document.createElement("button");
  del.type = "button";
  del.className = "icon-btn";
  del.textContent = "🗑";
  del.addEventListener("click", async () => {
    const { ok } = await appConfirm({
      title: `「${p.item_name}」の購入記録を削除しますか？`,
      confirmLabel: "削除する",
      danger: true,
    });
    if (!ok) return;
    try {
      await api(`/api/purchases/${p.id}`, { method: "DELETE" });
      showToast("削除しました");
      await loadAll();
    } catch (e) {
      showToast(e.message);
    }
  });
  el.querySelector(".receipt-actions").appendChild(del);
  return el;
}

async function loadPurchases() {
  const purchases = await api("/api/purchases?months=3");
  const list = $("list-purchases");
  list.innerHTML = "";
  $("empty-purchases").hidden = purchases.length !== 0;
  for (const p of purchases.slice(0, 50)) list.appendChild(renderPurchase(p));
}

$("purchase-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("p-name").value.trim();
  const amount = Number($("p-amount").value);
  if (!name || !Number.isFinite(amount)) return;
  try {
    await api("/api/purchases", {
      method: "POST",
      body: { item_name: name, amount_jpy: amount, category: $("p-category").value },
    });
    $("p-name").value = "";
    $("p-amount").value = "";
    showToast("記録しました");
    await loadAll();
  } catch (err) {
    showToast(err.message);
  }
});

// ---------------------------------------------------------------- init

async function loadAll() {
  try {
    await Promise.all([loadReceipts(), loadSuggestions(), loadPurchases()]);
  } catch (e) {
    showToast(e.message);
  }
}

loadAll();
refreshOnReturn(loadAll);
