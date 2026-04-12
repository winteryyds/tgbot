"""
Telegram File Extractor Bot — LXC 轻量稳定版 V2 增强版

增强目标：
1. 保持轻量：仅依赖 Telethon + 系统 ffmpeg（二进制，可选）
2. 支持公开频道/群组的相册 / 媒体组（优先支持图片、视频相册）
3. 错误提示更稳：尽量把 Telegram / 网络 / 磁盘类问题转成可读提示
4. 自动清理更稳：任务结束即清理临时目录，且后台周期性清理异常遗留文件
5. 继续适配 Proxmox / Debian / Ubuntu 的小规格 LXC 容器

轻量化策略：
- 默认并发仅 1 个任务，降低 CPU / 内存峰值
- 相册下载与上传按顺序执行，避免一次性把大量媒体压进内存
- ffmpeg 仅在“已经走下载流程”且“原消息没有可用缩略图”时调用
- ffmpeg 固定单线程，不额外引入 Pillow / OpenCV 等重型 Python 依赖
- 自动清理只扫描机器人自己的临时目录，避免额外系统负担
"""

import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError, RPCError
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
    PhotoCachedSize,
    PhotoSize,
    PhotoSizeProgressive,
    PhotoStrippedSize,
)


TRUE_VALUES = {"1", "true", "yes", "on"}
PLACEHOLDER_ENV_VALUES = {
    "BOT_TOKEN": {"your_bot_token_here"},
    "API_HASH": {"your_api_hash_here"},
}
PUBLIC_LINK_PATTERN = re.compile(
    r"https?://(?:t|telegram)\.me/(?:s/)?(?P<chat>[A-Za-z0-9_]+)/(?P<msg_id>\d+)(?:/?(?:\?.*)?)?",
    re.IGNORECASE,
)
PRIVATE_LINK_PATTERN = re.compile(
    r"https?://(?:t|telegram)\.me/c/\d+/\d+(?:/?(?:\?.*)?)?",
    re.IGNORECASE,
)
SUPPORTED_MEDIA_TYPES = {"photo", "video", "audio"}
ALBUM_COMPATIBLE_MEDIA_TYPES = {"photo", "video"}
CAPTION_TEXT_LIMIT = 850
MAX_MEDIA_GROUP_ITEMS = 10
DEFAULT_NOFORWARDS_MAX_FILE_SIZE_MB = 2048
DEFAULT_MAX_CONCURRENT_JOBS = 1
DEFAULT_FFMPEG_TIMEOUT_SEC = 20
DEFAULT_MIN_FREE_SPACE_MB = 256
DEFAULT_MEDIA_GROUP_WINDOW = 12
DEFAULT_TEMP_RETENTION_MINUTES = 180
DEFAULT_CLEANUP_INTERVAL_MINUTES = 30
DEFAULT_AUTO_DELETE_ERROR_SEC = 120


LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("tgbot")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    session_dir: str
    temp_dir_base: str
    noforwards_max_file_size_bytes: int
    max_concurrent_jobs: int
    min_free_space_bytes: int
    media_group_window: int
    temp_retention_minutes: int
    cleanup_interval_minutes: int
    auto_delete_error_sec: int
    enable_ffmpeg_thumb: bool
    ffmpeg_bin: str
    ffmpeg_timeout_sec: int

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = require_env("BOT_TOKEN")
        api_id = env_int("API_ID", 0)
        if api_id <= 0:
            raise RuntimeError("环境变量 API_ID 必须是正整数")

        api_hash = require_env("API_HASH")
        session_dir = os.environ.get("SESSION_DIR", "/var/lib/tgbot/sessions").strip() or "/var/lib/tgbot/sessions"
        temp_dir_base = os.environ.get("TEMP_DIR_BASE", "/tmp/tgbot").strip() or "/tmp/tgbot"
        noforwards_max_file_size_mb = max(1, env_int("NOFORWARDS_MAX_FILE_SIZE_MB", DEFAULT_NOFORWARDS_MAX_FILE_SIZE_MB))
        max_concurrent_jobs = max(1, env_int("MAX_CONCURRENT_JOBS", DEFAULT_MAX_CONCURRENT_JOBS))
        min_free_space_mb = max(64, env_int("MIN_FREE_SPACE_MB", DEFAULT_MIN_FREE_SPACE_MB))
        media_group_window = max(4, env_int("MEDIA_GROUP_WINDOW", DEFAULT_MEDIA_GROUP_WINDOW))
        temp_retention_minutes = max(30, env_int("TEMP_RETENTION_MINUTES", DEFAULT_TEMP_RETENTION_MINUTES))
        cleanup_interval_minutes = max(10, env_int("CLEANUP_INTERVAL_MINUTES", DEFAULT_CLEANUP_INTERVAL_MINUTES))
        auto_delete_error_sec = max(0, env_int("AUTO_DELETE_ERROR_SEC", DEFAULT_AUTO_DELETE_ERROR_SEC))
        enable_ffmpeg_thumb = env_bool("ENABLE_FFMPEG_THUMB", True)
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg"
        ffmpeg_timeout_sec = max(5, env_int("FFMPEG_TIMEOUT_SEC", DEFAULT_FFMPEG_TIMEOUT_SEC))

        return cls(
            bot_token=bot_token,
            api_id=api_id,
            api_hash=api_hash,
            session_dir=session_dir,
            temp_dir_base=temp_dir_base,
            noforwards_max_file_size_bytes=noforwards_max_file_size_mb * 1024 * 1024,
            max_concurrent_jobs=max_concurrent_jobs,
            min_free_space_bytes=min_free_space_mb * 1024 * 1024,
            media_group_window=media_group_window,
            temp_retention_minutes=temp_retention_minutes,
            cleanup_interval_minutes=cleanup_interval_minutes,
            auto_delete_error_sec=auto_delete_error_sec,
            enable_ffmpeg_thumb=enable_ffmpeg_thumb,
            ffmpeg_bin=ffmpeg_bin,
            ffmpeg_timeout_sec=ffmpeg_timeout_sec,
        )


