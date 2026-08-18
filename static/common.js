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
