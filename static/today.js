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

async function completeFromList(task) {
  const { ok } = await appConfirm({
    title: `「${task.name}」を完了にしますか？`,
    confirmLabel: "完了にする",
  });
  if (!ok) return;
  try {
    await api(`/api/tasks/${task.id}/complete`, { method: "POST" });
    showToast("完了にしました（タップで取り消せます）");
    await load();
  } catch (e) {
    showToast(e.message);
  }
}

async function uncompleteFromList(task) {
  const { ok } = await appConfirm({
    title: `「${task.name}」の記録を取り消しますか？`,
    message: "未完了に戻ります。Google Tasks 側も戻します。",
    confirmLabel: "取り消す",
  });
  if (!ok) return;
  try {
    const res = await api(`/api/tasks/${task.id}/uncomplete`, { method: "POST" });
    showToast(res.restored ? "未完了に戻しました" : "今日の記録はありません");
    await load();
  } catch (e) {
    showToast(e.message);
  }
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

    list.innerHTML = "";
    for (const { task, status } of data.items) {
      const v = STATUS_VIEW[status] || STATUS_VIEW.pending;
      const li = document.createElement("li");
      li.className = `today-item ${v.cls}`;
      li.innerHTML = `
        <span class="status-icon">${v.icon}</span>
        <div class="info">
          <div class="name">${escapeHtml(task.name)}</div>
          <div class="meta">${escapeHtml(task.category)}・約${task.est_minutes}分</div>
        </div>
        <span class="status-label ${v.labelCls}">${v.label}</span>`;
      if (status === "pending") {
        // pending 行はタップで完了（確認ダイアログ付き）
        li.classList.add("is-tappable");
        li.setAttribute("role", "button");
        li.setAttribute("aria-label", `${task.name} を完了にする`);
        li.addEventListener("click", () => completeFromList(task));
      } else {
        // done / skip 行はタップで取り消し（誤タップの救済）
        li.classList.add("is-tappable");
        li.setAttribute("role", "button");
        li.setAttribute("aria-label", `${task.name} の記録を取り消す`);
        li.addEventListener("click", () => uncompleteFromList(task));
      }
      list.appendChild(li);
    }
  } catch (e) {
    showToast(e.message);
  }
}

load();
refreshOnReturn(load); // PWA 復帰・日付またぎで再取得