@dataclass
class MediaBatch:
    items: list
    is_group: bool
    skipped_unsupported: int = 0

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass
class DownloadedMedia:
    message: object
    media_type: str
    local_path: str
    size_bytes: int


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必填环境变量：{name}")

    if value.lower() in PLACEHOLDER_ENV_VALUES.get(name, set()):
        raise RuntimeError(f"环境变量 {name} 仍然是示例占位值，请填写真实配置")

    return value


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数，当前值：{raw}") from exc


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw.lower() in TRUE_VALUES


SETTINGS = Settings.from_env()
SESSION_ROOT = Path(SETTINGS.session_dir)
TEMP_ROOT = Path(SETTINGS.temp_dir_base)
SESSION_ROOT.mkdir(parents=True, exist_ok=True)
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
SESSION_PATH = str(SESSION_ROOT / "bot_session")

bot = TelegramClient(
    SESSION_PATH,
    SETTINGS.api_id,
    SETTINGS.api_hash,
    connection_retries=5,
    request_retries=2,
    retry_delay=2,
    auto_reconnect=True,
    flood_sleep_threshold=60,
)
JOB_SEMAPHORE = asyncio.Semaphore(SETTINGS.max_concurrent_jobs)
BACKGROUND_TASKS = set()

if os.environ.get("MAX_FILE_SIZE_MB", "").strip():
    logger.warning(
        "检测到旧配置 MAX_FILE_SIZE_MB：当前版本已不再对允许转发的资源做文件大小限制；"
        "仅对禁止转发、必须下载重传的资源应用 NOFORWARDS_MAX_FILE_SIZE_MB。"
    )


def track_task(task):
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(lambda finished: BACKGROUND_TASKS.discard(finished))
    return task


def extract_public_link(text: str) -> Optional[tuple[str, int]]:
    match = PUBLIC_LINK_PATTERN.search(text or "")
    if not match:
        return None

    chat = match.group("chat")
    if chat.lower() == "c":
        return None

    return chat, int(match.group("msg_id"))


def get_media_type(message) -> Optional[str]:
    media = getattr(message, "media", None)
    if media is None:
        return None

    if isinstance(media, MessageMediaPhoto):
        return "photo"

    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc is None:
            return None

        mime = doc.mime_type or ""
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                return "video"
            if isinstance(attr, DocumentAttributeAudio):
                return "audio"

        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("image/"):
            return "photo"

    return None


def get_remote_file_size(message) -> Optional[int]:
    file_obj = getattr(message, "file", None)
    size = getattr(file_obj, "size", None)
    if size:
        return int(size)

    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is not None and getattr(document, "size", None):
        return int(document.size)

    return None


def get_upload_attributes(message, media_type: str) -> Optional[list]:
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is None:
        return None

    kept = []
    for attr in document.attributes:
        if media_type == "video" and isinstance(attr, (DocumentAttributeVideo, DocumentAttributeFilename)):
            kept.append(attr)
        elif media_type == "audio" and isinstance(attr, (DocumentAttributeAudio, DocumentAttributeFilename)):
            kept.append(attr)

    return kept or None


