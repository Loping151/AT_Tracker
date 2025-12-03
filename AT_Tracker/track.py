from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.aps import scheduler
from gsuid_core.sv import SV
from gsuid_core.logger import logger
from gsuid_core.utils.download_resource.download_file import download
from gsuid_core.utils.image.convert import convert_img
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import json
import asyncio
from collections import deque
import hashlib
import os
import shutil

from .at_tracker_config import ATTrackerConfig
from .utils.resource.RESOURCE_PATH import (
    RECORD_PATH,
    AVATAR_CACHE_PATH,
)

# --- 字体路径 ---
font_path = Path(__file__).parent / "SourceHanSerifCN-Bold.otf"

# --- 全局缓存 ---
message_cache: Dict[int, deque] = {}  # group_id -> deque of messages
at_records: Dict[int, List[Dict]] = {}  # group_id -> list of at records
active_at_tracking: Dict[int, List[Dict]] = {}  # group_id -> list of active tracking sessions

# --- SV定义 ---
at_tracker_sv = SV("AT追踪", area="GROUP")
at_tracker_msg = SV("AT追踪消息监听", priority=5, area="GROUP")


# --- 配置获取函数 ---
def get_config(key: str):
    """获取配置值"""
    return ATTrackerConfig.get_config(key).data


# --- 初始化与定时任务 ---
async def init():
    """初始化插件"""
    logger.debug("AT追踪插件已启动")
    load_at_records()
    await cleanup_old_records()


@scheduler.scheduled_job("cron", day="*", hour=4, minute=0, misfire_grace_time=60)
async def scheduled_cleanup():
    """每日定时执行清理任务"""
    logger.debug("开始执行每日AT记录清理任务...")
    await cleanup_old_records()
    logger.debug("每日AT记录清理任务执行完毕。")


async def cleanup_old_records():
    """删除超过指定天数的旧AT记录和相关文件"""
    logger.debug("开始清理旧的AT记录...")
    retention_days = get_config("RETENTION_DAYS")
    cutoff_date = datetime.now() - timedelta(days=retention_days)

    for group_id, records in list(at_records.items()):
        records_to_keep = []
        for record in records:
            try:
                record_time = datetime.strptime(record["start_time"], "%Y%m%d %H:%M:%S")
                if record_time < cutoff_date:
                    record_id = record["id"]
                    group_data_dir = RECORD_PATH / str(group_id)

                    # 删除关联的图片文件
                    for image_filename in record.get("associated_images", []):
                        image_path = group_data_dir / image_filename
                        if image_path.exists():
                            try:
                                os.remove(image_path)
                                logger.debug(f"已删除过期记录关联的图片: {image_path}")
                            except OSError as e:
                                logger.error(f"删除图片 {image_path} 失败: {e}")

                    # 删除记录文件本身
                    file_path = group_data_dir / f"at_record_{record_id}.json"
                    if file_path.exists():
                        try:
                            os.remove(file_path)
                            logger.debug(f"已删除过期记录文件: {file_path}")
                        except OSError as e:
                            logger.error(f"删除记录文件 {file_path} 失败: {e}")
                else:
                    records_to_keep.append(record)
            except (ValueError, KeyError) as e:
                logger.warning(f"处理记录时出错，将保留该记录: {record.get('id', 'N/A')}, 错误: {e}")
                records_to_keep.append(record)

        at_records[group_id] = records_to_keep

    for group_dir in RECORD_PATH.iterdir():
        if group_dir.is_dir() and not any(group_dir.iterdir()) and group_dir.name.isdigit():
            try:
                shutil.rmtree(group_dir)
                logger.debug(f"已删除空的群组数据文件夹: {group_dir}")
            except OSError as e:
                logger.error(f"删除空文件夹 {group_dir} 失败: {e}")

    if get_config("EnableAvatarCache"):
        try:
            if AVATAR_CACHE_PATH.exists():
                shutil.rmtree(AVATAR_CACHE_PATH)
            AVATAR_CACHE_PATH.mkdir(parents=True, exist_ok=True)
            logger.info("已清理头像缓存。")
        except OSError as e:
            logger.error(f"清理头像缓存失败: {e}")


