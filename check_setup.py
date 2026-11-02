"""检查配置、测试登录或单次验证码识别；不会创建订单或支付。"""

import argparse
import base64
import importlib.metadata
import json
import random
import sys
import time
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent


def install_test_guard():
    """仅允许登录、验证码和余额查询接口；付费识别最多发出一次。"""
    import requests

    original_send = requests.Session.send
    events = []
    post_endpoints = {
        ("iaaa.pku.edu.cn", "/iaaa/oauth.jsp"),
        ("iaaa.pku.edu.cn", "/iaaa/oauthlogin.do"),
        ("epe.pku.edu.cn", "/venue-server/api/login"),
        ("epe.pku.edu.cn", "/venue-server/roleLogin"),
        ("epe.pku.edu.cn", "/venue-server/api/captcha/check"),
        ("upload.chaojiying.net", "/Upload/GetScore.php"),
        ("upload.chaojiying.net", "/Upload/Processing.php"),
    }
    get_hosts = {"iaaa.pku.edu.cn", "epe.pku.edu.cn"}

    def guarded_send(session, request, **kwargs):
        url = urlsplit(request.url)
        endpoint = (url.hostname, url.path)
        if any(part in url.path.lower() for part in ("reservation", "order", "finances", "payment")):
            raise RuntimeError("测试保护：禁止预约、订单和支付请求")
        allowed = (
            url.scheme == "https"
            and (
                (request.method == "POST" and endpoint in post_endpoints)
                or (request.method == "GET" and url.hostname in get_hosts)
            )
        )
        if not allowed:
            raise RuntimeError("测试保护：请求不在登录/验证码接口范围内")
        if url.path == "/Upload/Processing.php" and any(
            event["path"] == url.path for event in events
        ):
            raise RuntimeError("测试保护：禁止重复上传付费识别请求")
        event = {"method": request.method, "host": url.hostname, "path": url.path}
        events.append(event)
        response = original_send(session, request, **kwargs)
        event["http_status"] = response.status_code
        return response

    requests.Session.send = guarded_send
    return events