def get_original_filename(message, media_type: str) -> str:
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is not None:
        for attr in document.attributes:
            if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                return sanitize_filename(attr.file_name, fallback_name(media_type))

    return fallback_name(media_type)


def fallback_name(media_type: str) -> str:
    ext_map = {
        "photo": "file.jpg",
        "video": "file.mp4",
        "audio": "file.mp3",
    }
    return ext_map.get(media_type, "file.bin")


def sanitize_filename(filename: str, default_name: str) -> str:
    cleaned = os.path.basename((filename or "").strip())
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", cleaned)
    cleaned = cleaned.strip(" .")

    if not cleaned or cleaned in {".", ".."}:
        cleaned = default_name

    stem, ext = os.path.splitext(cleaned)
    stem = (stem or "file")[:100].rstrip(" .") or "file"
    ext = ext[:16]
    return f"{stem}{ext}" if ext else stem


def build_caption(original_text: str, source_link: str) -> str:
    footer = f'📎 <a href="{html.escape(source_link, quote=True)}">来源链接</a>'
    text = (original_text or "").strip()
    if not text:
        return footer

    safe_text = html.escape(text)
    if len(safe_text) > CAPTION_TEXT_LIMIT:
        safe_text = safe_text[:CAPTION_TEXT_LIMIT].rstrip() + "..."

    return f"{safe_text}\n\n{footer}"


def build_caption_payload(caption: str, count: int):
    if count <= 1:
        return caption
    return [caption] + [""] * (count - 1)


def get_batch_text(messages: Sequence) -> str:
    for message in messages:
        text = (getattr(message, "message", None) or getattr(message, "raw_text", None) or "").strip()
        if text:
            return text
    return ""


def format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size_bytes}B"


def has_enough_free_space(path: str, expected_file_bytes: int) -> tuple[bool, int]:
    usage = shutil.disk_usage(path)
    required = expected_file_bytes + SETTINGS.min_free_space_bytes
    return usage.free >= required, usage.free


def ensure_enough_free_space(path: str, expected_file_bytes: Optional[int]) -> None:
    if not expected_file_bytes or expected_file_bytes <= 0:
        return

    enough, free_bytes = has_enough_free_space(path, expected_file_bytes)
    if not enough:
        raise RuntimeError(
            f"磁盘剩余空间不足，当前可用 {format_size(free_bytes)}，"
            f"至少需要 {format_size(expected_file_bytes + SETTINGS.min_free_space_bytes)}。"
        )


def batch_has_video(messages: Sequence) -> bool:
    return any(get_media_type(message) == "video" for message in messages)


def is_album_eligible(batch: MediaBatch) -> bool:
    return batch.count > 1 and all(get_media_type(message) in ALBUM_COMPATIBLE_MEDIA_TYPES for message in batch.items)


def estimate_batch_total_size(messages: Sequence) -> tuple[int, bool]:
    total = 0
    has_unknown = False
    for message in messages:
        size = get_remote_file_size(message)
        if size is None:
            has_unknown = True
            continue
        total += size
    return total, has_unknown


def get_reupload_size_limit_bytes(no_forwards: bool) -> Optional[int]:
    if not no_forwards:
        return None
    return SETTINGS.noforwards_max_file_size_bytes


def validate_batch_size_limit(batch: MediaBatch, size_limit_bytes: Optional[int]) -> None:
    if not size_limit_bytes or size_limit_bytes <= 0:
        return

    for index, message in enumerate(batch.items, start=1):
        size = get_remote_file_size(message)
        if size is None:
            continue
        if size > size_limit_bytes:
            if batch.count > 1:
                raise RuntimeError(
                    f"媒体组第 {index} 项太大（{format_size(size)}），超过下载重传限制 {format_size(size_limit_bytes)}。"
                )
            raise RuntimeError(
                f"文件太大（{format_size(size)}），超过下载重传限制 {format_size(size_limit_bytes)}。"
            )


def pick_best_thumb(thumbs: Sequence) -> Optional[object]:
    best_thumb = None
    best_size = -1

    for thumb in thumbs or []:
        if isinstance(thumb, PhotoStrippedSize):
            continue
        if isinstance(thumb, PhotoSizeProgressive):
            current_size = max(thumb.sizes or [0])
        elif isinstance(thumb, PhotoCachedSize):
            current_size = len(thumb.bytes or b"")
        elif isinstance(thumb, PhotoSize):
            current_size = thumb.size or 0
        else:
            continue

        if current_size > best_size:
            best_size = current_size
            best_thumb = thumb

    return best_thumb


