"""
Telegram File Extractor Bot — 纯 MTProto (Telethon)

所有媒体都以"重新发送"的方式发给用户（隐藏来源）。
来源链接以超链接形式显示在 caption 底部。
视频保留原始封面，无封面时用 ffmpeg 从视频提取。

策略：
1. 允许转发 → 秒传（引用原 media 对象），通过底层 API 附加缩略图
2. 禁止转发 → 下载文件后重新发送
"""

import os
import re
import logging
import tempfile
from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    DocumentAttributeFilename,
    PhotoSize,
    PhotoSizeProgressive,
    PhotoCachedSize,
    PhotoStrippedSize,
    InputMediaDocument,
    InputDocument,
)
from telethon.tl.functions.messages import SendMediaRequest
from telethon.utils import get_input_peer

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

# 会话文件存放路径（持久化目录）
SESSION_DIR = os.environ.get("SESSION_DIR", "/var/lib/tgbot/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)
SESSION_PATH = os.path.join(SESSION_DIR, "bot_session")

bot = TelegramClient(SESSION_PATH, API_ID, API_HASH)

LINK_PATTERN = re.compile(
    r"https?://t\.me/(?P<chat>[a-zA-Z0-9_]+)/(?P<msg_id>\d+)"
)


def get_media_type(message):
    media = message.media
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


def get_video_attributes(message):
    if not hasattr(message.media, "document") or not message.media.document:
        return None
    for attr in message.media.document.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            return attr
    return None


def get_filename(message, media_type):
    if hasattr(message.media, "document") and message.media.document:
        for attr in message.media.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name
    ext_map = {"photo": ".jpg", "video": ".mp4", "audio": ".mp3"}
    return "file" + ext_map.get(media_type, ".bin")


def format_size(size_bytes):
    if size_bytes < 1024 * 1024:
        return "{:.1f}KB".format(size_bytes / 1024)
    return "{:.1f}MB".format(size_bytes / (1024 * 1024))


def build_caption(original_text, link):
    parts = []
    if original_text:
        text = original_text[:900] + "..." if len(original_text) > 900 else original_text
        parts.append(text)
    parts.append('📎 <a href="{}">来源链接</a>'.format(link))
    return "\n\n".join(parts)


async def download_thumb_from_message(message, tmpdir):
    """从原消息的 document.thumbs 下载缩略图"""
    try:
        if not hasattr(message.media, "document") or not message.media.document:
            return None
        thumbs = message.media.document.thumbs
        if not thumbs:
            return None

        best = None
        best_size = 0
        for t in thumbs:
            if isinstance(t, PhotoStrippedSize):
                continue
            if isinstance(t, PhotoSizeProgressive):
                s = max(t.sizes) if t.sizes else 0
            elif isinstance(t, PhotoCachedSize):
                s = len(t.bytes) if t.bytes else 0
            elif isinstance(t, PhotoSize):
                s = t.size
            else:
                continue
            if s > best_size:
                best_size = s
                best = t

        if best is None:
            return None

        thumb_path = os.path.join(tmpdir, "thumb.jpg")

        if isinstance(best, PhotoCachedSize) and best.bytes:
            with open(thumb_path, "wb") as f:
                f.write(best.bytes)
        else:
            await bot.download_media(message, file=thumb_path, thumb=best)

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 100:
            logger.info("Thumb from message: {} bytes".format(os.path.getsize(thumb_path)))
            return thumb_path
    except Exception as e:
        logger.warning("Failed to download thumb from message: {}".format(e))
    return None


async def get_thumb(message, tmpdir):
    """获取缩略图"""
    return await download_thumb_from_message(message, tmpdir)


async def resend_with_thumb(user_id, message, caption, tmpdir):
    """
    策略1：秒传 + 封面
    上传缩略图为 InputFile，再通过底层 SendMediaRequest 引用原 document 发送。
    """
    try:
        doc = message.media.document

        thumb_path = await download_thumb_from_message(message, tmpdir)
        thumb_input = None
        if thumb_path:
            thumb_input = await bot.upload_file(thumb_path)

        input_doc = InputDocument(
            id=doc.id,
            access_hash=doc.access_hash,
            file_reference=doc.file_reference,
        )

        await bot.send_file(
            entity=user_id,
            file=input_doc,
            thumb=thumb_input,
            caption=caption,
            parse_mode="html",
            supports_streaming=True,
        )
        return True
    except Exception as e:
        logger.warning("[RESEND] failed: {}".format(e))
        return False


@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    await event.respond(
        "👋 发送一个公开频道/群组的消息链接，我会提取其中的视频、音频或图片。\n\n"
        "支持格式：\n"
        "• <code>https://t.me/channel_name/123</code>\n\n"
        "• 所有媒体都会隐藏来源重新发送\n"
        "• 来源链接会显示在消息下方\n"
        "• 视频保留原始封面\n\n"
        "⚠️ 仅支持公开频道/群组",
        parse_mode="html",
    )


@bot.on(events.NewMessage(pattern=r"https?://t\.me/"))
async def link_handler(event):
    text = event.raw_text or ""

    match = LINK_PATTERN.search(text)
    if not match:
        await event.respond("❌ 没有识别到有效的 t.me 链接。")
        return

    chat_username = match.group("chat")
    msg_id = int(match.group("msg_id"))

    if chat_username == "c":
        await event.respond("❌ 不支持私有频道/群组链接，仅支持公开的。")
        return

    source_link = "https://t.me/{}/{}".format(chat_username, msg_id)
    status_msg = await event.respond("⏳ 正在处理...")
    user_id = event.chat_id

    # 解析频道
    try:
        entity = await bot.get_entity(chat_username)
    except Exception:
        await status_msg.edit("❌ 找不到频道 @{}, 请确认是公开频道。".format(chat_username))
        return

    # 获取消息
    try:
        message = await bot.get_messages(entity, ids=msg_id)
    except Exception as e:
        await status_msg.edit("❌ 获取消息失败：{}".format(e))
        return

    if message is None:
        await status_msg.edit("❌ 找不到该消息，请检查链接是否正确。")
        return

    media_type = get_media_type(message)
    if media_type is None:
        await status_msg.edit("❌ 该消息不包含支持的媒体（仅支持视频、音频、图片）。")
        return

    no_forwards = getattr(entity, "noforwards", False)
    caption = build_caption(message.text or "", source_link)
    logger.info("@{}/{} type={} noforwards={}".format(chat_username, msg_id, media_type, no_forwards))

    # ====== 策略1: 允许转发 → 秒传 + 缩略图 ======
    if not no_forwards:
        with tempfile.TemporaryDirectory() as tmpdir:
            ok = await resend_with_thumb(user_id, message, caption, tmpdir)
            if ok:
                await status_msg.delete()
                logger.info("[RESEND] success: {}/{}".format(chat_username, msg_id))
                return
            logger.info("[RESEND] failed, falling back to download")

    # ====== 策略2: 禁止转发 或 秒传失败 → 下载后发送 ======
    try:
        filename = get_filename(message, media_type)

        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, filename)

            await status_msg.edit("⬇️ 正在下载：{}".format(filename))
            await bot.download_media(message, file=filepath)

            file_size = os.path.getsize(filepath)
            size_str = format_size(file_size)

            if file_size > 2 * 1024 * 1024 * 1024:
                await status_msg.edit("❌ 文件太大（{}），超过 2GB 限制。".format(size_str))
                return

            # 获取缩略图
            thumb_path = None
            if media_type in ("video", "audio"):
                thumb_path = await get_thumb(message, tmpdir)

            await status_msg.edit("📤 正在发送：{}（{}）".format(filename, size_str))

            send_kwargs = dict(
                entity=user_id,
                file=filepath,
                caption=caption,
                parse_mode="html",
                force_document=False,
                supports_streaming=True,
            )
            if thumb_path:
                send_kwargs["thumb"] = thumb_path
            if media_type == "video":
                video_attr = get_video_attributes(message)
                if video_attr:
                    send_kwargs["attributes"] = [video_attr]

            await bot.send_file(**send_kwargs)
            await status_msg.delete()
            logger.info("[DOWNLOAD] success: {}/{} ({}) thumb={}".format(
                chat_username, msg_id, size_str, "yes" if thumb_path else "no"))

    except Exception as e:
        logger.error("Error: {}".format(e), exc_info=True)
        await status_msg.edit("❌ 出错了：{}".format(e))


@bot.on(events.NewMessage())
async def fallback_handler(event):
    if event.raw_text.startswith("/") or "t.me/" in (event.raw_text or ""):
        return
    await event.respond("请发送一个 t.me 链接，我会提取其中的媒体文件。")


if __name__ == "__main__":
    logger.info("Bot starting (MTProto)")
    bot.start(bot_token=BOT_TOKEN)
    logger.info("Bot started, listening...")
    bot.run_until_disconnected()
