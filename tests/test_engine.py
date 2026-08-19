"""app/engine.py のユニットテスト（SPEC「テスト」節: engine）。

- urgency 計算
- EWMA とクランプ（effective_interval）
- weekly 判定
- 予算充填（1件目は必ず入る）
- プラン決定性（同入力→同出力）
- skip が学習・対象判定に影響しないこと
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app import engine
from app.engine import JST

# 2025-06-02 は月曜（weekday=0）
MONDAY = date(2025, 6, 2)
TUESDAY = date(2025, 6, 3)


def make_task(
    id: int,
    name: str = "タスク",
    schedule_type: str = "interval",
    interval_days: float | None = 3,
    weekdays: str | None = None,
    est_minutes: int = 10,
    adaptive: int = 1,
    enabled: int = 1,
    created_at: str = "2025-01-01T00:00:00+09:00",
) -> dict:
    return {
        "id": id,
        "name": name,
        "category": "その他",
        "est_minutes": est_minutes,
        "schedule_type": schedule_type,
        "interval_days": interval_days,
        "weekdays": weekdays,
        "adaptive": adaptive,
        "enabled": enabled,
        "notes": "",
        "created_at": created_at,
    }


def done(task_id: int, dt: datetime) -> dict:
    return {"task_id": task_id, "completed_at": dt.isoformat(), "action": "done"}


def skip(task_id: int, dt: datetime) -> dict:
    return {"task_id": task_id, "completed_at": dt.isoformat(), "action": "skip"}


def jst(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=JST)


# ---------------------------------------------------------------- parse helpers

class TestParseHelpers:
    def test_parse_weekdays_basic(self):
        assert engine.parse_weekdays("0,3") == {0, 3}

    def test_parse_weekdays_ignores_invalid(self):
        assert engine.parse_weekdays("0, 9, x, 6") == {0, 6}

    def test_parse_weekdays_empty(self):
        assert engine.parse_weekdays(None) == set()
        assert engine.parse_weekdays("") == set()

    def test_gaps_from_done(self):
        dts = [jst(2025, 6, 1), jst(2025, 6, 3), jst(2025, 6, 6)]
        assert engine.gaps_from_done(dts) == pytest.approx([2.0, 3.0])

    def test_parse_dt_naive_is_jst(self):
        dt = engine.parse_dt("2025-06-01T12:00:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(hours=9)


# ---------------------------------------------------------------- effective_interval

class TestEffectiveInterval:
    def test_non_adaptive_returns_interval_days(self):
        task = make_task(1, interval_days=3, adaptive=0)
        assert engine.effective_interval(task, [10.0, 10.0, 10.0]) == 3.0

    def test_tiny_gaps_excluded_from_learning(self):
        """再送・二重記録由来の極小 gap（<0.1日）は EWMA 学習に使わない。"""
        task = make_task(1, interval_days=3, adaptive=1)
        # 極小 gap が混ざっても、それを除いた gap 列と同じ結果になる
        with_tiny = engine.effective_interval(task, [3.0, 0.001, 3.0, 3.0])
        without = engine.effective_interval(task, [3.0, 3.0, 3.0])
        assert with_tiny == without == pytest.approx(3.0)
        # 極小 gap を除くと2件未満になる場合は interval_days に戻る
        assert engine.effective_interval(task, [0.001, 0.002, 3.0]) == 3.0

    def test_fewer_than_two_gaps_returns_interval_days(self):
        task = make_task(1, interval_days=3, adaptive=1)
        assert engine.effective_interval(task, []) == 3.0
        assert engine.effective_interval(task, [5.0]) == 3.0

    def test_ewma_weights_recent_gap(self):
        # EWMA(α=0.3): 0.3*3.0 + 0.7*1.0 = 1.6（クランプ範囲 [1,4] 内）
        task = make_task(1, interval_days=2, adaptive=1)
        assert engine.effective_interval(task, [1.0, 3.0]) == pytest.approx(1.6)

    def test_clamp_high(self):
        # EWMA=10.0 だが上限 2.0*interval=4.0 にクランプ
        task = make_task(1, interval_days=2, adaptive=1)
        assert engine.effective_interval(task, [10.0, 10.0]) == pytest.approx(4.0)

    def test_clamp_low(self):
        # EWMA=0.5 だが下限 0.5*interval=1.0 にクランプ
        task = make_task(1, interval_days=2, adaptive=1)
        assert engine.effective_interval(task, [0.5, 0.5]) == pytest.approx(1.0)

    def test_uses_only_recent_five_gaps(self):
        # 6件あるうち最古の 100.0 は捨てられ、EWMA は 1.0 →
        # 下限 0.5*10=5.0 にクランプされる。
        # （もし 6 件全部使うと EWMA ≒ 17.6 でクランプされず失敗する）
        task = make_task(1, interval_days=10, adaptive=1)
        gaps = [100.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        assert engine.effective_interval(task, gaps) == pytest.approx(5.0)


# ---------------------------------------------------------------- urgency

class TestUrgency:
    def test_elapsed_over_interval(self):
        task = make_task(1)
        last = jst(2025, 6, 1)
        now = jst(2025, 6, 5)  # 4日経過
        assert engine.urgency(task, last, now, 2.0) == pytest.approx(2.0)

    def test_uses_created_at_when_no_last_done(self):
        task = make_task(1, created_at="2025-06-01T00:00:00+09:00")
        now = jst(2025, 6, 4)  # created_at から3日経過
        assert engine.urgency(task, None, now, 3.0) == pytest.approx(1.0)

    def test_zero_interval_is_always_due(self):
        task = make_task(1)
        assert engine.urgency(task, jst(2025, 6, 1), jst(2025, 6, 2), 0.0) == float("inf")


# ---------------------------------------------------------------- build_plan: weekly 判定

class TestBuildPlanWeekly:
    def test_weekly_included_on_matching_weekday(self):
        task = make_task(1, schedule_type="weekly", interval_days=None, weekdays="0")
        plan = engine.build_plan([task], [], MONDAY, 30)
        assert plan == [1]

    def test_weekly_excluded_on_other_weekday(self):
        task = make_task(1, schedule_type="weekly", interval_days=None, weekdays="0")
        plan = engine.build_plan([task], [], TUESDAY, 30)
        assert plan == []

    def test_weekly_excluded_if_done_today(self):
        task = make_task(1, schedule_type="weekly", interval_days=None, weekdays="0")
        history = [done(1, jst(2025, 6, 2, 8, 0))]
        plan = engine.build_plan([task], history, MONDAY, 30)
        assert plan == []

    def test_weekly_skip_today_does_not_exclude(self):
        # skip はその日のプラン運用上の話であり、再生成時の対象判定からは外さない
        task = make_task(1, schedule_type="weekly", interval_days=None, weekdays="0")
        history = [skip(1, jst(2025, 6, 2, 8, 0))]
        plan = engine.build_plan([task], history, MONDAY, 30)
        assert plan == [1]

    def test_disabled_task_excluded(self):
        task = make_task(1, schedule_type="weekly", interval_days=None,
                         weekdays="0", enabled=0)
        assert engine.build_plan([task], [], MONDAY, 30) == []


# ---------------------------------------------------------------- build_plan: interval 判定

class TestBuildPlanInterval:
    def test_urgent_task_included(self):
        # 最終完了から3日、interval=3 → urgency 1.0 で対象
        task = make_task(1, interval_days=3, adaptive=0)
        history = [done(1, jst(2025, 5, 31, 0, 0))]  # now=6/3 0:00 の3日前
        plan = engine.build_plan([task], history, MONDAY, 30)
        assert plan == [1]

    def test_not_yet_due_task_excluded(self):
        # 最終完了から3日、interval=5 → urgency 0.6 で対象外
        task = make_task(1, interval_days=5, adaptive=0)
        history = [done(1, jst(2025, 5, 31, 0, 0))]
        plan = engine.build_plan([task], history, MONDAY, 30)
        assert plan == []

    def test_no_history_uses_created_at(self):
        task = make_task(1, interval_days=3, adaptive=0,
                         created_at="2025-05-30T00:00:00+09:00")
        plan = engine.build_plan([task], [], MONDAY, 30)
        assert plan == [1]

    def test_empty_plan_when_no_targets(self):
        # 対象ゼロなら空リスト（無理に1件出さない）
        t1 = make_task(1, interval_days=30, adaptive=0,
                       created_at="2025-06-01T00:00:00+09:00")
        t2 = make_task(2, schedule_type="weekly", interval_days=None, weekdays="5")
        assert engine.build_plan([t1, t2], [], MONDAY, 30) == []


# ---------------------------------------------------------------- build_plan: calendar 判定（v4）

class TestBuildPlanCalendar:
    def _cal(self, id: int, **overrides) -> dict:
        return make_task(id, schedule_type="calendar", interval_days=None,
                         adaptive=0, **overrides)

    def test_included_only_when_in_gomi_due(self):
        task = self._cal(1)
        assert engine.build_plan([task], [], MONDAY, 30, gomi_due={1}) == [1]
        # gomi_due に無ければ常に対象外（urgency では絶対に入らない）
        assert engine.build_plan([task], [], MONDAY, 30, gomi_due=set()) == []
        assert engine.build_plan([task], [], MONDAY, 30) == []

    def test_placed_before_weekly_and_interval(self):
        cal9 = self._cal(9, est_minutes=5)
        w2 = make_task(2, schedule_type="weekly", interval_days=None,
                       weekdays="0", est_minutes=5)
        i1 = make_task(1, interval_days=3, adaptive=0, est_minutes=5)
        history = [done(1, jst(2025, 5, 28, 0, 0))]
        plan = engine.build_plan([i1, w2, cal9], history, MONDAY, 120, gomi_due={9})
        assert plan == [9, 2, 1]  # calendar → weekly → interval

    def test_excluded_if_done_today(self):
        task = self._cal(1)
        history = [done(1, jst(2025, 6, 2, 8, 0))]
        assert engine.build_plan([task], history, MONDAY, 30, gomi_due={1}) == []

    def test_disabled_excluded(self):
        task = self._cal(1, enabled=0)
        assert engine.build_plan([task], [], MONDAY, 30, gomi_due={1}) == []

    def test_est_minutes_counted_into_budget(self):
        # calendar の est_minutes も予算に算入される（先頭配置なので必ず入る）
        cal = self._cal(1, est_minutes=25)
        w2 = make_task(2, schedule_type="weekly", interval_days=None,
                       weekdays="0", est_minutes=10)
        plan = engine.build_plan([cal, w2], [], MONDAY, 30, gomi_due={1})
        assert plan == [1]  # 25 + 10 > 30 で weekly が入らない


# ---------------------------------------------------------------- build_plan: 並び順

class TestBuildPlanOrder:
    def test_weekly_first_then_interval_by_urgency_desc(self):
        # weekly は登録順（id 昇順）で先頭、interval は urgency 降順
        w2 = make_task(2, schedule_type="weekly", interval_days=None,
                       weekdays="0", est_minutes=5)
        w5 = make_task(5, schedule_type="weekly", interval_days=None,
                       weekdays="0", est_minutes=5)
        # id=1: 6日前完了 / interval 3 → urgency 2.0
        # id=3: 4日前完了 / interval 3 → urgency ≒ 1.33
        i1 = make_task(1, interval_days=3, adaptive=0, est_minutes=5)
        i3 = make_task(3, interval_days=3, adaptive=0, est_minutes=5)
        history = [
            done(1, jst(2025, 5, 28, 0, 0)),
            done(3, jst(2025, 5, 30, 0, 0)),
        ]
        plan = engine.build_plan([i1, w2, i3, w5], history, MONDAY, 120)
        assert plan == [2, 5, 1, 3]


# ---------------------------------------------------------------- build_plan: 予算充填

class TestBuildPlanBudget:
    def _urgent(self, id: int, est: int, urgency_rank: int) -> tuple[dict, dict]:
        """urgency_rank が大きいほど緊急なタスクと完了履歴を返す。"""
        task = make_task(id, interval_days=1, adaptive=0, est_minutes=est)
        hist = done(id, jst(2025, 6, 2) - timedelta(days=urgency_rank))
        return task, hist

    def test_first_task_always_included_even_over_budget(self):
        task, hist = self._urgent(1, est=60, urgency_rank=5)
        plan = engine.build_plan([task], [hist], MONDAY, 30)
        assert plan == [1]

    def test_stops_when_budget_exceeded(self):
        t1, h1 = self._urgent(1, est=25, urgency_rank=9)
        t2, h2 = self._urgent(2, est=10, urgency_rank=8)
        t3, h3 = self._urgent(3, est=5, urgency_rank=7)
        # 25 → +10 で 35 > 30 なので 2 件目以降は入らない
        plan = engine.build_plan([t1, t2, t3], [h1, h2, h3], MONDAY, 30)
        assert plan == [1]

    def test_fills_up_to_budget(self):
        t1, h1 = self._urgent(1, est=10, urgency_rank=9)
        t2, h2 = self._urgent(2, est=10, urgency_rank=8)
        t3, h3 = self._urgent(3, est=10, urgency_rank=7)
        plan = engine.build_plan([t1, t2, t3], [h1, h2, h3], MONDAY, 30)
        assert plan == [1, 2, 3]


# ---------------------------------------------------------------- 決定性

class TestDeterminism:
    def test_same_input_same_output(self):
        tasks = [
            make_task(1, interval_days=2, est_minutes=10),
            make_task(2, schedule_type="weekly", interval_days=None, weekdays="0,3"),
            make_task(3, interval_days=7, est_minutes=20),
        ]
        history = [
            done(1, jst(2025, 5, 25, 9, 0)),
            done(1, jst(2025, 5, 28, 21, 0)),
            done(1, jst(2025, 5, 31, 7, 30)),
            done(3, jst(2025, 5, 20, 10, 0)),
            skip(3, jst(2025, 5, 30, 10, 0)),
        ]
        p1 = engine.build_plan(tasks, list(history), MONDAY, 30)
        p2 = engine.build_plan(tasks, list(history), MONDAY, 30)
        assert p1 == p2

    def test_history_order_does_not_matter(self):
        tasks = [make_task(1, interval_days=2, adaptive=1)]
        history = [
            done(1, jst(2025, 5, 25)),
            done(1, jst(2025, 5, 27)),
            done(1, jst(2025, 5, 30)),
        ]
        p1 = engine.build_plan(tasks, history, MONDAY, 30)
        p2 = engine.build_plan(tasks, list(reversed(history)), MONDAY, 30)
        assert p1 == p2


# ---------------------------------------------------------------- skip と学習

class TestSkipDoesNotAffectLearning:
    def test_plan_identical_with_and_without_skips(self):
        tasks = [
            make_task(1, interval_days=3, adaptive=1),
            make_task(2, interval_days=2, adaptive=1),
        ]
        base_history = [
            done(1, jst(2025, 5, 24)),
            done(1, jst(2025, 5, 27)),
            done(1, jst(2025, 5, 30)),
            done(2, jst(2025, 5, 29)),
        ]
        skips = [
            skip(1, jst(2025, 5, 31)),
            skip(2, jst(2025, 6, 1)),
            skip(2, jst(2025, 6, 2, 8, 0)),
        ]
        p_without = engine.build_plan(tasks, base_history, MONDAY, 60)
        p_with = engine.build_plan(tasks, base_history + skips, MONDAY, 60)
        assert p_without == p_with

    def test_skip_does_not_reset_urgency(self):
        # 3日前に done、昨日 skip。skip が done 扱いされると urgency < 1 になり
        # 対象から外れてしまうが、正しくは対象のまま。
        task = make_task(1, interval_days=3, adaptive=0)
        history = [
            done(1, jst(2025, 5, 31, 0, 0)),
            skip(1, jst(2025, 6, 1, 9, 0)),
        ]
        plan = engine.build_plan([task], history, MONDAY, 30)
        assert plan == [1]
