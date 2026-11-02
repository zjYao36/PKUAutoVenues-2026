import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
import fcntl
from unittest.mock import patch

from booking import next_pending, run_weekly
from booking_settings import TZ, load_settings, occurrences


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 26, 14, tzinfo=TZ)


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch("requests.Session.send", side_effect=AssertionError("测试禁止联网"))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "weekly.ini"
        source = (ROOT / "预约设置.sample.ini").read_text(encoding="utf-8")
        values = {"weekdays": "2,3,4,5", "start_time": "10:00", "end_time": "12:00", "courts": "2,3", "advance_days": "1"}
        for key, value in values.items():
            source = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", source)
        self.path.write_text(source, encoding="utf-8")
        self.settings = load_settings(self.path, now=NOW)

    def test_rule_preserves_time_courts_and_user_release_rule(self):
        plan = self.settings
        self.assertEqual(plan.weekdays, (2, 3, 4, 5))
        self.assertEqual((plan.start_time, plan.end_time), ("10:00", "12:00"))
        self.assertEqual(plan.courts, ("2号", "3号"))
        self.assertFalse(plan.auto_pay)
        self.assertEqual(plan.date, "2026-10-27")
        self.assertEqual(plan.release_at, datetime(2026, 10, 26, 12, tzinfo=TZ))

    def test_dates_skip_weekend_and_repeat_next_week(self):
        jobs = list(occurrences(self.settings, now=NOW, days=10))
        self.assertEqual([job.date for job in jobs], ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-03", "2026-11-04"])
        self.assertTrue(all(job.release_at.isoweekday() in (1, 2, 3, 4) for job in jobs))

    def test_started_interval_is_skipped(self):
        now = datetime(2026, 10, 30, 10, 30, tzinfo=TZ)
        plan = load_settings(self.path, now=now)
        self.assertEqual(plan.date, "2026-11-03")
        self.assertEqual(next(occurrences(plan, now=now)).date, "2026-11-03")

    def test_single_date_and_weekdays_are_mutually_exclusive(self):
        self.path.write_text(self.path.read_text().replace("date =\n", "date = 2026-10-30\n"))
        with self.assertRaisesRegex(ValueError, "只填一个"):
            load_settings(self.path, now=NOW)

    def test_invalid_weekdays(self):
        source = self.path.read_text()
        for value in ("0,2", "8", "2-5", "2,,3"):
            self.path.write_text(source.replace("weekdays = 2,3,4,5", f"weekdays = {value}"))
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_settings(self.path, now=NOW)

    def test_scheduler_runs_each_day_once_and_keeps_no_pay(self):
        seen = []
        def execute(job, root):
            seen.append(job.date)
            self.assertFalse(job.auto_pay)
            self.assertEqual(job.courts, ("2号", "3号"))
            return 0
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_weekly(self.settings, root=self.root, executor=execute, clock=lambda: NOW, job_limit=5)
        self.assertEqual(result, 0)
        self.assertEqual(seen, ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-03"])

    def test_failure_and_restart_do_not_repeat_same_day(self):
        seen = []
        def execute(job, root):
            seen.append(job.date)
            return 1
        with contextlib.redirect_stdout(io.StringIO()):
            run_weekly(self.settings, root=self.root, executor=execute, clock=lambda: NOW, job_limit=1)
            run_weekly(self.settings, root=self.root, executor=execute, clock=lambda: NOW, job_limit=1)
        self.assertEqual(seen, ["2026-10-27", "2026-10-28"])

    def test_existing_submission_is_skipped(self):
        (self.root / "logs").mkdir()
        record = {"venueSiteId": "85", "date": "2026-10-27", "beginTime": "10:00", "endTime": "12:00", "spaceName": "2号", "success": True}
        (self.root / "logs/booking-once_previous.json").write_text(json.dumps(record))
        self.assertEqual(next_pending(self.settings, root=self.root, now=NOW).date, "2026-10-28")

    def test_second_weekly_process_is_blocked(self):
        state = self.root / ".state"
        state.mkdir()
        with (state / "weekly.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "已有每周抢场任务"):
                run_weekly(self.settings, root=self.root, executor=lambda *a, **k: self.fail("不应执行"), clock=lambda: NOW, job_limit=1)

    def test_repeated_logger_does_not_duplicate_output(self):
        from utils.logger import Logger
        with patch("utils.logger.LOG_FILE", self.root / "logger-test.log"):
            first = Logger("weekly-unit-test")
            second = Logger("weekly-unit-test")
        try:
            self.assertIs(first._file_handler, second._file_handler)
            owned = [handler for handler in first._logger.handlers if getattr(handler, "_pku_output", None)]
            self.assertEqual(len(owned), 2)
        finally:
            for handler in list(first._logger.handlers):
                first._logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
