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
      <button type="button" class="icon-btn" data-act="done-now">✅ 今日やった</button>
      <button type="button" class="icon-btn" data-act="edit">編集</button>
      <button type="button" class="icon-btn danger" data-act="delete">削除</button>
    `;
    row.querySelector('[data-act="done-now"]').addEventListener("click", async () => {
      const { ok } = await appConfirm({
        title: `「${task.name}」を今日完了にしますか？`,
        message: "前倒し実施の記録です。次回の周期は今日を起点にずれます。",
        confirmLabel: "完了にする",
      });
      if (!ok) return;
      try {
        const res = await api(`/api/tasks/${task.id}/complete`, { method: "POST" });
        if (res.recorded) {
          showToast("完了を記録しました");
        } else {
          // 既に記録済み → 取り消し（誤操作の救済）を提案する
          const undo = await appConfirm({
            title: "今日は既に記録済みです",
            message: `「${task.name}」の今日の記録を取り消しますか？`,
            confirmLabel: "取り消す",
          });
          if (undo.ok) {
            const r = await api(`/api/tasks/${task.id}/uncomplete`, { method: "POST" });
            showToast(r.restored ? "未完了に戻しました" : "今日の記録はありません");
          }
        }
      } catch (e) {
        showToast(e.message);
      }
    });
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
      const { ok } = await appConfirm({
        title: `「${task.name}」を削除しますか？`,
        message: "完了実績も一緒に消えます。",
        confirmLabel: "削除する",
        danger: true,
      });
      if (!ok) return;
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
  const st = $("f-schedule-type").value;
  const isWeekly = st === "weekly";
  const isCalendar = st === "calendar";
  $("f-interval-wrap").hidden = isWeekly || isCalendar;
  $("f-weekdays-wrap").hidden = !isWeekly;
  $("f-adaptive-wrap").hidden = isWeekly || isCalendar;
}

function resetForm() {
  $("f-id").value = "";
  $("f-name").value = "";
  $("f-category").value = "その他";
  $("f-minutes").value = "10";
  $("f-schedule-type").value = "interval";
  $("f-schedule-type").disabled = false;
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
  // カレンダー連動タスクは種別変更不可（API が 422 を返す）。
  // 名前・分数・メモの編集は可能なので、種別だけ操作不能にする。
  $("f-schedule-type").disabled = task.schedule_type === "calendar";
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
  const st = $("f-schedule-type").value;
  const isWeekly = st === "weekly";
  const isCalendar = st === "calendar";
  const body = {
    name: $("f-name").value.trim(),
    category: $("f-category").value,
    est_minutes: parseInt($("f-minutes").value, 10) || 10,
    notes: $("f-notes").value.trim(),
  };
  if (!isCalendar) {
    // カレンダー連動はシステム管理のため schedule 系フィールドを送らない
    // （PUT は部分更新なので種別・間隔・曜日・adaptive はそのまま保たれる）
    body.schedule_type = isWeekly ? "weekly" : "interval";
    body.interval_days = isWeekly ? null : parseFloat($("f-interval").value);
    body.weekdays = isWeekly ? readWeekdays() : null;
    body.adaptive = $("f-adaptive").checked ? 1 : 0;
  }
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
  $("s-token").value = s.ntfy_token ?? "";
}

// 推測されにくいトピック名を生成（ntfy.sh のトピックは実質公開のため）
$("btn-gen-topic").addEventListener("click", () => {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  $("s-topic").value = `kajiflow-${hex}`;
  showToast("トピック名を生成しました。保存を忘れずに");
});

$("settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/settings", {
      method: "PUT",
      body: {
        daily_budget_min: $("s-budget").value || "30",
        ntfy_topic: $("s-topic").value.trim(),
        ntfy_server: $("s-server").value.trim() || "https://ntfy.sh",
        ntfy_token: $("s-token").value.trim(),
      },
    });
    showToast("設定を保存しました");
  } catch (e) {
    showToast(e.message);
  }
});

// ---------------------------------------------------------------- Google Tasks 連携

function formatSyncTime(iso) {
  // ISO 8601 → 'YYYY-MM-DD HH:MM'（表示用。パースできなければそのまま）
  const m = String(iso || "").match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]}` : String(iso || "");
}

function renderGtasksWarnings(warnings) {
  const listEl = $("gt-warnings");
  listEl.hidden = !warnings || warnings.length === 0;
  listEl.innerHTML = (warnings || [])
    .map((w) => `<li>${escapeHtml(w)}</li>`)
    .join("");
}

async function loadGtasksStatus() {
  const s = await api("/api/gtasks/status");
  $("gt-auth").textContent = s.authorized
    ? "認証状態: 認証済み"
    : "認証状態: 未認証";
  $("gt-auth-hint").hidden = s.authorized;
  $("gt-last-sync").textContent = s.last_sync_at
    ? `最終同期: ${formatSyncTime(s.last_sync_at)}`
    : "最終同期: まだ同期していません";
  renderGtasksWarnings(s.last_result ? s.last_result.warnings : []);
}

$("btn-gtasks-sync").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  btn.textContent = "同期中…";
  try {
    const res = await api("/api/gtasks/sync", { method: "POST" });
    const warnings = res.warnings || [];
    showToast(
      `同期しました（push ${res.pushed}件・Vault完了 ${res.completed_in_vault}件・` +
      `家事完了 ${res.completed_chores}件・新規取込 ${res.new_from_google}件` +
      (warnings.length ? `・警告 ${warnings.length}件` : "") + "）"
    );
    renderGtasksWarnings(warnings);
    await loadGtasksStatus();
  } catch (e) {
    showToast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "今すぐ同期";
  }
});

// ---------------------------------------------------------------- 再生成

$("btn-regenerate").addEventListener("click", async () => {
  const { ok } = await appConfirm({
    title: "今日のプランを作り直しますか？",
    message: "タスクの追加・変更を今日の分にすぐ反映します。",
    confirmLabel: "作り直す",
  });
  if (!ok) return;
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
    await Promise.all([loadTasks(), loadTemplates(), loadSettings(), loadGtasksStatus()]);
  } catch (e) {
    showToast(e.message);
  }
  // PWA 復帰時は表示系のみ再取得（設定フォームは入力中の値を壊さないため触らない）
  refreshOnReturn(async () => {
    try {
      await Promise.all([loadTasks(), loadGtasksStatus()]);
    } catch (e) {
      showToast(e.message);
    }
  });
})();