async def download_message_thumb(message, tmpdir: str) -> Optional[str]:
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is None or not getattr(document, "thumbs", None):
        return None

    best_thumb = pick_best_thumb(document.thumbs)
    if best_thumb is None:
        return None

    thumb_path = os.path.join(tmpdir, f"thumb_{getattr(message, 'id', 'item')}.jpg")

    try:
        if isinstance(best_thumb, PhotoCachedSize) and best_thumb.bytes:
            with open(thumb_path, "wb") as fp:
                fp.write(best_thumb.bytes)
        else:
            downloaded_path = await bot.download_media(message, file=thumb_path, thumb=best_thumb)
            if downloaded_path:
                thumb_path = os.fspath(downloaded_path)

        if os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as exc:
        logger.warning("下载原始缩略图失败：%s", exc)

    return None


async def run_ffmpeg_thumbnail(video_path: str, thumb_path: str, seek_position: str) -> bool:
    ffmpeg_path = shutil.which(SETTINGS.ffmpeg_bin)
    if not ffmpeg_path:
        return False

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        seek_position,
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-threads",
        "1",
        "-vf",
        "scale=320:320:force_original_aspect_ratio=decrease",
        "-q:v",
        "7",
        thumb_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=SETTINGS.ffmpeg_timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.warning("ffmpeg 抽帧超时，已跳过：%s", video_path)
        return False

    if proc.returncode != 0:
        error_text = (stderr or b"").decode(errors="ignore").strip()
        if error_text:
            logger.debug("ffmpeg 执行失败（%s）：%s", seek_position, error_text)
        return False

    return os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0


async def extract_video_thumb_with_ffmpeg(video_path: str, tmpdir: str) -> Optional[str]:
    if not SETTINGS.enable_ffmpeg_thumb:
        return None

    if not shutil.which(SETTINGS.ffmpeg_bin):
        logger.debug("系统未找到 ffmpeg，跳过本地抽帧")
        return None

    thumb_path = os.path.join(tmpdir, f"ffmpeg_thumb_{os.path.basename(video_path)}.jpg")
    for seek in ("00:00:01", "00:00:00"):
        try:
            if await run_ffmpeg_thumbnail(video_path, thumb_path, seek):
                logger.info("已通过 ffmpeg 提取视频封面：%s", os.path.basename(video_path))
                return thumb_path
        except Exception as exc:
            logger.warning("ffmpeg 抽帧失败：%s", exc)
            break

    return None


async def get_thumb_for_upload(message, media_type: str, tmpdir: str, local_file_path: Optional[str] = None) -> Optional[str]:
    thumb_path = await download_message_thumb(message, tmpdir)
    if thumb_path:
        return thumb_path

    if media_type == "video" and local_file_path:
        return await extract_video_thumb_with_ffmpeg(local_file_path, tmpdir)

    return None


def cleanup_stale_temp_entries() -> int:
    if not TEMP_ROOT.exists():
        return 0

    now = time.time()
    max_age_seconds = SETTINGS.temp_retention_minutes * 60
    removed = 0

    for entry in TEMP_ROOT.iterdir():
        try:
            age = now - entry.stat().st_mtime
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.debug("读取临时项信息失败：%s", exc)
            continue

        if age < max_age_seconds:
            continue

        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("清理临时项失败：%s (%s)", entry, exc)

    return removed


async def periodic_cleanup_loop() -> None:
    while True:
        try:
            removed = cleanup_stale_temp_entries()
            if removed:
                logger.info("自动清理完成，移除 %s 个过期临时项", removed)
        except Exception as exc:
            logger.warning("自动清理任务执行失败：%s", exc)

        await asyncio.sleep(SETTINGS.cleanup_interval_minutes * 60)


async def safe_edit(message, text: str) -> None:
    try:
        await message.edit(text)
    except MessageNotModifiedError:
        pass
    except Exception as exc:
        logger.debug("编辑状态消息失败：%s", exc)


async def safe_delete(message) -> None:
    try:
        await message.delete()
    except Exception as exc:
        logger.debug("删除消息失败：%s", exc)


async def delayed_delete(message, delay_sec: int) -> None:
    if delay_sec <= 0:
        return
    await asyncio.sleep(delay_sec)
    await safe_delete(message)


