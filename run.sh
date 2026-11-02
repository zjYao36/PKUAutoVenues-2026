#!/bin/zsh
set -eu
cd -- "${0:A:h}"
export TZ=Asia/Shanghai

if [[ "${1:-}" == "check" ]]; then
  shift
  exec ./.venv/bin/python check_setup.py "$@"
fi

if [[ $# -eq 0 ]]; then
  exec ./.venv/bin/python -u booking.py --preview
fi

if [[ "${1:-}" == "preview" ]]; then
  shift
  exec ./.venv/bin/python -u booking.py --preview "$@"
fi

if [[ "${1:-}" == "start" ]]; then
  shift
  exec /usr/bin/caffeinate -i ./.venv/bin/python -u booking.py --start "$@"
fi

./.venv/bin/python check_setup.py --require-booking-config || exit $?
# 初次使用保留手动付款；此参数仍然会创建真实订单。
exec ./.venv/bin/python main.py "$@" --skip-pay
