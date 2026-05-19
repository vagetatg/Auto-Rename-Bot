import os
import re
import time
import shutil
import asyncio
import logging
from datetime import datetime
from PIL import Image
from pyrogram import Client, filters
from pyrogram.types import Message
from plugins.antinsfw import check_anti_nsfw
from helper.utils import progress_for_pyrogram
from helper.database import codeflixbots

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Track running operations
renaming_operations = {}

# ---------------- REGEX PATTERNS ---------------- #

SEASON_EPISODE_PATTERNS = [
    (re.compile(r'S(\d+)(?:E|EP)(\d+)', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'S(\d+)[\s-]*(?:E|EP)(\d+)', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'Season\s*(\d+)\s*Episode\s*(\d+)', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'\[S(\d+)\]\[E(\d+)\]', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'S(\d+)[^\d]*(\d+)', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'(?:E|EP|Episode)\s*(\d+)', re.IGNORECASE), (None, 'episode')),
]

QUALITY_PATTERNS = [
    (re.compile(r'\b(\d{3,4}[pi])\b', re.IGNORECASE), lambda m: m.group(1)),
    (re.compile(r'\b(4k|2160p)\b', re.IGNORECASE), lambda m: "4K"),
    (re.compile(r'\b(2k|1440p)\b', re.IGNORECASE), lambda m: "2K"),
    (re.compile(r'\b(HDRip|HDTV|WEB-DL|BluRay)\b', re.IGNORECASE), lambda m: m.group(1)),
    (re.compile(r'\[(\d{3,4}[pi])\]', re.IGNORECASE), lambda m: m.group(1))
]

# ---------------- HELPERS ---------------- #

def sanitize_filename(filename):
    filename = re.sub(r'[\\/:*?"<>|]', '', filename)
    filename = re.sub(r'\s+', ' ', filename).strip()
    return filename


def extract_season_episode(filename):
    for pattern, (season_group, episode_group) in SEASON_EPISODE_PATTERNS:
        match = pattern.search(filename)

        if match:
            season = match.group(1) if season_group else None
            episode = match.group(2) if episode_group else match.group(1)

            logger.info(f"Season: {season}, Episode: {episode}")
            return season, episode

    logger.warning(f"No season/episode matched for {filename}")
    return None, None


def extract_quality(filename):
    for pattern, extractor in QUALITY_PATTERNS:
        match = pattern.search(filename)

        if match:
            quality = extractor(match)

            logger.info(f"Quality: {quality}")
            return quality

    logger.warning(f"No quality matched for {filename}")
    return "Unknown"


async def cleanup_files(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)

        except Exception as e:
            logger.error(f"Cleanup error {path}: {e}")


async def process_thumbnail(thumb_path):
    if not thumb_path or not os.path.exists(thumb_path):
        return None

    try:
        with Image.open(thumb_path) as img:
            img = img.convert("RGB")
            img = img.resize((320, 320))
            img.save(thumb_path, "JPEG")

        return thumb_path

    except Exception as e:
        logger.error(f"Thumbnail error: {e}")

        await cleanup_files(thumb_path)
        return None


