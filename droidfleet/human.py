"""拟人化操作决策器。

只负责"怎么等、怎么点、怎么滑、看不看评论"，不直接操作设备。
所有时间/几何量都来自 rules/ 下的分布参数，不暴露固定阈值。
"""
from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, TypeVar

from droidfleet.rules import Rules, load_rules

Observation = Literal["success", "unchanged", "unknown"]
ScreenState = TypeVar("ScreenState")
Signature = TypeVar("Signature")


def read_after_interruption(profile: HumanProfile, read: Callable[[], ScreenState],
                            recoverable: tuple[type[Exception], ...], *,
                            deadline: float | None = None) -> ScreenState:
    """短暂中断后只重读页面，由应用根据读到的状态决定下一步。"""
    from droidfleet import log

    end = time.monotonic() + profile.rules.num("human.rhythm.recovery_seconds")
    if deadline is not None:
        end = min(end, deadline)
    interrupted = False
    while True:
        try:
            result = read()
        except recoverable as error:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise
            if not interrupted:
                log.event("recovery.wait", reason=str(error))
            interrupted = True
            time.sleep(min(profile.poll_delay(), remaining))
        else:
            if interrupted:
                log.event("recovery.read", outcome="fresh_state")
            return result


def confirm_stable(profile: HumanProfile, initial: ScreenState, capture: Callable[[], ScreenState],
                   signature: Callable[[ScreenState], Signature | None], *,
                   deadline: float | None = None,
                   matches: Callable[[Signature, Signature], bool] | None = None) -> ScreenState | None:
    """返回连续两次识别到同一目标时的最新页面。"""
    end = time.monotonic() + profile.rules.num("human.rhythm.stable.timeout_seconds")
    if deadline is not None:
        end = min(end, deadline)
    previous = signature(initial)
    while time.monotonic() < end:
        wait = profile.stable_delay()
        remaining = end - time.monotonic()
        if wait >= remaining:
            return None
        time.sleep(wait)
        screen = capture()
        if deadline is not None and time.monotonic() >= deadline:
            return None
        current = signature(screen)
        if (current is not None and previous is not None
                and (matches(previous, current) if matches is not None else current == previous)):
            return screen
        previous = current
    return None


