"""读取预约设置，预览或启动单次/每周循环预约。"""

import argparse
from dataclasses import replace
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys

from booking_settings import TZ, load_settings, occurrences


ROOT = Path(__file__).resolve().parent


def profile_name(value):
    if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        raise argparse.ArgumentTypeError("任务名称只能包含字母、数字、下划线和短横线。")
    return value


def state_directory(settings, root=ROOT):
    if settings.profile:
        profile_name(settings.profile)
        return root / ".state" / "profiles" / settings.profile
    return root / ".state"


def show_plan(settings):
    now = datetime.now(TZ)
    print(f"场馆：{settings.venue_name}（ID {settings.venue}）")
    print(f"打球日期：{settings.date}")
    print(f"完整时段：{settings.start_time}–{settings.end_time}")
    print(f"只约这些场地：{'、'.join(settings.courts)}（按顺序尝试）")
    print(f"放号时间：{settings.release_at:%Y-%m-%d %H:%M:%S} 北京时间")
    print(f"计划登录：{settings.release_at - timedelta(minutes=1):%Y-%m-%d %H:%M:%S}")
    if settings.release_at <= now:
        print("放号时间已过；启动后将立即尝试预约。")
    print(f"自动付款：{'开启' if settings.auto_pay else '关闭，按订单页提示手动处理'}")
    print(f"当日最多 {settings.attempts} 轮；成功一单后{'继续等待下一打球日' if settings.weekdays else '结束'}。")
    print("启动后使用这一份设置；修改配置需停止任务后重新启动。", flush=True)


def find_existing(settings, root=ROOT):
    state_dir = state_directory(settings, root)
    paths = list(state_dir.glob("*.json")) + list((root / "logs").glob("booking-once_*.json"))
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            if path.parent == state_dir:
                raise ValueError("本地预约记录无法读取，请先核对已有订单。") from error
            continue
        venue = record.get("venueSiteId", record.get("venue"))
        date = record.get("date")
        start = record.get("start_time", record.get("beginTime"))
        end = record.get("end_time", record.get("endTime"))
        court = record.get("court", record.get("spaceName"))
        submitted = record.get("state") in ("submitting", "submitted") or record.get("success") or record.get("submit_attempts", 0) > 0
        if submitted and (str(venue), date, start, end) == (settings.venue, settings.date, settings.start_time, settings.end_time) and court in settings.courts:
            return record
    return None


def validate_receipt(settings, receipt):
    if (
        str(receipt.get("venue")) != settings.venue
        or receipt.get("date") != settings.date
        or receipt.get("start_time") != settings.start_time
        or receipt.get("end_time") != settings.end_time
        or receipt.get("court") not in settings.courts
    ):
        raise ValueError("选中的预约与配置不一致，已停止下单。")


def save_receipt(path, receipt, state):
    record = {**receipt, "state": state, "updated_at": datetime.now(TZ).isoformat()}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def job_key(settings):
    value = "|".join((settings.venue, settings.date, settings.start_time, settings.end_time, ",".join(settings.courts)))
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def next_pending(settings, root=ROOT, now=None):
    for job in occurrences(settings, now=now):
        completed = state_directory(job, root) / "runs" / (job_key(job) + ".json")
        if completed.exists() or find_existing(job, root):
            continue
        return job
    return None


