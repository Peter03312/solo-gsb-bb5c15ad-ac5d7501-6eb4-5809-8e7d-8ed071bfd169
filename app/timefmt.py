"""时间工具：ISO 8601 输入按真实瞬间归一到 UTC，保留输入精度（含小数秒）。

规则（与 README「时间精度」一致）：
  * 带时区（Z / ±HH:MM 等偏移）输入：先换算为真实 UTC 瞬间再比较，不同时区表示
    的同一瞬间视为同一时刻；
  * 无时区输入：仍按 UTC 解释（不做任何本地时区推断）；
  * 归一结果保留小数秒精度：整秒输出 ``...:01:02Z``（与历史整秒记录字节一致），
    非整秒输出 ``...:01:02.345000Z``（微秒精度，以微秒为最小规范单位）；
  * 比较一律按解析后的真实 UTC 瞬间进行，绝不依赖字符串字典序——同秒的不同瞬间
    （``.100`` / ``.200`` / ``.300``）必须判为先后不同，不能因截去小数秒而误判。
"""
from __future__ import annotations

from datetime import datetime, timezone


def to_utc_iso(dt: datetime) -> str:
    """把（可能带时区的）输入时刻按真实瞬间归一为 UTC ISO 8601 字符串。

    * ``dt.tzinfo is None``（无时区）→ 按 UTC 解释；
    * 带时区 → ``astimezone(UTC)``，保持真实瞬间；
    * 整秒输出秒精度 ``Z`` 形式（与历史整秒记录字节一致，保障同号重试/指纹兼容）；
      非整秒输出微秒精度 ``Z`` 形式（以微秒为最小规范单位，末尾补零到 6 位）。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    if dt_utc.microsecond == 0:
        # 与历史整秒规范完全一致：YYYY-MM-DDTHH:MM:SSZ
        return dt_utc.replace(microsecond=0).isoformat(timespec="seconds").replace("+00:00", "Z")
    # 非整秒：固定微秒精度（6 位小数，末尾补零），保证规范串可按瞬间唯一确定，
    # 且不与整秒串产生字典序歧义（比较仍一律走 parse_iso，不依赖字符串序）。
    return dt_utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    """解析库内归一化 ISO 字符串（或遗留整秒串）为带 UTC 时区的 datetime。

    库内串恒为 UTC（``Z`` 结尾，秒或微秒精度）；此函数对历史遗留数据与未来可能的
    其它合法 ISO 形式同样健壮。
    """
    text = s
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        # 防御：库内不应出现无时区串；若出现仍按 UTC 解释
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """把输入时刻归一为带 UTC 时区的 datetime（比较用，保留真实瞬间与微秒精度）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def instant_eq(a: str | datetime, b: str | datetime) -> bool:
    """真实瞬间相等（不同时区表示同一瞬间 → True；同秒不同小数 → False）。"""
    return _coerce(a) == _coerce(b)


def instant_lt(a: str | datetime, b: str | datetime) -> bool:
    """真实瞬间严格早于。"""
    return _coerce(a) < _coerce(b)


def instant_le(a: str | datetime, b: str | datetime) -> bool:
    """真实瞬间早于或等于。"""
    return _coerce(a) <= _coerce(b)


def instant_gt(a: str | datetime, b: str | datetime) -> bool:
    """真实瞬间严格晚于。"""
    return _coerce(a) > _coerce(b)


def instant_ge(a: str | datetime, b: str | datetime) -> bool:
    """真实瞬间晚于或等于。"""
    return _coerce(a) >= _coerce(b)


def seconds_between(start: str | datetime, end: str | datetime) -> float:
    """两个真实瞬间之间的秒数（end - start，可为小数；保留小数秒精度）。"""
    return (_coerce(end) - _coerce(start)).total_seconds()


def _coerce(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return to_utc(value)
    return parse_iso(value)