# --- 数据读写 ---
def load_at_records():
    """加载本地保存的AT记录"""
    global at_records
    at_records = {}
    try:
        if not RECORD_PATH.exists():
            return
        for group_dir in RECORD_PATH.iterdir():
            if group_dir.is_dir() and group_dir.name.isdigit():
                try:
                    group_id = int(group_dir.name)
                    at_records[group_id] = []
                    for file_path in group_dir.glob("at_record_*.json"):
                        with open(file_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            at_records[group_id].append(data)
                except (ValueError, json.JSONDecodeError) as e:
                    logger.error(f"加载群组 {group_dir.name} 的记录失败: {e}")
    except Exception as e:
        logger.error(f"加载所有AT记录失败: {e}")


def save_at_record(record: Dict):
    """保存AT记录到本地"""
    try:
        group_id = record["group_id"]
        record_id = record["id"]

        group_data_dir = RECORD_PATH / str(group_id)
        group_data_dir.mkdir(parents=True, exist_ok=True)

        file_path = group_data_dir / f"at_record_{record_id}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存AT记录失败: {e}")


# --- 网络与工具函数 ---
async def download_image(url: str, save_path: Path) -> bool:
    """下载图片到本地并转换为webp"""
    try:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        # 使用gs的下载函数
        await download(url, save_path.parent, save_path.name, tag="[AT_Tracker]")

        # 转换为webp格式
        try:
            img = Image.open(save_path)
            webp_path = save_path.with_suffix(".webp")
            img.save(webp_path, "WEBP")
            # 删除原始文件
            if save_path.exists() and save_path != webp_path:
                os.remove(save_path)
            return True
        except Exception as e:
            logger.warning(f"转换图片为webp失败，保留原始格式: {e}")
            return True
    except Exception as e:
        logger.error(f"下载图片失败 {url}: {e}")
    return False


async def get_user_avatar(qq: str) -> Optional[Image.Image]:
    """获取用户QQ头像"""
    try:
        if not get_config("EnableAvatarCache"):
            # 不启用缓存，直接下载到临时文件
            avatar_url = f"http://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
            temp_file = f"{qq}_temp.jpg"
            await download(avatar_url, AVATAR_CACHE_PATH, temp_file, tag="[AT_Tracker]")
            avatar_path = AVATAR_CACHE_PATH / temp_file
            if avatar_path.exists():
                try:
                    img = Image.open(avatar_path)
                    avatar_path.unlink()
                    return img
                except Exception as e:
                    logger.warning(f"打开临时头像文件失败: {e}")
                    try:
                        avatar_path.unlink()
                    except:
                        pass
                    return None
            return None

        # 启用缓存
        avatar_path = AVATAR_CACHE_PATH / f"{qq}.jpg"
        if avatar_path.exists():
            return Image.open(avatar_path)

        avatar_url = f"http://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
        await download(avatar_url, AVATAR_CACHE_PATH, f"{qq}.jpg", tag="[AT_Tracker]")

        if avatar_path.exists():
            return Image.open(avatar_path)
        return None
    except Exception as e:
        logger.error(f"获取QQ头像失败 {qq}: {e}")
        return None


async def parse_and_enrich_message(bot: Bot, group_id: int, event: Event) -> List[Dict]:
    """解析消息内容，并为at消息段补充群名片信息"""
    content_list = []

    # 处理文本内容
    if event.text:
        text = event.text.strip()
        if text:
            content_list.append({"type": "text", "content": text[:200]})

    # 处理图片列表
    if event.image_list:
        for image_url in event.image_list:
            if isinstance(image_url, str):
                content_list.append({"type": "image", "url": image_url})

    # 处理at列表
    if event.at_list:
        for at_qq in event.at_list:
            qq = str(at_qq)
            # 只记录QQ号，不带@前缀。
            # 具体的昵称显示交给生成图片时的上下文处理。
            card = "全体成员" if qq == "all" else qq
            content_list.append({"type": "at", "qq": qq, "card": card})

    return content_list

async def process_images_in_message(msg_record: Dict, group_id: int, associated_images: List[str]):
    """检查消息中是否有图片，下载它们并更新关联图片列表"""
    for item in msg_record.get("content", []):
        if item["type"] == "image":
            url = item["url"]
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            img_name = f"{timestamp}_{hashlib.md5(url.encode()).hexdigest()}.webp"
            img_path = RECORD_PATH / str(group_id) / img_name
            if not img_path.exists():
                await download_image(url, img_path)
            item["local_path"] = str(img_path)
            if img_name not in associated_images:
                associated_images.append(img_name)


