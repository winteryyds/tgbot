#!/bin/bash
# =============================================================
# Telegram File Extractor Bot — LXC 一键部署脚本
# 适用系统：Debian 11/12、Ubuntu 20.04/22.04/24.04
# 用法：chmod +x install.sh && sudo bash install.sh
# =============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---------- 检测运行权限 ----------
[[ $EUID -eq 0 ]] || die "请使用 root 或 sudo 运行此脚本"

# ---------- 检测系统 ----------
. /etc/os-release 2>/dev/null || true
log "系统：${PRETTY_NAME:-unknown}"

# ---------- 安装 Python 3 ----------
log "安装依赖：python3 python3-venv python3-pip..."
if command -v apt-get &>/dev/null; then
    apt-get update -qq
    apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates
elif command -v yum &>/dev/null; then
    yum install -y python3 python3-pip
elif command -v dnf &>/dev/null; then
    dnf install -y python3 python3-pip
else
    die "不支持的包管理器，请手动安装 python3 和 pip"
fi

PYTHON=$(command -v python3)
PY_VER=$($PYTHON --version 2>&1)
log "Python 版本：$PY_VER"

# ---------- 创建系统用户 ----------
if ! id tgbot &>/dev/null; then
    log "创建系统用户 tgbot..."
    useradd --system --no-create-home --shell /usr/sbin/nologin tgbot
fi

# ---------- 创建目录 ----------
log "创建应用目录..."
mkdir -p /opt/tgbot
mkdir -p /var/lib/tgbot/sessions
mkdir -p /etc/tgbot

# ---------- 复制应用文件 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log "复制文件：$SCRIPT_DIR → /opt/tgbot"
cp "$SCRIPT_DIR/bot.py"          /opt/tgbot/
cp "$SCRIPT_DIR/requirements.txt" /opt/tgbot/

# ---------- 创建虚拟环境并安装依赖 ----------
log "创建 Python 虚拟环境..."
$PYTHON -m venv /opt/tgbot/venv

log "安装 Python 依赖（telethon）..."
/opt/tgbot/venv/bin/pip install --upgrade pip --quiet
/opt/tgbot/venv/bin/pip install -r /opt/tgbot/requirements.txt --quiet

# ---------- 配置环境变量 ----------
if [[ ! -f /etc/tgbot/tgbot.env ]]; then
    cp "$SCRIPT_DIR/tgbot.env" /etc/tgbot/tgbot.env
    warn "已创建配置文件 /etc/tgbot/tgbot.env"
    warn "请编辑该文件，填写 BOT_TOKEN、API_ID、API_HASH，然后运行："
    warn "  systemctl start tgbot"
else
    log "配置文件已存在，跳过覆盖：/etc/tgbot/tgbot.env"
fi

# ---------- 设置权限 ----------
chown -R tgbot:tgbot /opt/tgbot /var/lib/tgbot
chown root:root /etc/tgbot/tgbot.env
chmod 600 /etc/tgbot/tgbot.env

# ---------- 安装 systemd 服务 ----------
log "安装 systemd 服务..."
cp "$SCRIPT_DIR/tgbot.service" /etc/systemd/system/tgbot.service
systemctl daemon-reload
systemctl enable tgbot

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} 安装完成！${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "下一步："
echo "  1. 编辑配置：nano /etc/tgbot/tgbot.env"
echo "  2. 启动服务：systemctl start tgbot"
echo "  3. 查看状态：systemctl status tgbot"
echo "  4. 查看日志：journalctl -u tgbot -f"
echo ""
