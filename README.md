# Telegram File Extractor Bot — LXC 轻量稳定版 V2 增强版

> 面向 **Proxmox LXC / Debian / Ubuntu** 的 Telegram 媒体提取机器人部署包。  
> 目标是：**轻量、稳定、低依赖、适合小规格 LXC 长期运行**。

---

## 这次增强了什么

在原先 V2 的基础上，这一版补上了三类你明确提出的能力：

### 1）相册 / 媒体组支持

现在支持处理公开频道 / 群组中的常见媒体组：

- 图片相册
- 视频相册
- 图片 + 视频混合相册（Telegram 常见媒体组）

处理策略：

1. **优先尝试直接按媒体组发送**，减少下载和重复上传
2. **如果源消息禁止转发**，或者直接发送失败：
   - 自动下载整组媒体
   - 优先尝试按媒体组重新发送
   - 如果媒体组发送失败，再自动回退为逐条发送

这样做的原因是：

- 先保留相册体验
- 但一旦 Telegram 对媒体组重发有限制，也不至于整组任务直接失败

> 说明：媒体组最稳定的支持对象是 **图片 / 视频**。如果遇到 Telegram 特殊分组类型，程序会优先保证“能提取出来”，必要时回退为逐条发送。

---

### 2）更稳的错误提示

增强后，机器人会把很多原本比较生硬的异常，转换成更容易理解的提示，例如：

- 链接里的频道用户名无效
- 频道是私有的或当前账号无权读取
- 消息 ID 无效
- Telegram 限流中，需要等待多少秒
- 本地磁盘空间不足
- 请求超时
- 本地目录权限不足

设计原则是：

- **日志里保留技术细节**，便于运维排错
- **用户看到的是简洁提示**，避免直接抛 Python 异常

---

### 3）自动清理策略

为了更适合 LXC 小容器，这一版增加了两层清理策略：

#### 第一层：任务级临时目录自动清理
每次下载 / 转发任务都会使用独立临时目录，任务结束后立即删除。

#### 第二层：后台周期性清理异常遗留文件
如果机器人在下载中途异常退出，可能留下残留临时文件。增强版会：

- 启动时先清理一次旧临时文件
- 运行过程中按周期扫描临时目录
- 删除超出保留时间的旧任务目录

另外，错误状态消息也支持自动延迟删除，减少聊天窗口堆积。

---

## 设计原则

这一版仍然坚持“轻量优先”：

- **Python 依赖只有 Telethon**
- **ffmpeg 仅作为系统二进制可选使用**
- **不引入 Pillow / OpenCV / 数据库 / Web 面板**
- **默认单任务并发**，更适合 1C / 256MB~512MB 的 LXC
- **自动清理只扫描机器人自己的临时目录**，避免高开销

---

## 目录结构

```text
lxc-deploy-V2/
├── bot.py
├── install.sh
├── README.md
├── requirements.txt
├── tgbot.env
├── tgbot.service
└── uninstall.sh
```

---

## 功能说明

### 支持的输入

- 公开频道 / 群组消息链接
- 支持格式：
  - `https://t.me/channel_name/123`
  - `https://t.me/s/channel_name/123`

### 支持的媒体类型

单条消息支持：

- 图片
- 视频
- 音频

媒体组 / 相册优先支持：

- 图片组
- 视频组
- 图片 + 视频混合组

### 输出行为

- 自动附加来源链接
- 尽量隐藏原始来源
- 能直发就直发，不能直发就下载重传
- 视频优先保留原始缩略图
- 没有可用缩略图时，可用 ffmpeg 本地抽帧

---

## 资源建议（适合 LXC）

### 推荐最小配置

| 项目 | 建议值 |
|------|--------|
| CPU | 1 核 |
| 内存 | 256MB 起，推荐 512MB |
| Swap | 512MB |
| 磁盘 | 6GB 起；若经常处理视频或媒体组，建议 8GB+ |
| 网络 | 可访问 Telegram API |

### 默认保守参数

默认配置偏保守，目标不是极限吞吐，而是 **让小 LXC 更稳**：

- `MAX_CONCURRENT_JOBS=1`
- `MAX_FILE_SIZE_MB=1024`
- `MIN_FREE_SPACE_MB=256`
- `MemoryMax=256M`
- `CPUQuota=50%`

---

## 快速部署

### 1）上传目录到容器

```bash
scp -r lxc-deploy-V2/ root@<容器IP>:/root/tgbot/
```

### 2）执行安装

```bash
cd /root/tgbot
chmod +x install.sh
bash install.sh
```

如果你暂时不想安装 ffmpeg：

```bash
INSTALL_FFMPEG=0 bash install.sh
```

> 不装 ffmpeg 也能运行；只是当视频没有原始缩略图时，不能本地抽帧生成封面。

### 3）填写配置

```bash
nano /etc/tgbot/tgbot.env
```

至少填写：

```env
BOT_TOKEN=123456789:ABCDEF...
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
```