def schedule_delete(message, delay_sec: int) -> None:
    if delay_sec <= 0:
        return
    try:
        track_task(asyncio.create_task(delayed_delete(message, delay_sec)))
    except RuntimeError:
        logger.debug("当前没有可用事件循环，跳过延迟删除任务")


async def show_error(status_msg, text: str) -> None:
    text = text.strip() or "发生未知错误，请稍后重试。"
    if not text.startswith("❌"):
        text = f"❌ {text}"
    await safe_edit(status_msg, text)
    schedule_delete(status_msg, SETTINGS.auto_delete_error_sec)


def friendly_exception_message(exc: Exception, fallback: str = "处理失败，请稍后重试。") -> str:
    if isinstance(exc, RuntimeError):
        return str(exc)

    if isinstance(exc, FloodWaitError):
        wait_seconds = getattr(exc, "seconds", None) or 0
        return f"Telegram 限流中，请等待 {wait_seconds} 秒后再试。"

    if isinstance(exc, asyncio.TimeoutError):
        return "请求超时，请稍后重试。"

    if isinstance(exc, PermissionError):
        return "权限不足，无法写入临时目录或目标目录。"

    if isinstance(exc, OSError):
        return "系统 I/O 出错，请检查磁盘空间、目录权限或稍后重试。"

    name = exc.__class__.__name__
    if name in {"UsernameInvalidError", "UsernameNotOccupiedError"}:
        return "链接中的公开频道/群组用户名无效，或该用户名不存在。"
    if name == "ChannelPrivateError":
        return "该频道/群组不可访问，可能是私有、已删除，或当前账号无权读取。"
    if name == "ChatAdminRequiredError":
        return "当前账号权限不足，无法读取该消息或媒体。"
    if name in {"MessageIdInvalidError", "MsgIdInvalidError"}:
        return "消息 ID 无效，请检查链接是否正确。"
    if name == "FileReferenceExpiredError":
        return "源媒体引用已过期，请稍后再试一次。"
    if name in {"MediaEmptyError", "MediaInvalidError"}:
        return "媒体不可用，或 Telegram 拒绝了当前发送方式。"
    if name == "PhotoInvalidDimensionsError":
        return "图片尺寸不被 Telegram 接受，无法重新发送。"
    if name == "SlowModeWaitError":
        wait_seconds = getattr(exc, "seconds", None) or 0
        return f"当前会话触发了慢速模式，请等待 {wait_seconds} 秒后再试。"

    if isinstance(exc, RPCError):
        return f"Telegram API 返回错误：{name}"

    return fallback


async def try_direct_resend_single(target_entity, message, media_type: str, caption: str) -> bool:
    try:
        send_kwargs = {
            "entity": target_entity,
            "file": message.media,
            "caption": caption,
            "parse_mode": "html",
            "force_document": False,
        }
        if media_type == "video":
            send_kwargs["supports_streaming"] = True

        await bot.send_file(**send_kwargs)
        return True
    except Exception as exc:
        logger.warning("[DIRECT] 直发单条媒体失败，将回退到下载重传：%s", exc)
        return False


async def try_direct_resend_batch(target_entity, batch: MediaBatch, caption: str) -> bool:
    if batch.count == 1:
        media_type = get_media_type(batch.items[0])
        if media_type is None:
            return False
        return await try_direct_resend_single(target_entity, batch.items[0], media_type, caption)

    if not is_album_eligible(batch):
        return False

    try:
        send_kwargs = {
            "entity": target_entity,
            "file": [message.media for message in batch.items],
            "caption": build_caption_payload(caption, batch.count),
            "parse_mode": "html",
            "force_document": False,
        }
        if batch_has_video(batch.items):
            send_kwargs["supports_streaming"] = True

        await bot.send_file(**send_kwargs)
        return True
    except Exception as exc:
        logger.warning("[DIRECT-GROUP] 直发媒体组失败，将回退到下载重传：%s", exc)
        return False


