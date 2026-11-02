from pathlib import Path
from datetime import datetime
from configparser import ConfigParser
import os
import re

PROJECT_DIR = Path(__file__).resolve().parent.parent

LOGS_DIR = PROJECT_DIR / "logs"
log_subdir = os.environ.get("PKU_BOOKING_LOG_SUBDIR", "")
if log_subdir:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", log_subdir):
        raise ValueError("无效的日志目录名称")
    LOGS_DIR = LOGS_DIR / log_subdir
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

CONFIG_FILE = PROJECT_DIR / "config.ini"
CONFIG = ConfigParser(interpolation=None)
CONFIG.read(CONFIG_FILE, encoding="utf-8")