### 4）启动服务

```bash
systemctl start tgbot
systemctl status tgbot
```

### 5）查看日志

```bash
journalctl -u tgbot -f
```

---

## 配置项说明

### 必填项

```env
BOT_TOKEN=
API_ID=
API_HASH=
```

### 常用项

```env
SESSION_DIR=/var/lib/tgbot/sessions
TEMP_DIR_BASE=/tmp/tgbot
MAX_FILE_SIZE_MB=1024
MAX_CONCURRENT_JOBS=1
MIN_FREE_SPACE_MB=256
ENABLE_FFMPEG_THUMB=1
FFMPEG_BIN=ffmpeg
FFMPEG_TIMEOUT_SEC=20
LOG_LEVEL=INFO
```

### 新增的增强配置

```env
# 拉取媒体组时，向前向后搜索的消息窗口
MEDIA_GROUP_WINDOW=12

# 临时文件保留时长（分钟）
TEMP_RETENTION_MINUTES=180

# 后台自动清理扫描周期（分钟）
CLEANUP_INTERVAL_MINUTES=30

# 错误状态消息自动删除时间（秒）
# 设为 0 表示不自动删除
AUTO_DELETE_ERROR_SEC=120
```

### 调参建议

- **超小 LXC**：保持默认即可
- **磁盘较小**：把 `MAX_FILE_SIZE_MB` 调低到 `512`
- **经常处理视频相册**：建议保留 `MIN_FREE_SPACE_MB=256` 或更高
- **临时目录想更独立**：可改 `TEMP_DIR_BASE=/tmp/tgbot`
- **不想自动删除错误消息**：设 `AUTO_DELETE_ERROR_SEC=0`
- **不需要 ffmpeg 抽帧**：设 `ENABLE_FFMPEG_THUMB=0`

---

## 自动清理策略说明

这版的自动清理主要清理的是：

- 下载中的临时文件
- 已完成任务留下的临时目录
- 异常中断后遗留的旧任务目录
- 过期错误状态消息（可配置）

不会清理：

- `SESSION_DIR` 会话文件
- `/etc/tgbot/tgbot.env` 配置文件
- systemd 日志

所以它属于：

> **只清理临时运行痕迹，不碰持久化配置和会话数据**

---

## systemd 服务说明

服务文件：

```text
/etc/systemd/system/tgbot.service
```

这一版继续保留这些适合 LXC 的限制：

- `User=tgbot`：低权限运行
- `MemoryMax=256M`：限制最大内存
- `CPUQuota=50%`：限制 CPU 占用
- `TasksMax=64`：限制进程/线程数量
- `PrivateTmp=true`：隔离临时目录
- `ProtectSystem=strict`：系统目录只读
- `ReadWritePaths=/var/lib/tgbot /tmp`：仅开放必要写入路径

> 如果你修改了 `SESSION_DIR` 或 `TEMP_DIR_BASE` 到别的目录，请同步检查 service 的 `ReadWritePaths`。

---

## 日常运维

```bash
# 查看实时日志
journalctl -u tgbot -f

# 重启
systemctl restart tgbot

# 停止
systemctl stop tgbot

# 查看状态
systemctl status tgbot
```

---

## 更新方式

如果你修改了程序：

```bash
cp bot.py /opt/tgbot/bot.py
chown tgbot:tgbot /opt/tgbot/bot.py
systemctl restart tgbot
```

如果你修改了环境变量：

```bash
nano /etc/tgbot/tgbot.env
systemctl restart tgbot
```

如果你修改了 service：

```bash
cp tgbot.service /etc/systemd/system/tgbot.service
systemctl daemon-reload
systemctl restart tgbot
```

---

## 卸载

```bash
bash /opt/tgbot/uninstall.sh
```

卸载脚本会：

- 停止服务
- 删除 `/opt/tgbot`
- 保留 `/var/lib/tgbot`
- 保留 `/etc/tgbot`

如需彻底删除：

```bash
rm -rf /var/lib/tgbot /etc/tgbot
```

---

## 当前限制

为了继续保持轻量，这一版仍然没有做一些会明显变重的能力：

- 不支持 `t.me/c/...` 私有链接
- 不引入数据库 / Web 后台 / 权限面板
- 不做复杂的任务队列系统
- 不做重型多媒体处理链路

也就是说，它的定位依然是：

> **一个适合小 LXC 长期运行、可维护、低依赖的 Telegram 媒体提取机器人**

---

## 依赖

Python 依赖只有：

```text
telethon==1.37.0
```

系统依赖：

- `python3`
- `python3-venv`
- `python3-pip`
- `ffmpeg`（可选，但推荐）

---

## 总结

这一版相对之前，重点是三件事：

- **补上相册 / 媒体组支持**
- **把错误提示做得更稳、更可读**
- **加入自动清理策略，减少临时文件堆积**

同时仍然尽量保持：

- 代码简单
- 依赖少
- 运维容易
- 对小规格 LXC 更友好
