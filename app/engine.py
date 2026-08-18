"""スケジューリングエンジン（純ロジック・DB 非依存）。

すべて決定的: 乱数や現在時刻の暗黙参照は行わず、now / date は引数で受ける。
タスク・履歴は plain dict のリストとして受け取る。

- task: {id, name, est_minutes, schedule_type, interval_days, weekdays,
         adaptive, enabled, created_at, ...}
- history の 1 件: {task_id, completed_at, action}
- 日時文字列は ISO8601。naive の場合は Asia/Tokyo とみなす。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

EWMA_ALPHA = 0.3   # 新しい gap ほど重い
MAX_GAPS = 5       # 学習に使う直近 gap 数
CLAMP_LOW = 0.5    # eff_interval の下限係数
CLAMP_HIGH = 2.0   # eff_interval の上限係数


def parse_dt(value: str | datetime) -> datetime:
    """ISO8601 文字列を aware datetime にする。naive は JST とみなす。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt


def parse_weekdays(weekdays: str | None) -> set[int]:
    """'0,3' 形式（月=0..日=6）を集合にする。不正要素は無視。"""
    result: set[int] = set()
    if not weekdays:
        return result
    for part in str(weekdays).split(","):
        part = part.strip()
        if part.isdigit():
            n = int(part)
            if 0 <= n <= 6:
                result.add(n)
    return result


def gaps_from_done(done_datetimes: list[datetime]) -> list[float]:
    """'done' 完了日時のリスト（昇順ソートして扱う）から連続する間隔（日数）を返す。"""
    dts = sorted(done_datetimes)
    return [
        (b - a).total_seconds() / 86400.0
        for a, b in zip(dts, dts[1:])
    ]


def effective_interval(task: dict, gaps: list[float]) -> float:
    """実効間隔（日数）を返す。

    - adaptive=0 または gap 実績が 2 件未満: interval_days をそのまま返す。
    - adaptive=1: 直近最大 5 件の gap の EWMA（α=0.3、新しいほど重い）を
      [0.5*interval_days, 2.0*interval_days] にクランプして返す。
    """
    interval = float(task.get("interval_days") or 0.0)
    if not task.get("adaptive") or len(gaps) < 2:
        return interval
    recent = gaps[-MAX_GAPS:]  # 昇順（古い→新しい）前提
    ewma = recent[0]
    for gap in recent[1:]:
        ewma = EWMA_ALPHA * gap + (1.0 - EWMA_ALPHA) * ewma
    low = CLAMP_LOW * interval
    high = CLAMP_HIGH * interval
    return max(low, min(high, ewma))


def urgency(
    task: dict,
    last_done_at: datetime | str | None,
    now: datetime | str,
    eff_interval: float,
) -> float:
    """経過日数 / 実効間隔。last_done_at が無ければ created_at 起点。"""
    now_dt = parse_dt(now)
    if last_done_at is not None:
        anchor = parse_dt(last_done_at)
    else:
        anchor = parse_dt(task["created_at"])
    elapsed_days = (now_dt - anchor).total_seconds() / 86400.0
    if eff_interval <= 0:
        # 間隔ゼロは「常に対象」とみなす（ゼロ除算回避）
        return float("inf") if elapsed_days > 0 else 0.0
    return elapsed_days / eff_interval


def build_plan(
    tasks: list[dict],
    history: list[dict],
    plan_date: date,
    budget_min: int,
) -> list[int]:
    """当日プラン（task_id のリスト、提示順）を組み立てる。

    - 対象: weekly は「plan_date が該当曜日かつ当日 'done' 未記録」、
      interval は urgency >= 1.0（urgency は plan_date の終端時点で評価）。
    - 並び順: weekly を先頭（登録順=id 順）、次に interval を urgency 降順。
    - 予算充填: est_minutes を積み budget_min を超える手前まで。1件目は必ず入れる。
    - 対象ゼロなら空リスト。
    - skip は対象判定・学習のどちらにも影響しない。
    """
    # urgency の基準時刻: plan_date の終端（翌日 0:00 JST）。
    # その日のうちに期限が来るタスクを当日プランに含めるため。
    now = datetime.combine(plan_date + timedelta(days=1), time(0, 0), tzinfo=JST)
    date_str = plan_date.isoformat()

    # task_id ごとの 'done' 完了日時（skip は学習・判定に使わない）
    done_by_task: dict[int, list[datetime]] = {}
    done_today: set[int] = set()
    for row in history:
        if row.get("action", "done") != "done":
            continue
        tid = row["task_id"]
        dt = parse_dt(row["completed_at"])
        done_by_task.setdefault(tid, []).append(dt)
        if dt.astimezone(JST).date().isoformat() == date_str:
            done_today.add(tid)

    weekly_part: list[dict] = []
    interval_part: list[tuple[float, int, dict]] = []

    for task in tasks:
        if not task.get("enabled", 1):
            continue
        if task.get("schedule_type") == "weekly":
            if plan_date.weekday() not in parse_weekdays(task.get("weekdays")):
                continue
            if task["id"] in done_today:
                continue
            weekly_part.append(task)
        else:  # interval
            dones = sorted(done_by_task.get(task["id"], []))
            gaps = gaps_from_done(dones)
            eff = effective_interval(task, gaps)
            last_done = dones[-1] if dones else None
            u = urgency(task, last_done, now, eff)
            if u >= 1.0:
                interval_part.append((u, task["id"], task))

    # weekly: 登録順（id 昇順）、interval: urgency 降順（同値は id 昇順で決定的に）
    weekly_part.sort(key=lambda t: t["id"])
    interval_part.sort(key=lambda x: (-x[0], x[1]))

    ordered = weekly_part + [t for _, _, t in interval_part]
    if not ordered:
        return []

    plan: list[int] = []
    total = 0
    for i, task in enumerate(ordered):
        est = int(task.get("est_minutes") or 0)
        if i == 0:
            plan.append(task["id"])
            total += est
            continue
        if total + est > budget_min:
            break
        plan.append(task["id"])
        total += est
    return plan
