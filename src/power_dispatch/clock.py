"""可注入的 UTC 时间源，以及按设施 IANA 时区解析本地窗口的规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# 缺失时刻（春季跳时）：向前推移到跳时后第一个有效时刻。
GAP_POLICY_SHIFT_FORWARD = "shift_forward"
# 重复时刻（秋季回拨）：采用第一次出现（fold=0，仍带夏令时偏移）。
AMBIGUITY_POLICY_FIRST_OCCURRENCE = "first_occurrence"
END_OF_DAY = "24:00"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        if self.current.tzinfo is None:
            raise ValueError("冻结时钟必须带时区")
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str, field: str = "时间") -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 ISO 8601 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def facility_zone(timezone_name: str, field: str = "timezone") -> ZoneInfo:
    """按 IANA 名称构造设施时区，拒绝缩写或裸偏移。"""
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError(f"{field} 必须是 IANA 时区")
    name = timezone_name.strip()
    # EST、CET 等缩写虽然在 tz 库中作为遗留兼容区域存在，但不是位置型 IANA 时区。
    if name != "UTC" and "/" not in name:
        raise ValueError(f"{field} 必须是位置型 IANA 时区，如 Asia/Shanghai")
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{field} 不是有效的 IANA 时区") from exc
    return zone


def calendar_version() -> str:
    """返回窗口冻结时采用的日历版本，随窗口一并持久化。"""
    try:
        return "tzdata-" + version("tzdata")
    except PackageNotFoundError:
        pass
    try:
        for line in Path("/usr/share/zoneinfo/tzdata.zi").read_text().splitlines():
            if line.startswith("# version "):
                return "system-" + line.split()[2]
        version_text = Path("/usr/share/zoneinfo/+VERSION").read_text().strip()
        return "system-" + version_text
    except OSError:
        return "system-unknown"


def _wall_clock_time(value: object, field: str) -> time:
    """解析窗口的本地墙钟时间 HH:MM 或 HH:MM:SS。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 HH:MM 本地时间")
    text = value.strip()
    try:
        parts = text.split(":")
        if len(parts) == 2:
            return time(int(parts[0]), int(parts[1]))
        if len(parts) == 3:
            return time(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, TypeError):
        pass
    raise ValueError(f"{field} 必须是 HH:MM 或 HH:MM:SS 本地时间")


def parse_wall_clock(value: object, field: str, *, allow_end_of_day: bool = False) -> str:
    """校验窗口本地墙钟时间，返回规范化文本；允许 24:00 表示当日结束。"""
    if allow_end_of_day and isinstance(value, str) and value.strip() == END_OF_DAY:
        return END_OF_DAY
    _wall_clock_time(value, field)
    return value.strip()  # type: ignore[union-attr]


def resolve_local_instant(naive: datetime, zone: ZoneInfo) -> tuple[datetime, str]:
    """把设施本地的朴素墙钟时刻解析为 UTC 时刻。

    返回 (UTC 时刻, 解析类别)，类别为 unique / gap_shifted / ambiguous_first。
    """
    fold_zero = naive.replace(tzinfo=zone, fold=0)
    fold_one = naive.replace(tzinfo=zone, fold=1)
    offset_zero = fold_zero.utcoffset()
    offset_one = fold_one.utcoffset()
    if offset_zero == offset_one:
        return fold_zero.astimezone(timezone.utc), "unique"
    roundtrip = fold_zero.astimezone(timezone.utc).astimezone(zone)
    if roundtrip.replace(tzinfo=None) != naive.replace(tzinfo=None):
        # 缺失时刻：两次 fold 给出的偏移夹住跳时空档，向前推移空档长度。
        shifted_naive = naive + (offset_one - offset_zero)
        shifted = shifted_naive.replace(tzinfo=zone, fold=0)
        return shifted.astimezone(timezone.utc), "gap_shifted"
    # 重复时刻：fold=0 对应第一次出现（仍带夏令时偏移）。
    return fold_zero.astimezone(timezone.utc), "ambiguous_first"


def local_window(
    zone: ZoneInfo,
    day: date,
    start_value: object,
    end_value: object,
) -> dict[str, object]:
    """解析设施当地某个到厂日的窗口，结束早于开始时顺延到下一自然日（跨午夜）。"""
    start_time = _wall_clock_time(start_value, "window_start_local")
    if isinstance(end_value, str) and end_value.strip() == END_OF_DAY:
        end_naive = datetime.combine(day + timedelta(days=1), time.min)
        crosses_midnight = True
        ends_at, end_resolution = resolve_local_instant(end_naive, zone)
    else:
        end_time = _wall_clock_time(end_value, "window_end_local")
        crosses_midnight = end_time <= start_time
        end_date = day + timedelta(days=1) if crosses_midnight else day
        ends_at, end_resolution = resolve_local_instant(
            datetime.combine(end_date, end_time), zone
        )
    starts_at, start_resolution = resolve_local_instant(
        datetime.combine(day, start_time), zone
    )
    if ends_at <= starts_at:
        raise ValueError("到厂窗口结束时刻必须晚于开始时刻")
    return {
        "starts_at": starts_at,
        "ends_at": ends_at,
        "crosses_midnight": crosses_midnight,
        "start_resolution": start_resolution,
        "end_resolution": end_resolution,
    }