async def add_metadata(input_path, output_path, user_id):
    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:
        raise RuntimeError("FFmpeg not found in PATH")

    title = await codeflixbots.get_title(user_id) or ""
    artist = await codeflixbots.get_artist(user_id) or ""
    author = await codeflixbots.get_author(user_id) or ""

    cmd = [
        ffmpeg,
        "-i", input_path,
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"author={author}",
        "-map", "0",
        "-c", "copy",
        "-loglevel", "error",
        output_path
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(stderr.decode())


# ---------------- MAIN HANDLER ---------------- #

@Client.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def auto_rename_files(client, message: Message):

    user_id = message.from_user.id

    format_template = await codeflixbots.get_format_template(user_id)

    if not format_template:
        return await message.reply_text(
            "Please set rename format using /autorename"
        )

    # Prevent duplicate processing
    file_id = None

    thumb_path = None
    download_path = None
    metadata_path = None

    try:

        # -------- GET MEDIA INFO -------- #

        if message.document:
            media = message.document
            media_type = "document"

        elif message.video:
            media = message.video
            media_type = "video"

        elif message.audio:
            media = message.audio
            media_type = "audio"

        else:
            return await message.reply_text("Unsupported file type")

        file_id = media.file_id
        file_name = media.file_name or "file"
        file_size = media.file_size

        # NSFW CHECK
        if await check_anti_nsfw(file_name, message):
            return await message.reply_text("NSFW content detected")

        # DUPLICATE CHECK
        if file_id in renaming_operations:

            old_time = renaming_operations[file_id]

            if (datetime.now() - old_time).seconds < 10:
                return

        renaming_operations[file_id] = datetime.now()

        # -------- EXTRACT INFO -------- #

        season, episode = extract_season_episode(file_name)
        quality = extract_quality(file_name)

        replacements = {
            "{season}": season or "XX",
            "{episode}": episode or "XX",
            "{quality}": quality,
            "Season": season or "XX",
            "Episode": episode or "XX",
            "QUALITY": quality
        }

        for placeholder, value in replacements.items():
            format_template = format_template.replace(
                placeholder,
                str(value)
            )

        # -------- FILE PATHS -------- #

        ext = os.path.splitext(file_name)[1]

        if not ext:
            ext = ".mp4" if media_type == "video" else ".mp3"

        new_filename = sanitize_filename(
            f"{format_template}{ext}"
        )

        os.makedirs("downloads", exist_ok=True)
        os.makedirs("metadata", exist_ok=True)

        download_path = os.path.join(
            "downloads",
            new_filename
        )

        metadata_path = os.path.join(
            "metadata",
            f"meta_{new_filename}"
        )

        # -------- DOWNLOAD -------- #

        msg = await message.reply_text("Downloading...")

        file_path = await client.download_media(
            message,
            file_name=download_path,
            progress=progress_for_pyrogram,
            progress_args=(
                "Downloading...",
                msg,
                time.time()
            )
        )

        # -------- METADATA -------- #

        await msg.edit("Adding metadata...")

        await add_metadata(
            file_path,
            metadata_path,
            user_id
        )

        file_path = metadata_path

        # -------- CAPTION -------- #

        caption = await codeflixbots.get_caption(
            message.chat.id
        )

        if not caption:
            caption = f"**{new_filename}**"

        # -------- THUMBNAIL -------- #

        thumb = await codeflixbots.get_thumbnail(
            message.chat.id
        )

        if thumb:

            thumb_path = await client.download_media(
                thumb
            )

        elif media_type == "video" and media.thumbs:

            thumb_path = await client.download_media(
                media.thumbs[0].file_id
            )

        thumb_path = await process_thumbnail(
            thumb_path
        )

        # -------- UPLOAD -------- #

        await msg.edit("Uploading...")

        upload_args = {
            "chat_id": message.chat.id,
            "caption": caption,
            "thumb": thumb_path,
            "progress": progress_for_pyrogram,
            "progress_args": (
                "Uploading...",
                msg,
                time.time()
            )
        }

        if media_type == "document":

            await client.send_document(
                document=file_path,
                **upload_args
            )

        elif media_type == "video":

            await client.send_video(
                video=file_path,
                **upload_args
            )

        elif media_type == "audio":

            await client.send_audio(
                audio=file_path,
                **upload_args
            )

        await msg.delete()

    except Exception as e:

        logger.error(f"Processing error: {e}")

        await message.reply_text(
            f"Error:\n`{str(e)}`"
        )

    finally:

        await cleanup_files(
            download_path,
            metadata_path,
            thumb_path
        )

        if file_id:
            renaming_operations.pop(file_id, None)
