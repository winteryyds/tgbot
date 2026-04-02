#!/bin/bash
# 卸载脚本

set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "请使用 root 运行"; exit 1; }

echo "正在停止并卸载 tgbot..."

systemctl stop tgbot 2>/dev/null || true
systemctl disable tgbot 2>/dev/null || true
rm -f /etc/systemd/system/tgbot.service
systemctl daemon-reload

rm -rf /opt/tgbot

echo "保留数据目录 /var/lib/tgbot 和配置 /etc/tgbot（会话文件）"
echo "如需彻底删除：rm -rf /var/lib/tgbot /etc/tgbot"

# 删除用户（可选）
# userdel tgbot

echo "卸载完成。"