def run_weekly(settings, root=ROOT, executor=None, clock=None, job_limit=None):
    """终端中持续运行；每个打球日期执行一轮任务，并记录成功或失败。"""
    state_dir = state_directory(settings, root)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    runs = state_dir / "runs"
    runs.mkdir(exist_ok=True)
    runs.chmod(0o700)
    executor = executor or execute
    clock = clock or (lambda: datetime.now(TZ))
    handled = 0
    with (state_dir / "weekly.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("已有每周抢场任务在运行，请先停止原任务。") from error
        while True:
            job = next_pending(settings, root=root, now=clock())
            if job is None:
                print("近期符合规则的日期都已有处理记录，任务已停止。")
                return 0
            print(f"\n下一打球日：{job.date}，{job.start_time}–{job.end_time}", flush=True)
            result = executor(job, root=root)
            record = {
                "venue": job.venue, "date": job.date, "start_time": job.start_time,
                "end_time": job.end_time, "courts": list(job.courts),
                "result": "submitted" if result == 0 else "failed",
            }
            save_receipt(runs / (job_key(job) + ".json"), record, "finished")
            handled += 1
            if job_limit is not None and handled >= job_limit:
                return 0


def execute(settings, root=ROOT, runner=None):
    state_dir = state_directory(settings, root)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    with (state_dir / "active.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("已有一个抢场任务正在运行，请先停止原任务。") from error
        existing = find_existing(settings, root)
        if existing:
            number = existing.get("trade_no") or existing.get("order", {}).get("tradeNo")
            suffix = f"（订单号 {number}）" if number else ""
            raise ValueError(f"这个日期、时段和场地已有提交记录{suffix}，不重复下单。请先在预约记录中核对。")

        receipt_path = state_dir / (job_key(settings) + ".json")
        submitted = False

        def before_submit(receipt):
            nonlocal submitted
            validate_receipt(settings, receipt)
            if submitted:
                raise ValueError("本次任务已发送过下单请求，停止重复发送。")
            save_receipt(receipt_path, receipt, "submitting")
            submitted = True

        def after_submit(receipt):
            validate_receipt(settings, receipt)
            save_receipt(receipt_path, receipt, "submitted")

        if runner is None:
            from main import main as runner
        result = runner(
            venue=settings.venue,
            target_date=settings.date,
            target_times=[(settings.start_time, 1)],
            preferred_spaces=list(settings.courts),
            skip_pay=not settings.auto_pay,
            strict_spaces=True,
            end_times=[settings.end_time],
            release_at=settings.release_at,
            max_turns=settings.attempts,
            retry_delay=settings.retry_interval,
            before_submit=before_submit,
            after_submit=after_submit,
        )
        if result:
            print(f"预约订单已提交：{result['trade_no']}")
            print(f"{settings.date} {settings.start_time}–{settings.end_time}，{result['court']}")
            print("请在预约记录查看最终状态及是否需要手动付款：https://epe.pku.edu.cn/venue/orders")
            return 0
        if submitted:
            print("已尝试下单，但结果未能确认；请先查看预约记录，不要重复运行。")
        else:
            print("本次未约到指定场地，任务已结束。")
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--start", action="store_true", help="启动实际预约；weekdays 模式会每周循环")
    mode.add_argument("--preview", action="store_true", help="只预览设置，不联网或预约（默认）")
    parser.add_argument("--config", type=Path, default=ROOT / "预约设置.ini")
    parser.add_argument("--profile", type=profile_name, default="", help="独立任务名称，默认使用原网球任务记录")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        settings = replace(settings, profile=args.profile)
        if settings.weekdays:
            labels = "一二三四五六日"
            print("循环打球日：" + "、".join("周" + labels[day - 1] for day in settings.weekdays))
            pending = next_pending(settings)
            if pending is None:
                raise ValueError("近期符合规则的日期都已有处理记录。")
            print("下面是下一次任务的设置：")
            show_plan(pending)
        else:
            show_plan(settings)
        existing = find_existing(settings)
        if existing and not settings.weekdays:
            raise ValueError("这个场地和时段已有提交记录，请先到预约记录核对，避免重复预约。")
        if not args.start:
            print("这是预览，没有登录、调用验证码服务或下单。")
            return 0
        subprocess.run([sys.executable, str(ROOT / "check_setup.py"), "--require-booking-config"], cwd=ROOT, check=True)
        if settings.weekdays:
            return run_weekly(settings)
        return execute(settings)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"未启动预约：{error}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        print("任务已停止；已经提交的订单请到预约记录核对。", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