async def process_group_message(bot: Bot, event: Event):
    """处理群组消息的AT追踪"""
    # 仅处理群组消息
    if not event.group_id:
        return

    group_id = int(event.group_id)
    user_id = int(event.user_id)
    cache_size = get_config("CACHE_SIZE")
    tracking_count = get_config("TRACKING_COUNT")

    # Part 1: 消息预处理和缓存
    if group_id not in message_cache:
        message_cache[group_id] = deque(maxlen=cache_size)

    # 获取用户昵称
    card = event.sender.get("nickname", str(user_id)) if event.sender else str(user_id)

    content = await parse_and_enrich_message(bot, group_id, event)

    msg_record = {
        "user_id": user_id,
        "card": card,
        "time": datetime.now().strftime("%Y%m%d %H:%M:%S"),
        "content": content,
        "message_id": event.msg_id,
    }

    message_cache[group_id].append(msg_record)

    # Part 2: 处理并更新所有活跃的追踪会话
    sessions_to_keep = []
    if group_id in active_at_tracking:
        for session in active_at_tracking[group_id]:
            # 查找与会话关联的内存中的记录
            record_to_update = next(
                (r for r in at_records.get(group_id, []) if r["id"] == session["record_id"]),
                None,
            )

            if record_to_update:
                # 实时追加消息并保存
                await process_images_in_message(msg_record, group_id, record_to_update["associated_images"])
                record_to_update["messages"].append(msg_record)
                save_at_record(record_to_update)
                logger.debug(f"Appended message to record {session['record_id']} and saved.")

                session["remaining"] -= 1
                if session["remaining"] > 0:
                    sessions_to_keep.append(session)
                else:
                    logger.info(f"AT tracking session for record {session['record_id']} has finished.")
            else:
                logger.warning(
                    f"Could not find record for active tracking session {session['record_id']}. Removing session."
                )

    if sessions_to_keep:
        active_at_tracking[group_id] = sessions_to_keep
    elif group_id in active_at_tracking:
        del active_at_tracking[group_id]

    # Part 3: 检查当前消息是否需要开启新的追踪会话
    has_at = any(item["type"] == "at" for item in content)
    at_targets = [{"qq": item["qq"], "card": item["card"]} for item in content if item["type"] == "at"]

    if has_at and str(user_id) != bot.bot_self_id:
        is_new_session_needed = True
        current_targets_qq = {str(t.get("qq")) for t in at_targets}

        if group_id in active_at_tracking:
            for session in active_at_tracking[group_id]:
                session_sender_id = str(session["sender_id"])
                session_targets_qq = {str(t.get("qq")) for t in session["targets"]}
                if session_sender_id == str(user_id) and session_targets_qq == current_targets_qq:
                    is_new_session_needed = False
                    break

        if is_new_session_needed:
            # 寻找上下文
            cache_list = list(message_cache[group_id])
            first_sender_msg_index = -1
            for i, msg in enumerate(cache_list):
                if msg["user_id"] == user_id:
                    first_sender_msg_index = i
                    break

            start_index = max(0, first_sender_msg_index - 1) if first_sender_msg_index != -1 else 0
            initial_messages = cache_list[start_index:]

            record_id = f"{group_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{hashlib.md5(str(event.msg_id).encode()).hexdigest()[:8]}"

            # 创建初始记录
            at_record = {
                "id": record_id,
                "group_id": group_id,
                "sender_id": user_id,
                "targets": at_targets,
                "start_time": datetime.now().strftime("%Y%m%d %H:%M:%S"),
                "messages": initial_messages,
                "associated_images": [],
            }

            # 处理初始消息中的图片
            for msg in initial_messages:
                await process_images_in_message(msg, group_id, at_record["associated_images"])

            # 立即写入内存和文件
            if group_id not in at_records:
                at_records[group_id] = []
            at_records[group_id].append(at_record)
            save_at_record(at_record)
            logger.info(f"New AT detected. Immediately created and saved record {record_id}.")

            # 创建新的追踪会话
            new_session = {
                "record_id": record_id,
                "sender_id": user_id,
                "targets": at_targets,
                "remaining": tracking_count,
            }

            if group_id not in active_at_tracking:
                active_at_tracking[group_id] = []
            active_at_tracking[group_id].append(new_session)
            logger.info(f"Started new AT tracking session for record {record_id}. Tracking next {tracking_count} messages.")


# --- 消息监听：自动追踪所有群消息 ---
@at_tracker_msg.on_message(block=False)
async def track_group_messages(bot: Bot, event: Event):
    """监听所有消息并自动追踪AT"""
    await process_group_message(bot, event)


# --- 命令处理器 ---
def get_query_user_id(ev: Event) -> str:
    """获取查询的用户ID，支持AT他人"""
    # 如果有AT信息，使用被AT的用户
    if ev.at:
        return str(ev.at)
    # 否则使用发送者
    return str(ev.user_id)


