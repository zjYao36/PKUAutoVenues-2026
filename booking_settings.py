"""不联网读取和校验日常预约配置。"""

from configparser import ConfigParser, Error as ConfigError
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
import re
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
VENUES = {
    "五四体育中心-室外网球场": "85", "五四室外网球": "85", "五四网球": "85",
    "五四体育中心-羽毛球馆": "86", "五四羽毛球": "86", "五四": "86", "54": "86",
    "邱德拔体育馆-羽毛球场": "60", "邱德拔羽毛球": "60", "邱德拔": "60", "qdb": "60",
}


@dataclass(frozen=True)
class BookingSettings:
    venue: str
    venue_name: str
    date: str
    start_time: str
    end_time: str
    courts: tuple[str, ...]
    release_at: datetime
    auto_pay: bool
    attempts: int
    retry_interval: float
    weekdays: tuple[int, ...] = ()
    advance_days: int = 3
    release_time: str = "12:00:00"
    profile: str = ""
    catch_up: bool = True


def occurrences(settings, now=None, days=35):
    """按打球日期产生未来任务；放号时间已过但尚未开场的日期仍可尝试。"""
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    if not settings.weekdays:
        yield settings
        return
    for offset in range(days):
        date = now.date() + timedelta(days=offset)
        if date.isoweekday() not in settings.weekdays:
            continue
        start = datetime.fromisoformat(f"{date}T{settings.start_time}").replace(tzinfo=TZ)
        if start <= now:
            continue
        release_at = datetime.fromisoformat(f"{date}T{settings.release_time}").replace(tzinfo=TZ) - timedelta(days=settings.advance_days)
        if not settings.catch_up and release_at < now - timedelta(minutes=5):
            continue
        yield replace(settings, date=date.isoformat(), release_at=release_at)


def load_settings(path: Path, now=None):
    config = ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        if not config.read(path, encoding="utf-8"):
            raise ValueError(f"找不到预约配置：{path}")
    except ConfigError as error:
        raise ValueError("预约设置文件格式有误，请检查配置段和等号。") from error
    try:
        venue_name = config.get("reservation", "venue").strip()
        venue = VENUES.get(venue_name, venue_name)
        if not venue.isdigit() or int(venue) <= 0:
            raise ValueError("venue 请填写已知场馆全名，或网页地址中的数字场馆 ID。")
        date = config.get("reservation", "date", fallback="").strip()
        weekdays_value = config.get("reservation", "weekdays", fallback="").strip()
        weekdays = ()
        if date and weekdays_value:
            raise ValueError("date 和 weekdays 只填一个：固定日期用 date，每周循环用 weekdays。")
        if weekdays_value:
            parts = [item.strip() for item in re.split(r"[,，]", weekdays_value)]
            if any(not re.fullmatch(r"[1-7]", item) for item in parts):
                raise ValueError("weekdays 填 1–7，用逗号分隔；周二到周五填 2,3,4,5。")
            weekdays = tuple(sorted(set(map(int, parts))))
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("请填写 date（如 2026-10-30），或 weekdays（如 2,3,4,5）。")
        if date:
            datetime.strptime(date, "%Y-%m-%d")
        start_time = config.get("reservation", "start_time").strip()
        end_time = config.get("reservation", "end_time").strip()
        for name, value in (("start_time", start_time), ("end_time", end_time)):
            if not re.fullmatch(r"\d{2}:\d{2}", value):
                raise ValueError(f"{name} 请使用 HH:MM 格式，如 06:00。")
            datetime.strptime(value, "%H:%M")
        if end_time <= start_time:
            raise ValueError("end_time 必须晚于 start_time，且在同一天。")
        now = now or datetime.now(TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=TZ)
        now = now.astimezone(TZ)
        if weekdays:
            for offset in range(8):
                candidate = now.date() + timedelta(days=offset)
                candidate_start = datetime.fromisoformat(f"{candidate}T{start_time}").replace(tzinfo=TZ)
                if candidate.isoweekday() in weekdays and candidate_start > now:
                    date = candidate.isoformat()
                    break
        start = datetime.fromisoformat(f"{date}T{start_time}").replace(tzinfo=TZ)
        if start <= now:
            raise ValueError("预约开始时间已经过去，请修改日期或时间。")
        names = [item.strip() for item in re.split(r"[,，]", config.get("reservation", "courts")) if item.strip()]
        if not names:
            raise ValueError("courts 至少填写一个场地编号或完整名称。")
        if any(name.isdigit() and int(name) <= 0 for name in names):
            raise ValueError("场地编号必须大于 0。")
        courts = tuple(dict.fromkeys(f"{int(name)}号" if name.isdigit() else name for name in names))
        advance_days = config.getint("execution", "advance_days", fallback=3)
        if not 0 <= advance_days <= 365:
            raise ValueError("advance_days 必须在 0–365 之间。")
        release_time = config.get("execution", "release_time", fallback="12:00:00").strip()
        if not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", release_time):
            raise ValueError("release_time 请填写 HH:MM 或 HH:MM:SS。")
        release_at = datetime.fromisoformat(f"{date}T{release_time}").replace(tzinfo=TZ) - timedelta(days=advance_days)
        if release_at >= start:
            raise ValueError("放号时间不能晚于或等于打球开始时间。")
        auto_pay = config.getboolean("execution", "auto_pay", fallback=False)
        catch_up = config.getboolean("execution", "catch_up", fallback=True)
        attempts = config.getint("execution", "attempts", fallback=20)
        retry_interval = config.getfloat("execution", "retry_interval", fallback=1)
        if not 1 <= attempts <= 100 or not 0.2 <= retry_interval <= 60:
            raise ValueError("attempts 应为 1–100，retry_interval 应为 0.2–60 秒。")
    except (ConfigError, KeyError) as error:
        raise ValueError(f"配置缺项：{error}") from error
    settings = BookingSettings(venue, venue_name, date, start_time, end_time, courts, release_at, auto_pay, attempts, retry_interval, weekdays, advance_days, release_time, catch_up=catch_up)
    if weekdays and not catch_up:
        settings = next(occurrences(settings, now=now), None)
        if settings is None:
            raise ValueError("近期没有符合星期和放号规则的任务，请检查提前天数。")
    return settings
