// KajiFlow 共通ユーティリティ（全画面で読み込む）

"use strict";

/** API を叩いて JSON を返す。エラー時は日本語メッセージ付きで throw。 */
async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.body !== undefined && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error("サーバーに接続できません");
  }
  if (!res.ok) {
    let detail = "エラーが発生しました";
    try {
      const data = await res.json();
      if (data && data.detail) {
        detail = typeof data.detail === "string" ? data.detail : detail;
      }
    } catch (e) { /* JSON でなければ既定文言 */ }
    throw new Error(detail);
  }
  return res.json();
}

/** トースト表示（画面下部に短時間） */
let _toastTimer = null;
function showToast(message) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 2400);
}

/** HTML エスケープ */
function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * アプリ内確認ダイアログ。window.confirm / prompt の代替。
 * ネイティブダイアログは Android のインストール済み PWA（standalone）で
 * 無効化され即 false を返すため、必ずこちらを使う。
 * 戻り値: {ok: boolean, note: string|null}（showNote=false のとき note は null）
 */
function appConfirm({ title, message = "", showNote = false, confirmLabel = "確定", danger = false }) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal-sheet" role="dialog" aria-modal="true">
        <p class="modal-title">${escapeHtml(title)}</p>
        ${message ? `<p class="modal-message">${escapeHtml(message)}</p>` : ""}
        ${showNote ? '<input type="text" class="modal-note" placeholder="完了メモ（任意）">' : ""}
        <div class="modal-actions">
          <button type="button" class="modal-cancel">キャンセル</button>
          <button type="button" class="modal-ok${danger ? " danger" : ""}">${escapeHtml(confirmLabel)}</button>
        </div>
      </div>`;
    const close = (result) => {
      overlay.remove();
      resolve(result);
    };
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close({ ok: false, note: null });
    });
    overlay.querySelector(".modal-cancel").addEventListener("click", () => close({ ok: false, note: null }));
    overlay.querySelector(".modal-ok").addEventListener("click", () => {
      const noteEl = overlay.querySelector(".modal-note");
      close({ ok: true, note: noteEl ? noteEl.value.trim() || null : null });
    });
    document.body.appendChild(overlay);
  });
}

/**
 * 画面復帰時のデータ再取得を登録する。
 * PWA がバックグラウンドから復帰しても JS は再実行されないため、
 * visibilitychange / focus / bfcache 復元（pageshow persisted）で reload を呼ぶ。
 * 日付またぎもサーバ側 API が当日基準で応答するため再取得だけで正しくなる。
 * 連続発火（フォーカス+visible 等）は 5 秒のガードでまとめる。
 */
function refreshOnReturn(reload) {
  let last = Date.now(); // 初回ロード直後の重複再取得を避ける
  const maybe = () => {
    if (Date.now() - last < 5000) return;
    last = Date.now();
    reload();
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") maybe();
  });
  window.addEventListener("focus", maybe);
  window.addEventListener("pageshow", (e) => {
    if (e.persisted) maybe();
  });
}

const WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"];

/** '0,3' 形式 → '月・木' 表示 */
function weekdaysToLabel(weekdays) {
  if (!weekdays) return "";
  return String(weekdays)
    .split(",")
    .map((s) => WEEKDAY_LABELS[parseInt(s.trim(), 10)])
    .filter(Boolean)
    .join("・");
}

/** タスクのスケジュール説明文 */
function scheduleLabel(task) {
  if (task.schedule_type === "calendar") {
    return "カレンダー連動";
  }
  if (task.schedule_type === "weekly") {
    return `毎週 ${weekdaysToLabel(task.weekdays)}`;
  }
  const d = Number(task.interval_days);
  if (d === 1) return "毎日";
  return `${Number.isInteger(d) ? d : d.toFixed(1)}日ごと`;
}

// Service Worker 登録（PWA）
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* 登録失敗しても通常動作に影響なし */
    });
  });
}