@at_tracker_sv.on_command(
    ("谁at我", "谁艾特我", "谁@我"),
    block=True,
)
async def handle_who_at_me(bot: Bot, event: Event):
    """查询AT记录，支持AT他人查询"""
    if not event.group_id:
        return await bot.send("此命令仅限群组使用")

    group_id = int(event.group_id)
    user_id = get_query_user_id(event)  # 支持AT他人
    retention_days = get_config("RETENTION_DAYS")

    # --- 获取用户的显示名称 ---
    # 如果是本人查询，直接从当前事件中获取命令发送者的群名片或昵称
    target_name = user_id
    if str(user_id) == str(event.user_id) and event.sender:
        # 优先使用群名片，其次昵称，最后保底使用QQ号
        target_name = event.sender.get("card") or event.sender.get("nickname") or user_id

    group_at_records = at_records.get(group_id, [])

    valid_records = []
    cutoff_date = datetime.now() - timedelta(days=retention_days)
    for record in group_at_records:
        try:
            record_time = datetime.strptime(record["start_time"], "%Y%m%d %H:%M:%S")
            if record_time >= cutoff_date:
                valid_records.append(record)
        except (ValueError, KeyError):
            logger.warning(f"记录 {record.get('id', 'N/A')} 的日期格式不正确，已跳过。")
            continue

    user_at_records = [
        record
        for record in valid_records
        if any(str(t.get("qq")) == user_id or t.get("qq") == "all" for t in record.get("targets", []))
    ]

    # 规则修改: 结束的session才决定最终要不要展示
    finalized_user_records = []
    for record in user_at_records:
        sender_id = str(record["sender_id"])
        messages = record["messages"]
        # 查找原始的@消息之后，发送者是否再次发言
        at_msg_index = -1
        for i, msg in enumerate(messages):
            if any(item.get("type") == "at" for item in msg.get("content", [])) and str(msg.get("user_id")) == sender_id:
                at_msg_index = i
                break

        if at_msg_index == -1:
            continue

        last_sender_index_after_at = -1
        for i in range(at_msg_index + 1, len(messages)):
            if str(messages[i].get("user_id")) == sender_id:
                last_sender_index_after_at = i

        # 确定最终消息范围
        start_index = 0
        end_index = len(messages)
        if last_sender_index_after_at != -1:
            end_index = min(last_sender_index_after_at + 2, len(messages))

        finalized_record = record.copy()
        finalized_record["messages"] = record["messages"][start_index:end_index]
        finalized_user_records.append(finalized_record)

    recent_records = sorted(finalized_user_records, key=lambda x: x["start_time"], reverse=True)[:10]

    if not recent_records:
        return await bot.send("最近没有人@你哦")

    try:
        images = []
        # 构建替换字典：{QQ号: 昵称}
        name_map = {user_id: target_name} if target_name != user_id else {}

        for record in recent_records:
            # 传入 name_map
            img = await generate_chat_image(bot, record, name_map=name_map)
            if img:
                # 转换图片为可发送格式
                converted_img = await convert_img(img)
                images.append(converted_img)

        if not images:
            logger.error("没有生成任何图片")
            return

        # 返回图片
        if len(images) == 1:
            # 单张图片：直接发送
            return await bot.send(images[0])
        else:
            # 多张图片：使用列表发送（系统会自动转发）
            return await bot.send(images)

    except Exception as e:
        import traceback
        logger.error("生成AT记录图片失败:\n" + traceback.format_exc())
        logger.error(f"处理查询命令失败: {e}")

def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    lines = []
    for paragraph in text.split("\n"):
        if font.getbbox(paragraph)[2] <= max_width:
            lines.append(paragraph)
        else:
            current_line = ""
            for char in paragraph:
                if font.getbbox(current_line + char)[2] <= max_width:
                    current_line += char
                else:
                    lines.append(current_line)
                    current_line = char
            lines.append(current_line)
    return "\n".join(lines)


