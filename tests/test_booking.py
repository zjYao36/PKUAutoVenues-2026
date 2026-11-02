import contextlib
from dataclasses import replace
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from booking import execute, find_existing, validate_receipt
from booking_settings import TZ, load_settings
from utils.selection import select_trades


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 26, 14, tzinfo=TZ)


def plan(path, date="2026-10-30"):
    source = (ROOT / "预约设置.sample.ini").read_text(encoding="utf-8")
    path.write_text(source.replace("date =\n", f"date = {date}\n"), encoding="utf-8")
    return load_settings(path, now=NOW)


def inventory(second_end="08:00", first_status=1):
    return {
        "spaceTimeInfo": [
            {"id": 1, "beginTime": "06:00", "endTime": "07:00"},
            {"id": 2, "beginTime": "07:00", "endTime": second_end},
        ],
        "reservationDateSpaceInfo": {"2026-10-30": [
            {"id": 243, "spaceName": "1号", "1": {"reservationStatus": first_status, "orderFee": 0}, "2": {"reservationStatus": 1, "orderFee": 5.5}},
            {"id": 244, "spaceName": "2号", "1": {"reservationStatus": 1, "orderFee": 0}, "2": {"reservationStatus": 1, "orderFee": 5.5}},
        ]},
    }


class BookingTests(unittest.TestCase):
    def setUp(self):
        # 整组测试不允许任何真实网络请求。
        self.network_guard = patch("requests.Session.send", side_effect=AssertionError("测试禁止联网"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = plan(self.root / "settings.ini")

    def receipt(self, court="1号"):
        return {"venue": "85", "date": "2026-10-30", "start_time": "06:00", "end_time": "07:00", "court": court, "order_id": 123, "trade_no": "TEST123"}

    def test_release_time_and_no_payment_default(self):
        self.assertEqual(self.settings.release_at, datetime(2026, 10, 27, 12, tzinfo=TZ))
        self.assertFalse(self.settings.auto_pay)
        self.assertEqual(self.settings.courts, ("1号",))

    def test_invalid_and_empty_dates(self):
        for value in ("", "2026-02-30", "2026-10-25"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan(self.root / "invalid.ini", date=value)

    def test_alternative_courts_keep_order(self):
        path = self.root / "settings.ini"
        path.write_text(path.read_text().replace("courts = 1", "courts = 2,1,2"))
        self.assertEqual(load_settings(path, now=NOW).courts, ("2号", "1号"))

    def test_exact_court_and_time(self):
        selected = select_trades(inventory(), self.settings.date, [("06:00", 1)], ["1号"], strict_spaces=True, end_times=["07:00"])
        self.assertEqual([trade["spaceId"] for trade in selected], ["243"])
        self.assertEqual(selected[-1]["endTime"], "07:00")

    def test_no_fallback_to_other_courts(self):
        self.assertIsNone(select_trades(inventory(first_status=4), self.settings.date, [("06:00", 1)], ["1号"], strict_spaces=True, end_times=["07:00"]))

    def test_whole_interval_not_shortened(self):
        self.assertIsNone(select_trades(inventory(), self.settings.date, [("06:00", 1)], ["1号"], strict_spaces=True, end_times=["09:00"]))
        selected = select_trades(inventory(), self.settings.date, [("06:00", 1)], ["1号"], strict_spaces=True, end_times=["08:00"])
        self.assertEqual(len(selected), 2)
        self.assertEqual(sum(trade["orderFee"] for trade in selected), 5.5)

    def test_gap_cannot_be_booked_as_contiguous(self):
        data = inventory()
        data["spaceTimeInfo"][1]["beginTime"] = "07:30"
        self.assertIsNone(select_trades(data, self.settings.date, [("06:00", 1)], ["1号"], strict_spaces=True, end_times=["08:00"]))

    def test_sub_hour_slots_cover_exact_end(self):
        data = inventory(second_end="07:00")
        data["spaceTimeInfo"][0]["endTime"] = "06:30"
        data["spaceTimeInfo"][1]["beginTime"] = "06:30"
        result = select_trades(data, self.settings.date, [("06:00", 1)], ["1号"], strict_spaces=True, end_times=["07:00"])
        self.assertEqual(len(result), 2)

    def test_launcher_uses_config_and_records_success(self):
        def runner(**kwargs):
            self.assertTrue(kwargs["skip_pay"])
            self.assertTrue(kwargs["strict_spaces"])
            self.assertEqual(kwargs["end_times"], ["07:00"])
            self.assertEqual(kwargs["release_at"], self.settings.release_at)
            receipt = self.receipt()
            kwargs["before_submit"](receipt)
            kwargs["after_submit"](receipt)
            return receipt
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(execute(self.settings, self.root, runner), 0)
        self.assertEqual(find_existing(self.settings, self.root)["state"], "submitted")
        with self.assertRaisesRegex(ValueError, "不重复下单"):
            execute(self.settings, self.root, runner)

    def test_unknown_submission_blocks_rerun(self):
        def runner(**kwargs):
            kwargs["before_submit"](self.receipt())
            return None
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(execute(self.settings, self.root, runner), 1)
        self.assertEqual(find_existing(self.settings, self.root)["state"], "submitting")
        with self.assertRaises(ValueError):
            execute(self.settings, self.root, runner)

    def test_changed_court_is_rejected_before_submission(self):
        with self.assertRaises(ValueError):
            validate_receipt(self.settings, self.receipt(court="2号"))

    def test_previous_one_off_order_is_detected(self):
        (self.root / "logs").mkdir()
        record = {"venueSiteId": "85", "date": "2026-10-30", "beginTime": "06:00", "endTime": "07:00", "spaceName": "1号", "success": True}
        (self.root / "logs/booking-once_previous.json").write_text(json.dumps(record))
        self.assertIsNotNone(find_existing(self.settings, self.root))

    def test_multiple_court_receipts_do_not_overwrite(self):
        for court in ("1号", "2号"):
            settings = replace(self.settings, courts=(court,))
            def runner(**kwargs):
                receipt = self.receipt(court=court)
                kwargs["before_submit"](receipt)
                kwargs["after_submit"](receipt)
                return receipt
            with contextlib.redirect_stdout(io.StringIO()):
                execute(settings, self.root, runner)
        self.assertEqual(len(list((self.root / ".state").glob("*.json"))), 2)

    def test_submission_network_timeout_is_not_retried(self):
        from utils.client import Client
        with patch("utils.client.Logger"):
            client = Client("test")
        client.session.request = MagicMock(side_effect=requests.ReadTimeout("simulated"))
        with self.assertRaises(Exception):
            client.post("https://epe.pku.edu.cn/venue-server/api/reservation/order/submit", data={})
        self.assertEqual(client.session.request.call_count, 1)

    def exercise_main(self, fail_submit):
        import main as program
        fake = MagicMock()
        fake.post.return_value.json.return_value = {"success": True, "token": "dummy"}
        fake.session.cookies.get.return_value = "dummy-sso"
        def epe_post(url, **kwargs):
            if url.endswith("/api/login") or url.endswith("/roleLogin"):
                return {"token": {"access_token": "dummy"}}
            if url.endswith("/captcha/check"):
                return {"success": True}
            if url.endswith("/order/submit"):
                if fail_submit:
                    raise RuntimeError("simulated submit failure")
                return {"id": 123, "tradeNo": "TEST123"}
            raise AssertionError("unexpected API")
        def epe_get(url, **kwargs):
            if url.endswith("/captcha/get"):
                return {"success": True, "repData": {"originalImageBase64": "dGVzdA==", "wordList": ["字"], "token": "dummy", "secretKey": "dummy"}}
            return inventory()
        fake.epe_post.side_effect = epe_post
        fake.epe_get.side_effect = epe_get
        with contextlib.ExitStack() as stack:
            for name in ("Logger", "Notifier", "wait_until", "encrypt_rsa", "encrypt_aes_ecb", "generate_order_pin"):
                stack.enter_context(patch.object(program, name))
            stack.enter_context(patch.object(program, "CONFIG", {"iaaa": {"username": "test", "password": "dummy"}, "epe": {"phone": "test"}}))
            stack.enter_context(patch.object(program, "LOGS_DIR", self.root))
            stack.enter_context(patch.object(program, "EpeClient", return_value=fake))
            recognizer = stack.enter_context(patch.object(program, "Recognizer"))
            recognizer.return_value.recognize_captcha.return_value = [(1, 1)]
            stack.enter_context(patch.object(program.time, "sleep"))
            result = program.main("85", "2026-10-30", [("06:00", 1)], ["1号"], True, strict_spaces=True, end_times=["07:00"], max_turns=20)
        return result, fake

    def test_main_does_not_repeat_after_submission_error(self):
        result, fake = self.exercise_main(fail_submit=True)
        self.assertIsNone(result)
        submits = [call for call in fake.epe_post.call_args_list if call.args[0].endswith("/order/submit")]
        self.assertEqual(len(submits), 1)

    def test_main_success_never_calls_payment_when_disabled(self):
        result, fake = self.exercise_main(fail_submit=False)
        self.assertEqual(result["trade_no"], "TEST123")
        self.assertFalse(any("/finances/" in call.args[0] for call in fake.epe_post.call_args_list))


if __name__ == "__main__":
    unittest.main()