async def resolve_media_batch(entity, msg_id: int) -> MediaBatch:
    anchor_message = await bot.get_messages(entity, ids=msg_id)
    if anchor_message is None:
        return MediaBatch(items=[], is_group=False, skipped_unsupported=0)

    grouped_id = getattr(anchor_message, "grouped_id", None)
    anchor_media_type = get_media_type(anchor_message)
    if not grouped_id:
        if anchor_media_type is None:
            return MediaBatch(items=[anchor_message], is_group=False, skipped_unsupported=0)
        return MediaBatch(items=[anchor_message], is_group=False, skipped_unsupported=0)

    start_id = max(1, msg_id - SETTINGS.media_group_window)
    end_id = msg_id + SETTINGS.media_group_window
    nearby_messages = await bot.get_messages(entity, ids=list(range(start_id, end_id + 1)))

    grouped_messages = []
    skipped_unsupported = 0
    for message in nearby_messages or []:
        if message is None or getattr(message, "grouped_id", None) != grouped_id:
            continue
        media_type = get_media_type(message)
        if media_type is None:
            skipped_unsupported += 1
            continue
        grouped_messages.append(message)

    grouped_messages.sort(key=lambda item: item.id)
    grouped_messages = grouped_messages[:MAX_MEDIA_GROUP_ITEMS]

    if grouped_messages:
        return MediaBatch(items=grouped_messages, is_group=len(grouped_messages) > 1, skipped_unsupported=skipped_unsupported)

    return MediaBatch(items=[anchor_message], is_group=False, skipped_unsupported=skipped_unsupported)


async def download_single_item(
    status_msg,
    message,
    media_type: str,
    tmpdir: str,
    index: int,
    total: int,
    size_limit_bytes: Optional[int],
) -> DownloadedMedia:
    filename = get_original_filename(message, media_type)
    if total > 1:
        filename = f"{index:02d}_{filename}"

    local_path = os.path.join(tmpdir, filename)
    if total > 1:
        await safe_edit(status_msg, f"⬇️ 正在下载 [{index}/{total}]：{filename}")
    else:
        await safe_edit(status_msg, f"⬇️ 正在下载：{filename}")

    downloaded_path = await bot.download_media(message, file=local_path)
    if not downloaded_path:
        raise RuntimeError("下载失败，未获得本地文件。")

    downloaded_path = os.fspath(downloaded_path)
    if not os.path.isfile(downloaded_path):
        raise RuntimeError("下载失败，本地文件不存在。")

    file_size = os.path.getsize(downloaded_path)
    if file_size <= 0:
        raise RuntimeError("下载失败，文件大小为 0。")

    if size_limit_bytes and file_size > size_limit_bytes:
        raise RuntimeError(
            f"文件太大（{format_size(file_size)}），超过下载重传限制 {format_size(size_limit_bytes)}。"
        )

    return DownloadedMedia(
        message=message,
        media_type=media_type,
        local_path=downloaded_path,
        size_bytes=file_size,
    )


async def send_downloaded_single(target_entity, status_msg, item: DownloadedMedia, caption: str, tmpdir: str) -> None:
    thumb_path = None
    if item.media_type in {"video", "audio"}:
        thumb_path = await get_thumb_for_upload(
            message=item.message,
            media_type=item.media_type,
            tmpdir=tmpdir,
            local_file_path=item.local_path,
        )

    display_name = os.path.basename(item.local_path)
    await safe_edit(status_msg, f"📤 正在发送：{display_name}（{format_size(item.size_bytes)}）")

    send_kwargs = {
        "entity": target_entity,
        "file": item.local_path,
        "caption": caption,
        "parse_mode": "html",
        "force_document": False,
    }

    attributes = get_upload_attributes(item.message, item.media_type)
    if attributes:
        send_kwargs["attributes"] = attributes
    if thumb_path:
        send_kwargs["thumb"] = thumb_path
    if item.media_type == "video":
        send_kwargs["supports_streaming"] = True

    await bot.send_file(**send_kwargs)


async def send_downloaded_album(target_entity, status_msg, items: Sequence[DownloadedMedia], caption: str) -> bool:
    total_size = sum(item.size_bytes for item in items)
    await safe_edit(status_msg, f"📤 正在发送媒体组：{len(items)} 项（共 {format_size(total_size)}）")

    try:
        send_kwargs = {
            "entity": target_entity,
            "file": [item.local_path for item in items],
            "caption": build_caption_payload(caption, len(items)),
            "parse_mode": "html",
            "force_document": False,
        }
        if any(item.media_type == "video" for item in items):
            send_kwargs["supports_streaming"] = True

        await bot.send_file(**send_kwargs)
        return True
    except Exception as exc:
        logger.warning("媒体组上传失败，将回退为逐条发送：%s", exc)
        return False


async def send_downloaded_items_individually(target_entity, status_msg, items: Sequence[DownloadedMedia], caption: str, tmpdir: str) -> None:
    for index, item in enumerate(items, start=1):
        item_caption = caption if index == 1 else ""
        await send_downloaded_single(target_entity, status_msg, item, item_caption, tmpdir)


