#!/bin/bash
# Telegram File Extractor Bot V2 卸载脚本

set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo "请使用 root 运行"; exit 1; }

echo "正在停止并卸载 tgbot..."

systemctl stop tgbot 2>/dev/null || true
systemctl disable tgbot 2>/dev/null || true
rm -f /etc/systemd/system/tgbot.service
systemctl daemon-reload

rm -rf /opt/tgbot

echo "已删除程序目录：/opt/tgbot"
echo "已保留数据目录 /var/lib/tgbot 和配置目录 /etc/tgbot（含 session / env）"
echo "如需彻底删除：rm -rf /var/lib/tgbot /etc/tgbot"
echo "注意：本脚本不会卸载系统级 ffmpeg / python3，以免影响其他程序"
echo "卸载完成。"
