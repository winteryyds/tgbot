# Telegram File Extractor Bot — LXC 部署方案

> 从 Docker 转换为原生 Linux 二进制部署，适用于 **Proxmox LXC 容器**（Debian/Ubuntu）。  
> 无需 Docker，资源占用更低，启动更快，与宿主机 kernel 直接交互。

---

## 目录结构

```
lxc-deploy/
├── bot.py            # 机器人主程序（已适配 SESSION_DIR 环境变量）
├── requirements.txt  # Python 依赖
├── tgbot.env         # 环境变量模板（需填写 Token）
├── tgbot.service     # systemd 服务单元
├── install.sh        # 一键安装脚本
└── uninstall.sh      # 卸载脚本
```

---

## 快速部署（推荐）

### 第一步：准备 LXC 容器

在 Proxmox 中创建 LXC 容器：

| 参数 | 推荐值 |
|------|--------|
| 模板 | Debian 12 / Ubuntu 22.04 |
| CPU | 1 核（软限制 50%） |
| 内存 | 256 MB（含 Swap 512MB 更稳） |
| 磁盘 | 4 GB（足够） |
| 网络 | 需要出网访问 Telegram API |
| 特权容器 | 否（非特权更安全） |

> **注意**：不需要开启 "嵌套虚拟化" 或任何特殊功能，普通非特权 LXC 即可。

### 第二步：上传文件到 LXC 容器

在宿主机（Proxmox）上执行，将 `lxc-deploy` 目录上传到容器（假设容器 ID 为 100）：

```bash
# 方法 A：通过 pct push（Proxmox 原生）
pct push 100 bot.py /root/tgbot/bot.py
pct push 100 requirements.txt /root/tgbot/requirements.txt
pct push 100 tgbot.env /root/tgbot/tgbot.env
pct push 100 tgbot.service /root/tgbot/tgbot.service
pct push 100 install.sh /root/tgbot/install.sh

# 方法 B：通过 SCP（容器已开启 SSH）
scp -r lxc-deploy/ root@<容器IP>:/root/tgbot/
```

### 第三步：进入容器并安装

```bash
# 进入容器
pct enter 100
# 或通过 SSH
ssh root@<容器IP>

# 执行安装
cd /root/tgbot
chmod +x install.sh
bash install.sh
```

安装脚本会自动完成：
- 安装 Python 3 + venv
- 创建系统用户 `tgbot`（无 shell，更安全）
- 创建 `/opt/tgbot`、`/var/lib/tgbot/sessions`、`/etc/tgbot` 目录
- 安装 Python 依赖到虚拟环境
- 注册并启用 systemd 服务

### 第四步：填写配置

```bash
nano /etc/tgbot/tgbot.env
```

填写以下三项（必填）：

```env
BOT_TOKEN=123456789:ABCdef...   # 从 @BotFather 获取
API_ID=12345678                  # 从 https://my.telegram.org/apps 获取
API_HASH=abcdef1234567890...     # 同上
```

### 第五步：启动服务

```bash
systemctl start tgbot
systemctl status tgbot
```

---

## 日常运维命令

```bash
# 查看实时日志
journalctl -u tgbot -f

# 查看最近 100 行日志
journalctl -u tgbot -n 100

# 重启服务
systemctl restart tgbot

# 停止服务
systemctl stop tgbot

# 查看资源占用
systemctl status tgbot
# 或
ps aux | grep bot.py
```

---

## 目录说明

| 路径 | 用途 |
|------|------|
| `/opt/tgbot/` | 程序文件 + Python 虚拟环境 |
| `/opt/tgbot/venv/` | Python 虚拟环境（隔离依赖） |
| `/var/lib/tgbot/sessions/` | Telethon 会话文件（持久化） |
| `/etc/tgbot/tgbot.env` | 环境变量配置（权限 600） |
| `/etc/systemd/system/tgbot.service` | systemd 服务单元 |

---

## 资源占用对比

| 指标 | Docker 方案 | LXC 原生方案 |
|------|-------------|-------------|
| 内存基础占用 | ~80MB（Docker daemon） + 进程 | 仅进程本身 ~30MB |
| 启动时间 | 10~30 秒 | <2 秒 |
| 磁盘占用 | ~200MB（镜像） | ~50MB（venv） |
| CPU 开销 | 有容器化损耗 | 接近原生 |
| 自动重启 | docker restart policy | systemd Restart=on-failure |

---

## 安全特性

systemd 服务已启用以下沙箱保护：

- `NoNewPrivileges=true` — 禁止提权
- `PrivateTmp=true` — 独立 /tmp 命名空间
- `ProtectSystem=strict` — 系统目录只读
- `ProtectHome=true` — 禁止访问 /home
- `User=tgbot` — 以低权限用户运行
- `MemoryMax=256M` — 内存上限（同 Docker mem_limit）
- `CPUQuota=50%` — CPU 限制（同 Docker cpus: 0.5）

---

## 更新机器人

```bash
# 1. 上传新的 bot.py
scp bot.py root@<容器IP>:/opt/tgbot/bot.py

# 2. 修正文件权限
chown tgbot:tgbot /opt/tgbot/bot.py

# 3. 重启服务
systemctl restart tgbot
```

---

## 卸载

```bash
bash /root/tgbot/uninstall.sh
# 如需彻底删除数据：
rm -rf /var/lib/tgbot /etc/tgbot
```

---

## 常见问题

**Q: 提示 `python3-venv` 找不到？**  
A: 手动运行 `apt-get install -y python3-venv` 后重新执行安装脚本。

**Q: 服务启动失败，日志提示 `BOT_TOKEN` 未设置？**  
A: 检查 `/etc/tgbot/tgbot.env` 是否已填写真实值，注意不要有多余空格。

**Q: 会话文件在哪里？重装后还能用吗？**  
A: 会话文件在 `/var/lib/tgbot/sessions/bot_session.session`，卸载脚本不会删除它，重装后自动复用。

**Q: 如何在多个 LXC 容器中运行多个 bot？**  
A: 每个 bot 建一个独立 LXC 容器，或修改 `tgbot.service` 的 `User`、`WorkingDirectory` 和端口，为每个 bot 创建独立的 service 文件。

---

## 获取 API_ID 和 API_HASH

1. 访问 [https://my.telegram.org/apps](https://my.telegram.org/apps)
2. 登录你的 Telegram 账号
3. 点击「API development tools」
4. 创建应用（名称随意）
5. 复制 `App api_id` 和 `App api_hash`
