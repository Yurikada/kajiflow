// manage.html: タスク管理・テンプレ追加・設定

"use strict";

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- タスク一覧

async function loadTasks() {
  const tasks = await api("/api/tasks");
  const listEl = $("task-list");
  const emptyEl = $("task-empty");

  listEl.querySelectorAll(".task-row").forEach((el) => el.remove());
  emptyEl.hidden = tasks.length !== 0;

  for (const task of tasks) {
    const row = document.createElement("div");
    row.className = "task-row" + (task.enabled ? "" : " is-disabled");
    row.innerHTML = `
      <label class="toggle">
        <input type="checkbox" ${task.enabled ? "checked" : ""} aria-label="有効/無効">
        <span class="knob"></span>
      </label>
      <div class="info">
        <div class="name">${escapeHtml(task.name)}</div>
        <div class="meta">${escapeHtml(task.category)}・${escapeHtml(scheduleLabel(task))}・約${task.est_minutes}分</div>
      </div>
      <button type="button" class="icon-btn" data-act="edit">編集</button>
      <button type="button" class="icon-btn danger" data-act="delete">削除</button>
    `;
    row.querySelector(".toggle input").addEventListener("change", async (ev) => {
      try {
        await api(`/api/tasks/${task.id}`, {
          method: "PUT",
          body: { enabled: ev.target.checked ? 1 : 0 },
        });
        showToast(ev.target.checked ? "有効にしました" : "無効にしました");
        await loadTasks();
      } catch (e) {
        showToast(e.message);
        ev.target.checked = !ev.target.checked;
      }
    });
    row.querySelector('[data-act="edit"]').addEventListener("click", () => startEdit(task));
    row.querySelector('[data-act="delete"]').addEventListener("click", async () => {
      if (!confirm(`「${task.name}」を削除しますか？（実績も消えます）`)) return;
      try {
        await api(`/api/tasks/${task.id}`, { method: "DELETE" });
        showToast("削除しました");
        await loadTasks();
      } catch (e) {
        showToast(e.message);
      }
    });
    listEl.appendChild(row);
  }
}

// ---------------------------------------------------------------- フォーム

function readWeekdays() {
  return [...$("f-weekdays").querySelectorAll("input:checked")]
    .map((i) => i.value)
    .join(",");
}

function setWeekdays(weekdays) {
  const set = new Set(String(weekdays || "").split(",").map((s) => s.trim()));
  for (const input of $("f-weekdays").querySelectorAll("input")) {
    input.checked = set.has(input.value);
  }
}

function updateScheduleVisibility() {
  const isWeekly = $("f-schedule-type").value === "weekly";
  $("f-interval-wrap").hidden = isWeekly;
  $("f-weekdays-wrap").hidden = !isWeekly;
  $("f-adaptive-wrap").hidden = isWeekly;
}

function resetForm() {
  $("f-id").value = "";
  $("f-name").value = "";
  $("f-category").value = "その他";
  $("f-minutes").value = "10";
  $("f-schedule-type").value = "interval";
  $("f-interval").value = "3";
  setWeekdays("");
  $("f-adaptive").checked = true;
  $("f-notes").value = "";
  $("form-heading").textContent = "新規タスク追加";
  $("f-submit").textContent = "追加する";
  $("f-cancel").hidden = true;
  updateScheduleVisibility();
}

