import contextlib
from dataclasses import replace
from datetime import datetime
import fcntl
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from booking import execute, next_pending, profile_name, run_weekly, state_directory
from booking_settings import TZ, load_settings
from utils.selection import select_trades


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 31, 14, tzinfo=TZ)


class YumaoTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("requests.Session.send", side_effect=AssertionError("测试禁止联网"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        settings = load_settings(ROOT / "羽毛球预约设置.sample.ini", now=NOW)
        self.settings = replace(settings, profile="yumao")

    def receipt(self):
        job = self.settings
        return {"venue": job.venue, "date": job.date, "start_time": job.start_time, "end_time": job.end_time, "court": "3号", "order_id": 99, "trade_no": "YUMAO_TEST"}

    def runner(self, **kwargs):
        self.assertTrue(kwargs["skip_pay"])
        self.assertTrue(kwargs["strict_spaces"])
        self.assertEqual(kwargs["preferred_spaces"], ["3号", "4号", "5号", "8号", "9号"])
        self.assertEqual(kwargs["end_times"], ["22:00"])
        receipt = self.receipt()
        kwargs["before_submit"](receipt)
        kwargs["after_submit"](receipt)
        return receipt

    def test_daily_config_and_release_rule(self):
        plan = self.settings
        self.assertEqual(plan.venue, "60")
        self.assertEqual(plan.weekdays, (1, 2, 3, 4, 5, 6, 7))
        self.assertEqual((plan.start_time, plan.end_time), ("21:00", "22:00"))
        self.assertEqual(plan.courts, ("3号", "4号", "5号", "8号", "9号"))
        self.assertFalse(plan.auto_pay)
        self.assertEqual(plan.advance_days, 3)

    def test_afternoon_start_does_not_book_old_releases(self):
        self.assertEqual(self.settings.date, "2026-11-04")
        self.assertEqual(self.settings.release_at, datetime(2026, 11, 1, 12, tzinfo=TZ))

    def test_start_just_after_noon_keeps_current_release(self):
        plan = load_settings(ROOT / "羽毛球预约设置.sample.ini", now=datetime(2026, 10, 31, 12, 0, 3, tzinfo=TZ))
        self.assertEqual(plan.date, "2026-11-03")
        self.assertEqual(plan.release_at, datetime(2026, 10, 31, 12, tzinfo=TZ))

    def test_tennis_active_lock_does_not_block_badminton(self):
        (self.root / ".state").mkdir()
        with (self.root / ".state/active.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(execute(self.settings, self.root, self.runner), 0)
        state = state_directory(self.settings, self.root)
        self.assertEqual(len(list(state.glob("*.json"))), 1)
        self.assertEqual(list((self.root / ".state").glob("*.json")), [])

    def test_tennis_weekly_lock_does_not_block_badminton(self):
        (self.root / ".state").mkdir()
        with (self.root / ".state/weekly.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.redirect_stdout(io.StringIO()):
                run_weekly(self.settings, root=self.root, executor=lambda *a, **k: 0, clock=lambda: NOW, job_limit=1)
        self.assertEqual(len(list((self.root / ".state/profiles/yumao/runs").glob("*.json"))), 1)
        self.assertFalse((self.root / ".state/runs").exists())

    def test_second_badminton_instance_is_blocked(self):
        state = state_directory(self.settings, self.root)
        state.mkdir(parents=True)
        with (state / "weekly.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "已有每周"):
                run_weekly(self.settings, root=self.root, executor=lambda *a, **k: self.fail("不应运行"), clock=lambda: NOW, job_limit=1)

    def test_profile_paths_cannot_escape(self):
        for value in ("../tennis", "/tmp/test", "name/other"):
            with self.subTest(value=value), self.assertRaises(Exception):
                profile_name(value)

    def test_only_one_allowed_court_is_selected_in_order(self):
        date = self.settings.date
        rows = [
            {"id": number, "spaceName": f"{number}号", "1": {"reservationStatus": 1 if number in (2, 8, 9) else 4, "orderFee": 10}}
            for number in (2, 3, 4, 5, 8, 9)
        ]
        info = {"spaceTimeInfo": [{"id": 1, "beginTime": "21:00", "endTime": "22:00"}], "reservationDateSpaceInfo": {date: rows}}
        trades = select_trades(info, date, [("21:00", 1)], list(self.settings.courts), strict_spaces=True, end_times=["22:00"])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["spaceName"], "8号")
        for row in rows:
            if row["spaceName"] != "2号":
                row["1"]["reservationStatus"] = 4
        self.assertIsNone(select_trades(info, date, [("21:00", 1)], list(self.settings.courts), strict_spaces=True, end_times=["22:00"]))


if __name__ == "__main__":
    unittest.main()
