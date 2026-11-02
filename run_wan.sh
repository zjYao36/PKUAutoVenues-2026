#!/bin/zsh
set -eu
cd -- "${0:A:h}"
export TZ=Asia/Shanghai
export PKU_BOOKING_LOG_SUBDIR=wangqiu_wan

booking_mode="${1:-preview}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$booking_mode" in
  preview)
    exec ./.venv/bin/python -u booking.py --preview --profile wangqiu_wan --config "$PWD/晚间网球预约设置.ini" "$@"
    ;;
  start)
    exec /usr/bin/caffeinate -i ./.venv/bin/python -u booking.py --start --profile wangqiu_wan --config "$PWD/晚间网球预约设置.ini" "$@"
    ;;
  check)
    exec ./.venv/bin/python -u check_setup.py "$@"
    ;;
  -h|--help|help)
    print '晚间网球独立入口：'
    print './run_wan.sh preview   预览晚间网球设置（默认，不联网）'
    print './run_wan.sh start     等待放号并预约晚间网球'
    print './run_wan.sh check     检查共用账号和依赖'
    print '配置文件：晚间网球预约设置.ini'
    ;;
  *)
    print -u2 '用法：./run_wan.sh [preview|start|check|--help]'
    exit 2
    ;;
esac