function startEdit(task) {
  $("f-id").value = String(task.id);
  $("f-name").value = task.name;
  $("f-category").value = task.category;
  if ($("f-category").value !== task.category) {
    // 一覧にないカテゴリは選択肢を足す
    const opt = document.createElement("option");
    opt.textContent = task.category;
    $("f-category").appendChild(opt);
    $("f-category").value = task.category;
  }
  $("f-minutes").value = String(task.est_minutes);
  $("f-schedule-type").value = task.schedule_type;
  $("f-interval").value = task.interval_days != null ? String(task.interval_days) : "3";
  setWeekdays(task.weekdays);
  $("f-adaptive").checked = Boolean(task.adaptive);
  $("f-notes").value = task.notes || "";
  $("form-heading").textContent = `タスク編集: ${task.name}`;
  $("f-submit").textContent = "保存する";
  $("f-cancel").hidden = false;
  updateScheduleVisibility();
  $("task-form").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("f-schedule-type").addEventListener("change", updateScheduleVisibility);
$("f-cancel").addEventListener("click", resetForm);

$("task-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const isWeekly = $("f-schedule-type").value === "weekly";
  const body = {
    name: $("f-name").value.trim(),
    category: $("f-category").value,
    est_minutes: parseInt($("f-minutes").value, 10) || 10,
    schedule_type: isWeekly ? "weekly" : "interval",
    interval_days: isWeekly ? null : parseFloat($("f-interval").value),
    weekdays: isWeekly ? readWeekdays() : null,
    adaptive: $("f-adaptive").checked ? 1 : 0,
    notes: $("f-notes").value.trim(),
  };
  if (isWeekly && !body.weekdays) {
    showToast("曜日を1つ以上選んでください");
    return;
  }
  const id = $("f-id").value;
  try {
    if (id) {
      await api(`/api/tasks/${id}`, { method: "PUT", body });
      showToast("保存しました");
    } else {
      await api("/api/tasks", { method: "POST", body });
      showToast("追加しました");
    }
    resetForm();
    await loadTasks();
  } catch (e) {
    showToast(e.message);
  }
});

// ---------------------------------------------------------------- テンプレ

async function loadTemplates() {
  const templates = await api("/api/templates");
  $("template-list").innerHTML = templates
    .map((t) => `
      <label>
        <input type="checkbox" value="${escapeHtml(t.name)}">
        <span>${escapeHtml(t.name)}</span>
        <span class="tpl-meta">${escapeHtml(scheduleLabel(t))}・${t.est_minutes}分</span>
      </label>`)
    .join("");
}

$("btn-apply-templates").addEventListener("click", async () => {
  const names = [...$("template-list").querySelectorAll("input:checked")]
    .map((i) => i.value);
  if (names.length === 0) {
    showToast("テンプレを選んでください");
    return;
  }
  try {
    const res = await api("/api/templates/apply", { method: "POST", body: { names } });
    showToast(`${res.created.length}件追加しました`);
    $("template-list").querySelectorAll("input:checked").forEach((i) => { i.checked = false; });
    await loadTasks();
  } catch (e) {
    showToast(e.message);
  }
});

// ---------------------------------------------------------------- 設定

async function loadSettings() {
  const s = await api("/api/settings");
  $("s-budget").value = s.daily_budget_min ?? "30";
  $("s-topic").value = s.ntfy_topic ?? "";
  $("s-server").value = s.ntfy_server ?? "https://ntfy.sh";
}

$("settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/settings", {
      method: "PUT",
      body: {
        daily_budget_min: $("s-budget").value || "30",
        ntfy_topic: $("s-topic").value.trim(),
        ntfy_server: $("s-server").value.trim() || "https://ntfy.sh",
      },
    });
    showToast("設定を保存しました");
  } catch (e) {
    showToast(e.message);
  }
});

// ---------------------------------------------------------------- 再生成

$("btn-regenerate").addEventListener("click", async () => {
  if (!confirm("今日のプランを作り直しますか？")) return;
  try {
    const res = await api("/api/plan/regenerate", { method: "POST" });
    showToast(`作り直しました（今日 ${res.total_count}件）`);
  } catch (e) {
    showToast(e.message);
  }
});

// ---------------------------------------------------------------- init

(async function init() {
  updateScheduleVisibility();
  try {
    await Promise.all([loadTasks(), loadTemplates(), loadSettings()]);
  } catch (e) {
    showToast(e.message);
  }
})();