def observe_tap(profile: HumanProfile, capture: Callable[[], ScreenState],
                classify: Callable[[ScreenState], Observation], *,
                deadline: float | None = None) -> tuple[ScreenState, Observation]:
    """只有连续观察到原状态，才允许调用方重新定位并修正一次。"""
    end = time.monotonic() + profile.rules.num("human.rhythm.retry_confirm_seconds")
    if deadline is not None:
        end = min(end, deadline)
    unchanged = True
    while True:
        screen = capture()
        status = classify(screen)
        if status == "success":
            return screen, status
        unchanged = unchanged and status == "unchanged"
        remaining = end - time.monotonic()
        if remaining <= 0:
            return screen, "unchanged" if unchanged else "unknown"
        time.sleep(min(profile.poll_delay(), remaining))


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class HumanProfile:
    rng: random.Random
    rules: Rules
    speed: float
    patience: float
    curiosity: float
    like_p: float
    fav_p: float
    scroll_style: float
    started: float = field(default_factory=time.monotonic)
    _liked_last: bool = False

    @classmethod
    def new(cls, seed: int | None = None, app: str | None = None,
            rules: Rules | None = None) -> "HumanProfile":
        rules = rules or load_rules(app)
        if seed is None:
            seed = int.from_bytes(os.urandom(8), "big")
        rng = random.Random(seed)
        speed = rng.uniform(*rules.range_of("human.rhythm.speed.range"))
        patience = rng.uniform(*rules.range_of("human.rhythm.patience.range"))
        curiosity = rng.uniform(*rules.range_of("human.consume.curiosity.range"))
        like_p = rng.uniform(*rules.range_of("human.consume.like.probability_range"))
        fav_p = rng.uniform(*rules.range_of("human.consume.favorite.probability_range"))
        scroll_style = rng.uniform(*rules.range_of("human.rhythm.scroll_style.range"))
        return cls(
            rng=rng,
            rules=rules,
            speed=speed,
            patience=patience,
            curiosity=curiosity,
            like_p=like_p,
            fav_p=fav_p,
            scroll_style=scroll_style,
        )

    # ---- 节奏 -----------------------------------------------------------
    def fatigue(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        minutes = max(0.0, (now - self.started) / 60.0)
        per_minute = self.rules.num("human.rhythm.fatigue.per_minute")
        cap = self.rules.num("human.rhythm.fatigue.cap_minutes")
        top = self.rules.num("human.rhythm.fatigue.max")
        return _clip(1.0 + per_minute * min(minutes, cap), 1.0, top)

    def page_dwell(self, complexity: float = 1.0) -> float:
        median = self.rules.num("human.rhythm.page_dwell.median")
        sigma = self.rules.num("human.rhythm.page_dwell.sigma")
        low, high = self.rules.range_of("human.rhythm.page_dwell.clip")
        base = self.rng.lognormvariate(math.log(median * complexity), sigma)
        return _clip(base * self.patience * self.fatigue(), low, high)

    def pre_tap(self, *, quick: bool = False) -> float:
        probability, long_range = self.rules.num("human.rhythm.pre_tap.long_probability"), \
            self.rules.range_of("human.rhythm.pre_tap.long_range")
        if not quick and self.rng.random() < probability:
            return self.rng.uniform(*long_range)
        low, high = self.rules.range_of("human.rhythm.pre_tap.range")
        clip_low, clip_high = self.rules.range_of("human.rhythm.pre_tap.clip")
        return _clip(self.rng.uniform(low, high) * self.patience, clip_low, clip_high)

    def post_tap(self, *, quick: bool = False) -> float:
        if not quick and self.rng.random() < self.rules.num("human.rhythm.post_tap.long_probability"):
            return self.rng.uniform(*self.rules.range_of("human.rhythm.post_tap.long_range"))
        low, high = self.rules.range_of("human.rhythm.post_tap.range")
        return self.rng.uniform(low, high) * (1.0 if quick else self.fatigue())

    def delay(self, seconds: float) -> float:
        return max(0.0, seconds) * self.rng.uniform(*self.rules.range_of("human.rhythm.delay_multiplier"))

    def poll_delay(self) -> float:
        return self.rng.uniform(*self.rules.range_of("human.rhythm.poll.range"))

    def stable_delay(self) -> float:
        return self.rng.uniform(*self.rules.range_of("human.rhythm.stable.sample_delay"))

    def swipe_pause(self) -> float:
        return self.rng.uniform(*self.rules.range_of("human.rhythm.swipe_pause.range"))

    def list_distance(self, purpose: Literal["search", "near", "correction"]) -> tuple[float, float]:
        return self.rules.range_of(f"human.touch.list_scroll.{purpose}")

    def same_bounds(self, first: tuple[float, ...], second: tuple[float, ...]) -> bool:
        tolerance = self.rules.num("human.touch.stable_tolerance_px")
        return all(abs(a - b) <= tolerance for a, b in zip(first, second, strict=True))

    def idle_pause(self) -> float:
        if self.rng.random() < self.rules.num("human.rhythm.idle.probability"):
            return self.rng.uniform(*self.rules.range_of("human.rhythm.idle.range"))
        return 0.0

    # ---- 触摸几何 -------------------------------------------------------
    def tap_offset(self, width: float, height: float) -> tuple[float, float]:
        sigma_frac = self.rules.num("human.touch.tap_offset.sigma_fraction")
        sigma_min = self.rules.num("human.touch.tap_offset.sigma_min_px")
        limit = self.rules.num("human.touch.tap_offset.limit_fraction")
        sx = max(width * sigma_frac, sigma_min)
        sy = max(height * sigma_frac, sigma_min)
        return (_clip(self.rng.gauss(0.0, sx), -width * limit, width * limit),
                _clip(self.rng.gauss(0.0, sy), -height * limit, height * limit))

    def tap_point(self, bounds: tuple[float, float, float, float],
                  width: int, height: int) -> tuple[int, int]:
        left, top, right, bottom = bounds
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ValueError("点击区域超出当前屏幕")
        x_min, x_max = math.floor(left) + 1, math.ceil(right) - 1
        y_min, y_max = math.floor(top) + 1, math.ceil(bottom) - 1
        if x_min > x_max or y_min > y_max:
            raise ValueError("目标区域过小，无法选择随机落点")
        center_x, center_y = (left + right) / 2, (top + bottom) / 2
        centers = {(int(center_x), int(center_y)), (round(center_x), round(center_y))}
        for _ in range(4):
            dx, dy = self.tap_offset(right - left, bottom - top)
            x = round(_clip(center_x + dx, x_min, x_max))
            y = round(_clip(center_y + dy, y_min, y_max))
            if (x, y) not in centers:
                return x, y
        neighbors = [(px, py) for px, py in ((x - 2, y), (x - 1, y), (x + 1, y), (x + 2, y),
                                             (x, y - 2), (x, y - 1), (x, y + 1), (x, y + 2))
                     if x_min <= px <= x_max and y_min <= py <= y_max and (px, py) not in centers]
        if not neighbors:
            raise ValueError("目标区域过小，无法选择非中心落点")
        return self.rng.choice(neighbors)

    def tap_hold(self) -> float:
        if self.rng.random() < self.rules.num("human.touch.tap_hold.probability"):
            return self.rng.uniform(*self.rules.range_of("human.touch.tap_hold.range"))
        return 0.0

    def swipe_duration(self) -> float:
        shape = self.rules.num("human.touch.swipe.duration_shape")
        scale = self.rules.num("human.touch.swipe.duration_scale")
        low, high = self.rules.range_of("human.touch.swipe.duration_clip")
        return _clip(self.rng.gammavariate(shape, scale) / self.speed, low, high)

    def swipe_distance(self, screen_height: float) -> float:
        low, high = self.rules.range_of("human.touch.swipe.distance_fraction")
        frac = self.rng.uniform(low, high)
        if self.rng.random() < self.rules.num("human.touch.swipe.long_probability"):
            frac = self.rng.uniform(*self.rules.range_of("human.touch.swipe.long_fraction"))
        return frac * screen_height * self.scroll_style

    def swipe(self, width: int, height: int, *, down: bool,
              area: tuple[float, float, float, float] | None = None,
              distance: tuple[float, float] | None = None) -> tuple[int, int, int, int, int]:
        if area is None:
            left, right = self.rules.range_of("human.touch.swipe.area_x")
            top, bottom = self.rules.range_of("human.touch.swipe.area_y")
        else:
            left, top, right, bottom = area
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("滑动区域须在屏幕内且具有正宽高")
        span = (bottom - top) * height
        length = (self.swipe_distance(span) if distance is None else
                  self.rng.uniform(*distance) * height * self.scroll_style)
        length = min(length, span * 0.95)
        upper = self.rng.uniform(top * height, bottom * height - length)
        lower = upper + length
        start_y, end_y = (lower, upper) if down else (upper, lower)
        start_x = self.rng.uniform(left, right) * width
        end_x = self.rng.uniform(left, right) * width
        return (int(start_x), int(start_y), int(end_x), int(end_y), int(self.swipe_duration() * 1000))

    def swipe_reverse(self) -> bool:
        return self.rng.random() < self.rules.num("human.touch.swipe.reverse_probability")

    # ---- 内容消费 -------------------------------------------------------
    def watch_plan(self, base: float | None = None) -> tuple[float, str]:
        base = self.rules.num("human.consume.watch.base_seconds") if base is None else base
        rewatch = self.rules.num("human.consume.watch.rewatch_probability")
        fast_p = self.rules.num("human.consume.watch.fast_probability")
        long_p = self.rules.num("human.consume.watch.long_probability")
        roll = self.rng.random()
        if roll < rewatch:
            return -1.0, "rewatch"
        if roll < rewatch + fast_p:
            return self.rng.uniform(*self.rules.range_of("human.consume.watch.fast_range")), "fast"
        if roll < rewatch + fast_p + long_p:
            multiplier = self.rng.uniform(*self.rules.range_of("human.consume.watch.long_multiplier"))
            low, high = self.rules.range_of("human.consume.watch.long_clip")
            return _clip(base * multiplier, low, high), "long"
        low, high = self.rules.range_of("human.consume.watch.clip")
        shape = self.rules.num("human.consume.watch.shape")
        scale = self.rules.num("human.consume.watch.scale")
        return _clip(base * self.rng.gammavariate(shape, scale), low, high), "watch"

    def comment_plan(self) -> dict | None:
        if self.rng.random() >= self.curiosity:
            return None
        scrolls_low, scrolls_high = self.rules.range_of("human.consume.comment.scrolls")
        return {
            "dwell": self.rng.uniform(*self.rules.range_of("human.consume.comment.dwell")),
            "scrolls": self.rng.randint(int(scrolls_low), int(scrolls_high)),
            "per_scroll": self.rng.uniform(*self.rules.range_of("human.consume.comment.per_scroll")),
            "like_comment": self.rng.random() < self.rules.num("human.consume.comment.like_probability"),
            "close_delay": self.rng.uniform(*self.rules.range_of("human.consume.comment.close_delay")),
        }

    def should_like(self) -> bool:
        suppress = self.rules.num("human.consume.like.repeat_suppress")
        if self._liked_last and self.rng.random() > suppress:
            return False
        hit = self.rng.random() < self.like_p
        self._liked_last = hit
        return hit

    def should_favorite(self) -> bool:
        return self.rng.random() < self.fav_p

    # ---- 任务节奏 -------------------------------------------------------
    def after_task_delay(self) -> float:
        """任务完成后、按返回/退出之前的随机停留（模拟注意力分散）。"""
        if self.rng.random() >= self.rules.num("human.after_task.exit_delay.probability"):
            return 0.0
        low, high = self.rules.range_of("human.after_task.exit_delay.range")
        return self.rng.uniform(low, high)

    def shopping_dwell(self) -> float:
        base = self.rules.num("human.tasks.shop.dwell_base")
        mean = self.rules.num("human.tasks.shop.dwell_extra_mean")
        low, high = self.rules.range_of("human.tasks.shop.clip")
        return _clip(base + self.rng.expovariate(1.0 / mean), low, high)

    def ad_tail(self) -> float:
        return self.rng.uniform(*self.rules.range_of("human.tasks.ad.tail"))

    def ad_close_delay(self) -> float:
        return self.rng.uniform(*self.rules.range_of("human.tasks.ad.close_delay"))

    def rapid_intervals(self, count: int) -> list[float]:
        per_second = self.rules.range_of("human.tasks.rapid.taps_per_second")
        low, high = 1.0 / per_second[1], 1.0 / per_second[0]
        return [self.rng.uniform(low, high) for _ in range(count)]

    def volume_index(self, max_index: int | None = None) -> int:
        if max_index is None:
            max_index = self.rules.integer("human.volume.fallback_max_index")
        if max_index <= 0:
            return 0
        low = self.rules.num("human.volume.low")
        high = self.rules.num("human.volume.high")
        min_index = self.rules.integer("human.volume.min_index")
        lo = max(min_index, int(math.ceil(low * max_index)))
        hi = max(lo, int(math.floor(high * max_index)))
        return self.rng.randint(min(lo, max_index), min(hi, max_index))

    def micro_jitter(self) -> float:
        return self.rng.uniform(*self.rules.range_of("human.touch.jitter.micro"))

    def jitter_px(self) -> float:
        return self.rules.num("human.touch.jitter.px")
