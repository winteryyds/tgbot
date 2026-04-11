#!/bin/bash
# =============================================================
# Telegram File Extractor Bot V2 - LXC 一键部署脚本
# 适用系统：Debian 11/12、Ubuntu 20.04/22.04/24.04
# 用法：chmod +x install.sh && sudo bash install.sh
# 可选参数：INSTALL_FFMPEG=0 bash install.sh
# =============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

APP_NAME="tgbot"
APP_DIR="/opt/tgbot"
STATE_DIR="/var/lib/tgbot"
CONF_DIR="/etc/tgbot"
SERVICE_FILE="/etc/systemd/system/tgbot.service"
INSTALL_FFMPEG="${INSTALL_FFMPEG:-1}"

log()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "请使用 root 或 sudo 运行此脚本"
command -v systemctl >/dev/null 2>&1 || die "当前系统未检测到 systemd，无法安装 tgbot.service"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for required_file in bot.py requirements.txt tgbot.env tgbot.service uninstall.sh README.md; do
    [[ -f "${SCRIPT_DIR}/${required_file}" ]] || die "缺少文件：${required_file}"
done

. /etc/os-release 2>/dev/null || true
log "系统：${PRETTY_NAME:-unknown}"

install_base_packages() {
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            python3 python3-venv python3-pip ca-certificates
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-pip
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 python3-pip
    else
        die "不支持的包管理器，请手动安装 python3、pip 和 ffmpeg"
    fi
}

install_ffmpeg_if_needed() {
    [[ "${INSTALL_FFMPEG}" != "0" ]] || {
        warn "已按要求跳过 ffmpeg 安装；如需视频抽帧封面，请手动安装 ffmpeg"
        return 0
    }

    if command -v ffmpeg >/dev/null 2>&1; then
        log "检测到 ffmpeg 已安装：$(command -v ffmpeg)"
        return 0
    fi

    log "尝试安装 ffmpeg（仅系统包，不增加 Python 依赖）..."
    if command -v apt-get >/dev/null 2>&1; then
        if ! DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg; then
            warn "ffmpeg 安装失败，机器人仍可运行，但将无法本地抽帧生成视频封面"
        fi
    elif command -v dnf >/dev/null 2>&1; then
        if ! dnf install -y ffmpeg; then
            warn "ffmpeg 安装失败，机器人仍可运行，但将无法本地抽帧生成视频封面"
        fi
    elif command -v yum >/dev/null 2>&1; then
        if ! yum install -y ffmpeg; then
            warn "ffmpeg 安装失败，机器人仍可运行，但将无法本地抽帧生成视频封面"
        fi
    fi
}

log "安装基础依赖：python3 / venv / pip"
install_base_packages
install_ffmpeg_if_needed

PYTHON="$(command -v python3)"
PY_VER="$(${PYTHON} --version 2>&1)"
log "Python 版本：${PY_VER}"

if ! id "${APP_NAME}" >/dev/null 2>&1; then
    log "创建系统用户 ${APP_NAME}..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "${APP_NAME}"
fi

log "创建目录..."
install -d -m 0755 "${APP_DIR}" "${CONF_DIR}"
install -d -o "${APP_NAME}" -g "${APP_NAME}" -m 0750 "${STATE_DIR}" "${STATE_DIR}/sessions"

log "复制程序文件到 ${APP_DIR}"
install -m 0644 "${SCRIPT_DIR}/bot.py" "${APP_DIR}/bot.py"
install -m 0644 "${SCRIPT_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"
install -m 0644 "${SCRIPT_DIR}/README.md" "${APP_DIR}/README.md"
install -m 0750 "${SCRIPT_DIR}/uninstall.sh" "${APP_DIR}/uninstall.sh"

log "创建 Python 虚拟环境..."
"${PYTHON}" -m venv "${APP_DIR}/venv"

log "安装 Python 依赖（Telethon）..."
"${APP_DIR}/venv/bin/pip" install --disable-pip-version-check --no-cache-dir --upgrade pip >/dev/null
"${APP_DIR}/venv/bin/pip" install --disable-pip-version-check --no-cache-dir -r "${APP_DIR}/requirements.txt" >/dev/null

if [[ ! -f "${CONF_DIR}/tgbot.env" ]]; then
    install -m 0600 "${SCRIPT_DIR}/tgbot.env" "${CONF_DIR}/tgbot.env"
    warn "已创建配置文件 ${CONF_DIR}/tgbot.env"
    warn "请先填写 BOT_TOKEN、API_ID、API_HASH，再启动服务"
else
    log "配置文件已存在，跳过覆盖：${CONF_DIR}/tgbot.env"
fi

log "安装 systemd 服务..."
install -m 0644 "${SCRIPT_DIR}/tgbot.service" "${SERVICE_FILE}"

chown -R "${APP_NAME}:${APP_NAME}" "${APP_DIR}" "${STATE_DIR}"
chown root:root "${CONF_DIR}/tgbot.env" "${SERVICE_FILE}"
chmod 0600 "${CONF_DIR}/tgbot.env"

systemctl daemon-reload
systemctl enable tgbot >/dev/null

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} 安装完成（V2）${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "下一步："
echo "  1. 编辑配置：nano ${CONF_DIR}/tgbot.env"
echo "  2. 启动服务：systemctl start tgbot"
echo "  3. 查看状态：systemctl status tgbot"
echo "  4. 查看日志：journalctl -u tgbot -f"
echo ""
if command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg：已安装 ($(command -v ffmpeg))"
else
    echo "ffmpeg：未安装，视频无原始缩略图时将不会自动抽帧生成封面"
fi
