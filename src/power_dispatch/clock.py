"""可注入的时间源、IANA 时区解析与日历规则版本。

事件时刻（例如签收扫描）必须携带时区偏移，按绝对瞬间处理；船期到厂时刻是
设施所在地的本地日历时间，需要按设施 IANA 时区解析：

- 正常时刻：唯一映射到一个 UTC 瞬间；
- 缺失时刻（春令时跳变中空掉的本地时间）：向后顺延到跳变后的第一个合法时刻；
- 重复时刻（秋令时回拨中出现两次的本地时间）：取第二次出现的那个瞬间。

两种异常情形都归约为两个候选瞬间中较晚的一个（PEP 495 的 fold 语义），
因此规则确定、可审计，且不依赖调用方猜测偏移。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def load_zone(timezone_name: str, field: str = "timezone") -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{field} 不是受支持的 IANA 时区") from exc


def parse_naive_local(value: str, field: str) -> datetime:
    """解析设施本地日历时间；显式拒绝携带偏移的输入。"""
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 本地时间（YYYY-MM-DDTHH:MM:SS）") from exc
    if parsed.tzinfo is not None:
        raise ValueError(f"{field} 必须使用设施本地日历时间，不能携带时区偏移")
    if "T" not in text and " " not in text:
        raise ValueError(f"{field} 必须包含当日时刻")
    return parsed


def resolve_local(naive: datetime, zone: ZoneInfo) -> tuple[datetime, str]:
    """把设施本地日历时间解析为 UTC 瞬间。

    返回 ``(utc_instant, kind)``，kind 为 ``normal``、``gap``（缺失时刻）或
    ``repeated``（重复时刻）。缺失与重复都取较晚的候选瞬间：缺失时顺延到
    跳变之后，重复时取第二次出现。
    """
    first = naive.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
    second = naive.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)
    if first == second:
        kind = "normal"
    elif first < second:
        kind = "repeated"
    else:
        kind = "gap"
    return max(first, second), kind


def tz_version() -> str:
    """返回解析时采用的 IANA 时区数据库版本，随窗口冻结。"""
    zi_file = Path("/usr/share/zoneinfo/tzdata.zi")
    try:
        first_line = zi_file.read_text(encoding="utf-8").splitlines()[0]
    except OSError:
        pass
    else:
        parts = first_line.split()
        if len(parts) >= 3 and parts[0] == "#" and parts[1] == "version":
            return parts[2]
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("tzdata")
    except (ImportError, PackageNotFoundError):
        return "local"
