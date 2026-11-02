#!/bin/zsh
set -eu
cd -- "${0:A:h}"
export TZ=Asia/Shanghai
export PKU_BOOKING_LOG_SUBDIR=yumao

booking_mode="${1:-preview}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$booking_mode" in
  preview)
    exec ./.venv/bin/python -u booking.py --preview --profile yumao --config "$PWD/羽毛球预约设置.ini" "$@"
    ;;
  start)
    exec /usr/bin/caffeinate -i ./.venv/bin/python -u booking.py --start --profile yumao --config "$PWD/羽毛球预约设置.ini" "$@"
    ;;
  check)
    exec ./.venv/bin/python -u check_setup.py "$@"
    ;;
  -h|--help|help)
    print '羽毛球独立入口：'
    print './run_yumao.sh preview   预览羽毛球预约设置（默认，不联网）'
    print './run_yumao.sh start     按配置等待放号并实际预约'
    print './run_yumao.sh check     检查共用的账号和依赖'
    print '配置文件：羽毛球预约设置.ini'
    ;;
  *)
    print -u2 '用法：./run_yumao.sh [preview|start|check|--help]'
    exit 2
    ;;
esac
