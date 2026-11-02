#!/bin/zsh
set -u
cd -- "${0:A:h}"
./run.sh start
exit_code=$?
read -r '?按回车关闭此窗口…'
exit "$exit_code"