async def send_preface_message(target_entity, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    await bot.send_message(target_entity, text, parse_mode="html", link_preview=False)


def cleanup_job_temp_files(tmpdir: str) -> None:
    tmp_path = Path(tmpdir)
    if not tmp_path.exists():
        return

    for entry in tmp_path.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.debug("清理任务临时文件失败：%s (%s)", entry, exc)


async def download_and_send_group_sequentially(
    target_entity,
    status_msg,
    batch: MediaBatch,
    preface_text: str,
    size_limit_bytes: Optional[int],
) -> None:
    with tempfile.TemporaryDirectory(prefix="job_", dir=SETTINGS.temp_dir_base) as tmpdir:
        await safe_edit(status_msg, "📝 正在发送当前媒体组说明...")
        await send_preface_message(target_entity, preface_text)

        for index, message in enumerate(batch.items, start=1):
            media_type = get_media_type(message)
            if media_type not in SUPPORTED_MEDIA_TYPES:
                raise RuntimeError("媒体组中存在暂不支持的项目，无法继续处理。")

            ensure_enough_free_space(tmpdir, get_remote_file_size(message))
            item = await download_single_item(
                status_msg,
                message,
                media_type,
                tmpdir,
                index=index,
                total=batch.count,
                size_limit_bytes=size_limit_bytes,
            )

            try:
                await send_downloaded_single(target_entity, status_msg, item, "", tmpdir)
            finally:
                cleanup_job_temp_files(tmpdir)


async def download_and_reupload_batch(
    target_entity,
    status_msg,
    batch: MediaBatch,
    caption: str,
    size_limit_bytes: Optional[int],
) -> None:
    with tempfile.TemporaryDirectory(prefix="job_", dir=SETTINGS.temp_dir_base) as tmpdir:
        estimated_total, has_unknown_size = estimate_batch_total_size(batch.items)
        if estimated_total > 0 and not has_unknown_size:
            ensure_enough_free_space(tmpdir, estimated_total)

        downloaded_items = []
        for index, message in enumerate(batch.items, start=1):
            media_type = get_media_type(message)
            if media_type not in SUPPORTED_MEDIA_TYPES:
                raise RuntimeError("媒体组中存在暂不支持的项目，无法继续处理。")
            downloaded_items.append(
                await download_single_item(
                    status_msg,
                    message,
                    media_type,
                    tmpdir,
                    index=index,
                    total=batch.count,
                    size_limit_bytes=size_limit_bytes,
                )
            )

        if len(downloaded_items) == 1:
            await send_downloaded_single(target_entity, status_msg, downloaded_items[0], caption, tmpdir)
            return

        if is_album_eligible(batch):
            album_ok = await send_downloaded_album(target_entity, status_msg, downloaded_items, caption)
            if album_ok:
                return

        await send_downloaded_items_individually(target_entity, status_msg, downloaded_items, caption, tmpdir)


def build_group_notice(batch: MediaBatch, sequential_reupload: bool = False) -> str:
    if sequential_reupload:
        base = f"🧩 检测到禁止转发媒体组，共 {batch.count} 项，将先发送说明，再按顺序逐条下载并发送。"
    else:
        base = f"🧩 检测到媒体组，共 {batch.count} 项，正在处理..."

    if batch.skipped_unsupported > 0:
        base += f"\n⚠️ 其中 {batch.skipped_unsupported} 项不是当前支持的媒体，已自动跳过。"
    return base


async def process_public_link(event, chat_username: str, msg_id: int) -> None:
    source_link = f"https://t.me/{chat_username}/{msg_id}"
    target_entity = event.chat_id or event.sender_id
    if target_entity is None:
        await event.respond("❌ 无法识别当前会话。")
        return

    status_msg = await event.respond("⏳ 正在排队处理...")

    async with JOB_SEMAPHORE:
        try:
            await safe_edit(status_msg, "⏳ 正在处理...")

            try:
                entity = await bot.get_entity(chat_username)
            except Exception as exc:
                await show_error(status_msg, friendly_exception_message(exc, f"找不到公开频道/群组 @{chat_username}。"))
                return

            try:
                batch = await resolve_media_batch(entity, msg_id)
            except Exception as exc:
                await show_error(status_msg, friendly_exception_message(exc, "获取消息失败，请稍后重试。"))
                return

            if not batch.items:
                await show_error(status_msg, "找不到该消息，请检查链接是否正确。")
                return

            if all(get_media_type(message) is None for message in batch.items):
                await show_error(
                    status_msg,
                    "该消息不包含支持的媒体。当前支持：图片、视频、音频；媒体组优先支持图片/视频。",
                )
                return

            no_forwards = bool(
                getattr(entity, "noforwards", False)
                or any(bool(getattr(message, "noforwards", False)) for message in batch.items)
            )
            size_limit_bytes = get_reupload_size_limit_bytes(no_forwards)
            validate_batch_size_limit(batch, size_limit_bytes)

            if batch.is_group:
                await safe_edit(status_msg, build_group_notice(batch, sequential_reupload=no_forwards))

            original_text = get_batch_text(batch.items)
            caption = build_caption(original_text, source_link)

            logger.info(
                "处理请求 chat=@%s msg_id=%s items=%s media_group=%s noforwards=%s reupload_limit=%s",
                chat_username,
                msg_id,
                batch.count,
                batch.is_group,
                no_forwards,
                format_size(size_limit_bytes) if size_limit_bytes else "none",
            )

            if not no_forwards:
                direct_ok = await try_direct_resend_batch(target_entity, batch, caption)
                if direct_ok:
                    await safe_delete(status_msg)
                    logger.info("处理完成（直发） chat=@%s msg_id=%s items=%s", chat_username, msg_id, batch.count)
                    return

            if no_forwards and batch.is_group:
                await download_and_send_group_sequentially(target_entity, status_msg, batch, caption, size_limit_bytes)
                await safe_delete(status_msg)
                logger.info("处理完成（顺序下载重传） chat=@%s msg_id=%s items=%s", chat_username, msg_id, batch.count)
                return

            await download_and_reupload_batch(target_entity, status_msg, batch, caption, size_limit_bytes)
            await safe_delete(status_msg)
            logger.info("处理完成（下载重传） chat=@%s msg_id=%s items=%s", chat_username, msg_id, batch.count)

        except Exception as exc:
            user_message = friendly_exception_message(exc)
            logger.error("处理消息时发生异常：%s", exc, exc_info=True)
            await show_error(status_msg, user_message)


@bot.on(events.NewMessage(incoming=True, pattern=r"(?i)^/(start|help)(?:@\w+)?$"))
async def start_handler(event):
    await event.respond(
        "👋 发送一个公开频道/群组的消息链接，我会提取其中的视频、音频、图片，"
        "并支持常见相册/媒体组。\n\n"
        "支持格式：\n"
        "• <code>https://t.me/channel_name/123</code>\n"
        "• <code>https://t.me/s/channel_name/123</code>\n\n"
        "特点：\n"
        "• 尽量直接复用 Telegram 媒体，减少重复下载\n"
        "• 需要时自动下载后重新发送，隐藏来源\n"
        "• 常见图片/视频相册会优先按媒体组发送\n"
        "• 视频优先保留原始封面，无封面时可用 ffmpeg 抽帧\n"
        "• 自动清理临时文件，更适合小规格 LXC 长期运行\n\n"
        "⚠️ 仅支持公开频道/群组，不支持 t.me/c 私有链接。",
        parse_mode="html",
    )


@bot.on(events.NewMessage(incoming=True))
async def message_router(event):
    text = (event.raw_text or "").strip()

    if text.startswith("/"):
        return

    if PRIVATE_LINK_PATTERN.search(text):
        await event.respond("❌ 暂不支持私有频道/群组链接（t.me/c/...），目前仅支持公开链接。")
        return

    public_link = extract_public_link(text)
    if public_link:
        chat_username, msg_id = public_link
        await process_public_link(event, chat_username, msg_id)
        return

    if event.is_private and text:
        await event.respond("请发送一个公开的 t.me 消息链接，我会提取其中的媒体文件或媒体组。")


if __name__ == "__main__":
    removed_on_startup = cleanup_stale_temp_entries()
    logger.info(
        "Bot starting: session_dir=%s temp_dir=%s noforwards_max_size=%s concurrency=%s ffmpeg=%s startup_cleanup=%s",
        SETTINGS.session_dir,
        SETTINGS.temp_dir_base,
        format_size(SETTINGS.noforwards_max_file_size_bytes),
        SETTINGS.max_concurrent_jobs,
        "enabled" if SETTINGS.enable_ffmpeg_thumb else "disabled",
        removed_on_startup,
    )
    bot.start(bot_token=SETTINGS.bot_token)
    track_task(bot.loop.create_task(periodic_cleanup_loop()))
    logger.info("Bot started, listening...")
    bot.run_until_disconnected()