def check_captcha(client, report):
    from utils.config import CONFIG
    from utils.encrypt import encrypt_aes_ecb, generate_uuid, md5_hash
    from utils.recognize import Recognizer

    def score():
        data = client.post(
            "https://upload.chaojiying.net/Upload/GetScore.php",
            data={
                "user": CONFIG["recognize:chaojiying"]["username"],
                "pass2": md5_hash(CONFIG["recognize:chaojiying"]["password"]),
            },
        ).json()
        if data.get("err_no") != 0:
            raise RuntimeError(f"超级鹰余额查询失败: {data.get('err_str', '未知错误')}")
        return int(data["tifen"])

    report["score_before"] = score()
    captcha = client.epe_get(
        "https://epe.pku.edu.cn/venue-server/api/captcha/get",
        params={
            "captchaType": "clickWord",
            "clientUid": f"point-{generate_uuid()}",
            "ts": str(int(time.time() * 1000)),
        },
    )
    if captcha.get("success") is not True:
        raise RuntimeError(f"获取验证码失败: {captcha.get('repMsg')}")
    data = captcha["repData"]
    words = data["wordList"]
    image_path = ROOT / "logs" / (report["test_id"] + ".png")
    image_path.write_bytes(base64.b64decode(data["originalImageBase64"]))
    report["captcha_image"] = str(image_path)
    report["words"] = words
    print(f"验证码获取成功，需定位 {len(words)} 个字。", flush=True)
    start = time.perf_counter()
    points = Recognizer().recognize_captcha(data["originalImageBase64"], words)
    report["recognition_seconds"] = round(time.perf_counter() - start, 3)
    report["recognized_points"] = points
    point_json = json.dumps([{"x": x, "y": y} for x, y in points], separators=(",", ":"))
    checked = client.epe_post(
        "https://epe.pku.edu.cn/venue-server/api/captcha/check",
        data={
            "captchaType": "clickWord",
            "pointJson": encrypt_aes_ecb(point_json, data["secretKey"]),
            "token": data["token"],
        },
    )
    report["venue_captcha_success"] = checked.get("success") is True
    report["venue_message"] = checked.get("repMsg")
    report["score_after"] = score()
    report["score_used"] = report["score_before"] - report["score_after"]
    print(f"识别耗时: {report['recognition_seconds']} 秒", flush=True)
    print(f"场馆验证码校验: {'通过' if report['venue_captcha_success'] else '未通过'}", flush=True)
    print(f"本次消耗题分: {report['score_used']}；剩余: {report['score_after']}", flush=True)
    if not report["venue_captcha_success"]:
        raise RuntimeError("场馆未接受验证码识别结果")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true", help="测试北大及智慧场馆登录")
    parser.add_argument("--captcha", action="store_true", help="测试一次超级鹰验证码识别及场馆校验，会消耗识别题分")
    parser.add_argument("--require-booking-config", action="store_true")
    args = parser.parse_args()

    print(f"Python: {sys.version.split()[0]}")
    for package in ("requests", "pycryptodome", "rsa"):
        print(f"{package}: {importlib.metadata.version(package)}")

    config = ConfigParser(interpolation=None)
    config.read(ROOT / "config.ini", encoding="utf-8")

    required_login = [("iaaa", "username"), ("iaaa", "password")]
    required_booking = [("epe", "phone")]
    method = config.get("recognize", "method", fallback="")
    if method in ("ttshitu", "chaojiying"):
        required_booking.extend(
            [(f"recognize:{method}", "username"), (f"recognize:{method}", "password")]
        )
        if method == "chaojiying":
            required_booking.append(("recognize:chaojiying", "softid"))
    else:
        required_booking.append(("recognize", "method"))

    missing_login = [
        f"[{section}] {key}"
        for section, key in required_login
        if not config.get(section, key, fallback="").strip()
    ]
    missing_booking = [
        f"[{section}] {key}"
        for section, key in required_booking
        if not config.get(section, key, fallback="").strip()
    ]
    if missing_login:
        print("登录配置待填写: " + ", ".join(missing_login))
    else:
        print("登录配置: 已填写（不显示内容）")
    if missing_booking:
        print("预约配置待填写: " + ", ".join(missing_booking))
    else:
        print("预约配置: 已填写（不显示内容）")

    if args.require_booking_config:
        return 2 if missing_login or missing_booking else 0
    if not (args.login or args.captcha):
        print("本地环境检查完成；未发送登录或预约请求。")
        return 0
    if missing_login:
        print("请先在本地 config.ini 填写登录账号。")
        return 2
    if args.captcha and (method != "chaojiying" or missing_booking):
        print("请先填写完整的超级鹰配置。")
        return 2

    report = None
    report_path = None
    if args.captcha:
        test_id = datetime.now().strftime("captcha-test_%Y-%m-%d_%H-%M-%S")
        report = {"test_id": test_id, "requests": install_test_guard(), "success": False}
        (ROOT / "logs").mkdir(exist_ok=True)
        report_path = ROOT / "logs" / (test_id + ".json")

    from utils.client import EpeClient
    from utils.encrypt import encrypt_rsa

    client = EpeClient("login-check")
    client.get("https://epe.pku.edu.cn/venue-server/loginto")
    client.post(
        "https://iaaa.pku.edu.cn/iaaa/oauth.jsp",
        data={
            "appID": "ty",
            "appName": "北京大学体测系统",
            "redirectUrl": "https://epe.pku.edu.cn/ggtypt/dologin",
            "redirectLogonUrl": "https://epe.pku.edu.cn/ggtypt/dologin",
        },
    )
    response = client.post(
        "https://iaaa.pku.edu.cn/iaaa/oauthlogin.do",
        data={
            "appid": "ty",
            "userName": config.get("iaaa", "username"),
            "password": encrypt_rsa(config.get("iaaa", "password")),
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "remTrustChk": "false",
            "redirUrl": "https://epe.pku.edu.cn/ggtypt/dologin",
        },
    ).json()
    if response.get("success") is not True or not response.get("token"):
        message = response.get("errors", {}).get("msg", "未返回 token")
        print(f"北大统一认证失败: {message}")
        return 1
    print("北大统一认证: 成功")
    client.get(
        "https://epe.pku.edu.cn/ggtypt/dologin",
        params={"_rand": random.random(), "token": response["token"]},
    )
    sso_token = client.session.cookies.get("sso_pku_token")
    if not sso_token:
        print("体测系统登录失败: 未收到 sso_pku_token")
        return 1
    login = client.epe_post(
        "https://epe.pku.edu.cn/venue-server/api/login",
        headers={"sso-token": sso_token},
    )
    client.cg_auth_token = login.get("token", {}).get("access_token")
    if not client.cg_auth_token:
        print("智慧场馆登录失败: 未收到 access_token")
        return 1
    role = client.epe_post(
        "https://epe.pku.edu.cn/venue-server/roleLogin", data={"roleid": 3}
    )
    if not role.get("token", {}).get("access_token"):
        print("智慧场馆身份登录失败: 未收到 access_token")
        return 1
    client.cg_auth_token = role["token"]["access_token"]
    print("智慧场馆登录及身份切换: 成功")
    if args.captcha:
        try:
            check_captcha(client, report)
            report["success"] = True
            return 0
        except Exception as error:
            report["error"] = f"{type(error).__name__}: {error}"
            print(f"验证码测试失败: {error}", flush=True)
            return 1
        finally:
            client.session.close()
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            report_path.chmod(0o600)
            print(f"测试报告: {report_path}", flush=True)
            print("验证码测试结束；未创建订单或支付。", flush=True)
    client.session.close()
    print("登录测试完成；未获取验证码、创建订单或支付。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
