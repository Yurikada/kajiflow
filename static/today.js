// today.html: 今日のリスト画面（遅延・滞納の表示はしない）

"use strict";

const STATUS_VIEW = {
  done: { icon: "✅", label: "完了", cls: "is-done", labelCls: "done" },
  skip: { icon: "➖", label: "スキップ", cls: "is-skip", labelCls: "" },
  pending: { icon: "⬜", label: "", cls: "", labelCls: "" },
};

function formatDateJa(dateStr) {
  const [y, m, d] = dateStr.split("-").map(Number);
  const wd = ["日", "月", "火", "水", "木", "金", "土"][new Date(y, m - 1, d).getDay()];
  return `${m}月${d}日（${wd}）`;
}

async function load() {
  try {
    const data = await api("/api/today");
    document.getElementById("heading").textContent =
      `今日のリスト ${formatDateJa(data.date)}`;

    const list = document.getElementById("list");
    const emptyNote = document.getElementById("empty-note");

    if (data.items.length === 0) {
      list.innerHTML = "";
      emptyNote.hidden = false;
      return;
    }
    emptyNote.hidden = true;

    list.innerHTML = data.items
      .map(({ task, status }) => {
        const v = STATUS_VIEW[status] || STATUS_VIEW.pending;
        return `
          <li class="today-item ${v.cls}">
            <span class="status-icon">${v.icon}</span>
            <div class="info">
              <div class="name">${escapeHtml(task.name)}</div>
              <div class="meta">${escapeHtml(task.category)}・約${task.est_minutes}分</div>
            </div>
            <span class="status-label ${v.labelCls}">${v.label}</span>
          </li>`;
      })
      .join("");
  } catch (e) {
    showToast(e.message);
  }
}

load();
refreshOnReturn(load); // PWA 復帰・日付またぎで再取得