async def generate_chat_image(bot: Bot, record: Dict, name_map: Dict[str, str] = None) -> Optional[Image.Image]:
    try:
        width, padding = 700, 20
        avatar_size, msg_padding = 45, 15
        bubble_padding, max_bubble_width = 12, 450

        try:
            cjk_font = ImageFont.truetype(str(font_path), 18)
            small_cjk_font = ImageFont.truetype(str(font_path), 14)
            time_cjk_font = ImageFont.truetype(str(font_path), 12)

        except Exception as e:
            logger.warning(f"加载字体失败，将使用默认字体: {e}")
            cjk_font = small_cjk_font = time_cjk_font = ImageFont.load_default()

        img = Image.new("RGB", (width, 20000), color="#f5f5f5")
        draw = ImageDraw.Draw(img)

        draw.rectangle([0, 0, width, 50], fill="#4a90e2")
        title = f"AT记录 by 小维151 - {record.get('start_time', '')}" # 这里的名字留着，不过分吧
        draw.text((padding, 15), title, font=cjk_font, fill="#ffffff")

        current_y = 65

        messages = record.get("messages", [])
        for msg in messages:
            user_id = str(msg.get("user_id", ""))

            avatar_x, avatar_y = padding, current_y

            avatar_img = await get_user_avatar(user_id)
            if avatar_img:
                avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
                mask = Image.new("L", (avatar_size, avatar_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse([0, 0, avatar_size, avatar_size], fill=255)
                img.paste(avatar_img, (avatar_x, avatar_y), mask)

            content_x = avatar_x + avatar_size + msg_padding
            content_y = current_y

            card = msg.get("card", "Unknown")
            draw.text((content_x, content_y), card, font=small_cjk_font, fill="#666666")
            content_y += 22

            inner_content_y = content_y

            for item in msg.get("content", []):
                item_spacing = 8

                if item["type"] == "text":
                    wrapped_text = wrap_text(item["content"], cjk_font, max_bubble_width)
                    bbox = draw.multiline_textbbox((0, 0), wrapped_text, font=cjk_font, spacing=5)
                    text_width = bbox[2] - bbox[0]
                    text_height = bbox[3] - bbox[1]

                    bubble_rect = (
                        content_x,
                        inner_content_y,
                        content_x + text_width + bubble_padding * 2,
                        inner_content_y + text_height + bubble_padding * 2,
                    )
                    draw.rounded_rectangle(bubble_rect, radius=10, fill="#ffffff")
                    draw.multiline_text(
                        (content_x + bubble_padding, inner_content_y + bubble_padding),
                        wrapped_text,
                        font=cjk_font,
                        fill="#333333",
                        spacing=5,
                    )
                    inner_content_y += text_height + bubble_padding * 2 + item_spacing

                elif item["type"] == "image":
                    local_path = item.get("local_path")
                    try:
                        if not (local_path and Path(local_path).exists()):
                            raise FileNotFoundError
                        with Image.open(local_path) as img_content:
                            img_content.thumbnail((250, 200), Image.Resampling.LANCZOS)
                            img.paste(img_content, (content_x, inner_content_y))
                            inner_content_y += img_content.height + item_spacing
                    except Exception:
                        placeholder_w, placeholder_h = 150, 100
                        draw.rounded_rectangle(
                            (content_x, inner_content_y, content_x + placeholder_w, inner_content_y + placeholder_h),
                            radius=10,
                            fill="#e0e0e0",
                        )
                        draw.text((content_x + 55, inner_content_y + 40), "[图片]", font=cjk_font, fill="#999999")
                        inner_content_y += placeholder_h + item_spacing

                elif item["type"] == "at":
                    qq_id = str(item.get('qq', ''))

                    # 优先使用 name_map 中的昵称，否则使用记录中的 card，最后保底用 qq_id
                    card_text = item.get('card', qq_id)
                    if name_map and qq_id in name_map:
                        card_text = name_map[qq_id]

                    # 统一添加 @ 前缀
                    at_text = f"@{card_text}"

                    bbox = draw.textbbox((0, 0), at_text, font=cjk_font)
                    text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]

                    at_rect = (
                        content_x,
                        inner_content_y,
                        content_x + text_width + bubble_padding,
                        inner_content_y + text_height + bubble_padding,
                    )
                    draw.rounded_rectangle(at_rect, radius=8, fill="#e3f2fd")
                    draw.text(
                        (content_x + bubble_padding / 2, inner_content_y + bubble_padding / 2),
                        at_text,
                        font=small_cjk_font,
                        fill="#1976d2",
                    )
                    inner_content_y += text_height + bubble_padding + item_spacing

            avatar_bottom = avatar_y + avatar_size
            content_bottom = inner_content_y

            time_str = msg.get("time", "").split(" ")[1] if " " in msg.get("time", "") else ""
            if time_str:
                draw.text((content_x, content_bottom), time_str, font=time_cjk_font, fill="#999999")
                content_bottom += 15

            current_y = max(avatar_bottom, content_bottom) + msg_padding

        final_height = current_y + padding
        img = img.crop((0, 0, width, final_height))
        return img

    except Exception as e:
        logger.error(f"生成聊天记录图片失败: {e}")
        return None

# 初始化
asyncio.create_task(init())
