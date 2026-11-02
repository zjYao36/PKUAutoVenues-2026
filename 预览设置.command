#!/bin/zsh
set -u
cd -- "${0:A:h}"
./run.sh preview
exit_code=$?
read -r '?按回车关闭此窗口…'
exit "$exit_code"
