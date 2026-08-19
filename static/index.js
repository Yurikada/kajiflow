// index.html: 「今の1件」画面

"use strict";

const els = {
  progress: document.getElementById("progress"),
  card: document.getElementById("card"),
  taskName: document.getElementById("task-name"),
  taskCategory: document.getElementById("task-category"),
  taskMinutes: document.getElementById("task-minutes"),
  emptyCard: document.getElementById("empty-card"),
  emptyEmoji: document.getElementById("empty-emoji"),
  emptyText: document.getElementById("empty-text"),
  actions: document.getElementById("actions"),
  btnDone: document.getElementById("btn-done"),
  btnSkip: document.getElementById("btn-skip"),
};

let currentTaskId = null;
let busy = false;

function render(data) {
  const { task, done_count, total_count } = data;

  // 進捗は小さく「2/4」程度のみ。滞納・遅延の表示は一切しない。
  els.progress.textContent = total_count > 0 ? `${done_count}/${total_count}` : "";

  if (task) {
    currentTaskId = task.id;
    els.taskName.textContent = task.name;
    els.taskCategory.textContent = task.category;
    els.taskMinutes.textContent = `約${task.est_minutes}分`;
    els.card.hidden = false;
    els.emptyCard.hidden = true;
    els.actions.hidden = false;
  } else {
    currentTaskId = null;
    els.card.hidden = true;
    els.actions.hidden = true;
    if (total_count === 0) {
      els.emptyEmoji.textContent = "🍵";
      els.emptyText.textContent = "今日はやることなし";
    } else {
      els.emptyEmoji.textContent = "🎉";
      els.emptyText.textContent = "今日の家事は終わりです";
    }
    els.emptyCard.hidden = false;
  }
}

async function load() {
  try {
    render(await api("/api/next"));
  } catch (e) {
    showToast(e.message);
  }
}

async function act(action) {
  if (busy || currentTaskId == null) return;
  busy = true;
  els.btnDone.disabled = true;
  els.btnSkip.disabled = true;
  try {
    render(await api(`/api/tasks/${currentTaskId}/${action}`, { method: "POST" }));
  } catch (e) {
    showToast(e.message);
  } finally {
    busy = false;
    els.btnDone.disabled = false;
    els.btnSkip.disabled = false;
  }
}

els.btnDone.addEventListener("click", () => act("complete"));
els.btnSkip.addEventListener("click", () => act("skip"));

load();
refreshOnReturn(load); // PWA 復帰・日付またぎで再取得
