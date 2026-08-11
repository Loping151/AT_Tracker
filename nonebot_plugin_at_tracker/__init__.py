from nonebot import on_command, on_message, get_driver
from nonebot.exception import FinishedException
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import json
import asyncio
from collections import deque
import hashlib
import io
from io import BytesIO
import pycurl
import os
import shutil
from nonebot import require
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

# --- 全局配置 ---
data_dir = Path("./data/at_tracker")
font_path = Path(__file__).parent / "SourceHanSerifCN-Bold.otf"
avatar_path = data_dir / "avatar_cache"
RETENTION_DAYS = 3
CACHE_SIZE = 5
TRACKING_COUNT = 10
MAX_DISK_MB = 1024  # 记录缓存最大磁盘占用(MB)，超过后从最早的记录开始清理

# 聊天图只用于出图，按渲染尺寸存储
IMAGE_MAX_SIZE = (250, 200)
AVATAR_RENDER_SIZE = 45

# --- 全局缓存 ---
message_cache: Dict[int, deque] = {}  # group_id -> deque of messages
at_records: Dict[int, List[Dict]] = {}  # group_id -> list of at records
active_at_tracking: Dict[int, List[Dict]] = {}  # group_id -> list of active tracking sessions
_disk_usage = {"total": -1}  # 记录目录占用字节数的运行计数，-1表示尚未统计
_cleanup_lock = asyncio.Lock()

driver = get_driver()

class HTTPError(Exception):
    pass

# --- 磁盘占用控制 ---
def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0

def _adjust_usage(delta: int):
    if _disk_usage["total"] >= 0:
        _disk_usage["total"] += delta

def _compute_disk_usage() -> int:
    total = 0
    if data_dir.exists():
        for group_dir in data_dir.iterdir():
            if not (group_dir.is_dir() and group_dir.name.isdigit()):
                continue
            for root, _, files in os.walk(group_dir):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
    return total

async def _refresh_disk_usage():
    _disk_usage["total"] = await asyncio.get_event_loop().run_in_executor(None, _compute_disk_usage)

def _remove_file(path: Path):
    try:
        size = path.stat().st_size
        path.unlink()
        _adjust_usage(-size)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.error(f"[谁AT我·追踪] 删除文件 {path} 失败: {e}")

def _delete_record_files(group_id: int, record: Dict):
    group_data_dir = data_dir / str(group_id)
    for image_filename in record.get('associated_images', []):
        _remove_file(group_data_dir / image_filename)
    _remove_file(group_data_dir / f"at_record_{record['id']}.json")

async def enforce_disk_limit():
    """磁盘占用超限时从最早的记录开始清理，直到降至上限的90%"""
    if not MAX_DISK_MB or _cleanup_lock.locked():
        return
    limit = MAX_DISK_MB * 1024 * 1024
    if 0 <= _disk_usage["total"] <= limit:
        return
    async with _cleanup_lock:
        if _disk_usage["total"] < 0:
            await _refresh_disk_usage()
        if _disk_usage["total"] <= limit:
            return
        target = int(limit * 0.9)
        all_records = [
            (record.get('start_time', ''), group_id, record)
            for group_id, records in at_records.items()
            for record in records
        ]
        all_records.sort(key=lambda x: x[0])
        dropped: Dict[int, set] = {}
        removed = 0
        for _, group_id, record in all_records:
            if _disk_usage["total"] <= target:
                break
            record_id = record.get('id')
            if not record_id:
                continue
            _delete_record_files(group_id, record)
            dropped.setdefault(group_id, set()).add(record_id)
            removed += 1
        for group_id, ids in dropped.items():
            at_records[group_id] = [r for r in at_records[group_id] if r.get('id') not in ids]
        if removed:
            logger.info(f"[谁AT我·追踪] 磁盘占用超限，已清理最早的 {removed} 条AT记录。")

# --- 初始化与定时任务 ---
@driver.on_startup
async def init():
    """初始化插件"""
    logger.debug("[谁AT我·追踪] AT追踪插件已启动")
    data_dir.mkdir(parents=True, exist_ok=True)
    avatar_path.mkdir(parents=True, exist_ok=True)
    load_at_records()
    await cleanup_old_records()
    await enforce_disk_limit()

@scheduler.scheduled_job("cron", day="*", hour=4, minute=0, misfire_grace_time=60)
async def scheduled_cleanup():
    """每日定时执行清理任务"""
    logger.debug("[谁AT我·追踪] 开始执行每日AT记录清理任务...")
    await cleanup_old_records()
    await enforce_disk_limit()
    logger.debug("[谁AT我·追踪] 每日AT记录清理任务执行完毕。")

async def cleanup_old_records():
    """删除超过指定天数的旧AT记录和相关文件"""
    logger.debug("[谁AT我·追踪] 开始清理旧的AT记录...")
    cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)
    
    for group_id, records in list(at_records.items()):
        records_to_keep = []
        for record in records:
            try:
                record_time = datetime.strptime(record['start_time'], '%Y%m%d %H:%M:%S')
                if record_time < cutoff_date:
                    record_id = record['id']
                    group_data_dir = data_dir / str(group_id)

                    # 删除关联的图片文件
                    for image_filename in record.get('associated_images', []):
                        image_path = group_data_dir / image_filename
                        if image_path.exists():
                            try:
                                os.remove(image_path)
                                logger.debug(f"[谁AT我·追踪] 已删除过期记录关联的图片: {image_path}")
                            except OSError as e:
                                logger.error(f"[谁AT我·追踪] 删除图片 {image_path} 失败: {e}")
                    
                    # 删除记录文件本身
                    file_path = group_data_dir / f"at_record_{record_id}.json"
                    if file_path.exists():
                        try:
                            os.remove(file_path)
                            logger.debug(f"[谁AT我·追踪] 已删除过期记录文件: {file_path}")
                        except OSError as e:
                            logger.error(f"[谁AT我·追踪] 删除记录文件 {file_path} 失败: {e}")
                else:
                    records_to_keep.append(record)
            except (ValueError, KeyError) as e:
                logger.warning(f"[谁AT我·追踪] 处理记录时出错，将保留该记录: {record.get('id', 'N/A')}, 错误: {e}")
                records_to_keep.append(record)
        
        at_records[group_id] = records_to_keep

    for group_dir in data_dir.iterdir():
        if group_dir.is_dir() and not any(group_dir.iterdir()) and group_dir.name.isdigit():
            try:
                shutil.rmtree(group_dir)
                logger.debug(f"[谁AT我·追踪] 已删除空的群组数据文件夹: {group_dir}")
            except OSError as e:
                logger.error(f"[谁AT我·追踪] 删除空文件夹 {group_dir} 失败: {e}")
    try:
        shutil.rmtree(avatar_path)
        avatar_path.mkdir(parents=True, exist_ok=True)
        logger.info("[谁AT我·追踪] 已清理头像缓存。")
    except OSError as e:
        logger.error(f"[谁AT我·追踪] 清理头像缓存失败: {e}")

    await _refresh_disk_usage()
        
# --- 数据读写 ---
def load_at_records():
    """加载本地保存的AT记录"""
    global at_records
    at_records = {}
    try:
        for group_dir in data_dir.iterdir():
            if group_dir.is_dir() and group_dir.name.isdigit():
                try:
                    group_id = int(group_dir.name)
                    at_records[group_id] = []
                    for file_path in group_dir.glob("at_record_*.json"):
                        with open(file_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            at_records[group_id].append(data)
                except (ValueError, json.JSONDecodeError) as e:
                    logger.error(f"[谁AT我·追踪] 加载群组 {group_dir.name} 的记录失败: {e}")
    except Exception as e:
        logger.error(f"[谁AT我·追踪] 加载所有AT记录失败: {e}")

def save_at_record(record: Dict):
    """保存AT记录到本地"""
    try:
        group_id = record['group_id']
        record_id = record['id']
        
        group_data_dir = data_dir / str(group_id)
        group_data_dir.mkdir(parents=True, exist_ok=True)
        
        file_path = group_data_dir / f"at_record_{record_id}.json"
        old_size = _file_size(file_path)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        _adjust_usage(_file_size(file_path) - old_size)
    except Exception as e:
        logger.error(f"[谁AT我·追踪] 保存AT记录失败: {e}")

# --- 网络与工具函数 ---
def download(url: str) -> bytes:
    """下载文件"""
    buffer = BytesIO()
    c = pycurl.Curl()
    c.setopt(c.URL, url)
    c.setopt(c.WRITEDATA, buffer)
    c.setopt(c.FOLLOWLOCATION, True)
    c.setopt(c.TIMEOUT, 20)
    c.perform()
    status_code = c.getinfo(pycurl.RESPONSE_CODE)
    c.close()
    if status_code != 200:
        raise HTTPError(f"httpx status code: {status_code}")
    return buffer.getvalue()

def _shrink_image(path: Path):
    """按渲染尺寸缩放并转webp，覆盖原文件"""
    with Image.open(path) as im:
        im.draft("RGB", IMAGE_MAX_SIZE)  # jpeg 可按比例解码，省时省内存
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "#ffffff")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail(IMAGE_MAX_SIZE, Image.Resampling.LANCZOS)
        im.save(path, "WEBP", quality=75, method=6)

async def download_image(url: str, save_path: Path) -> bool:
    """下载图片到本地并压缩为webp"""
    try:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        content = await asyncio.get_event_loop().run_in_executor(None, download, url)
        with open(save_path, 'wb') as f:
            f.write(content)
        try:
            await asyncio.get_event_loop().run_in_executor(None, _shrink_image, save_path)
        except Exception as e:
            logger.warning(f"[谁AT我·追踪] 压缩图片失败，保留原始文件: {e}")
        return True
    except Exception as e:
        logger.error(f"[谁AT我·追踪] 下载图片失败 {url}: {e}")
    return False

async def get_user_avatar(qq: str) -> Optional[Image.Image]:
    """获取用户QQ头像"""
    try:
        if os.path.exists(data_dir / "avatar_cache" / f"{qq}.jpg"):
            return Image.open(data_dir / "avatar_cache" / f"{qq}.jpg")
        
        avatar_url = f"http://q1.qlogo.cn/g?b=qq&nk={qq}&s=100"
        avatar_bytes = await asyncio.get_event_loop().run_in_executor(None, download, avatar_url)
        
        with open(data_dir / "avatar_cache" / f"{qq}.jpg", 'wb') as f:
            f.write(avatar_bytes)
        
        return Image.open(BytesIO(avatar_bytes))
    except Exception as e:
        logger.error(f"[谁AT我·追踪] 获取QQ头像失败 {qq}: {e}")
        return None

async def parse_and_enrich_message(bot: Bot, group_id: int, message: Message) -> List[Dict]:
    """解析消息内容，并为at消息段补充群名片信息"""
    content_list = []
    for seg in message:
        if seg.type == 'text':
            text = seg.data.get('text', '').strip()
            if text:
                content_list.append({'type': 'text', 'content': text[:200]})
        elif seg.type == 'image':
            url = seg.data.get('url', '')
            if url:
                content_list.append({'type': 'image', 'url': url})
        elif seg.type == 'file':
            content_list.append({'type': 'text', 'content': '[文件]'})
        elif seg.type == 'record':
            content_list.append({'type': 'text', 'content': '[语音]'})
        elif seg.type == 'json':
            content_list.append({'type': 'text', 'content': '[转发消息]'})
        elif seg.type == 'video':
            content_list.append({'type': 'text', 'content': '[视频]'})
        elif seg.type == 'at':
            qq = seg.data.get('qq', '')
            card = f"@{qq}"
            if qq != 'all':
                try:
                    info = await bot.get_group_member_info(group_id=group_id, user_id=int(qq))
                    card = info.get('card') or info.get('nickname', str(qq))
                except Exception as e:
                    logger.warning(f"[谁AT我·追踪] 获取群成员 {qq} 信息失败: {e}")
            else:
                card = "全体成员"
            content_list.append({'type': 'at', 'qq': qq, 'card': card})
        else:
            content_list.append({'type': 'text', 'content': f'[其他消息：{seg.type}]'})
            
    return content_list

async def process_images_in_message(msg_record: Dict, group_id: int, associated_images: List[str]):
    """检查消息中是否有图片，下载它们并更新关联图片列表"""
    for item in msg_record.get('content', []):
        if item['type'] == 'image':
            # 同一条消息会被多个会话处理，已落盘的直接复用，避免重复下载存储
            local_path = item.get('local_path')
            if local_path and Path(local_path).exists():
                img_name = Path(local_path).name
            else:
                url = item['url']
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                img_name = f"{timestamp}_{hashlib.md5(url.encode()).hexdigest()}.webp"
                img_path = data_dir / str(group_id) / img_name
                if not img_path.exists():
                    await download_image(url, img_path)
                    _adjust_usage(_file_size(img_path))
                item['local_path'] = str(img_path)
            if img_name not in associated_images:
                associated_images.append(img_name)

# --- 核心消息处理器 ---
message_listener = on_message(priority=1, block=False)

@message_listener.handle()
async def handle_message(bot: Bot, event: GroupMessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return
    
    group_id = event.group_id
    user_id = event.user_id

    # Part 1: 消息预处理和缓存
    if group_id not in message_cache:
        message_cache[group_id] = deque(maxlen=CACHE_SIZE) 
    
    try:
        member_info = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        card = member_info.get('card') or member_info.get('nickname', str(user_id))
    except Exception:
        card = str(user_id)
    
    content = await parse_and_enrich_message(bot, group_id, event.message)
    
    msg_record = {
        'user_id': user_id,
        'card': card,
        'time': datetime.now().strftime('%Y%m%d %H:%M:%S'),
        'content': content,
        'message_id': event.message_id
    }
    
    message_cache[group_id].append(msg_record)

    # Part 2: 处理并更新所有活跃的追踪会话
    sessions_to_keep = []
    if group_id in active_at_tracking:
        for session in active_at_tracking[group_id]:
            # 查找与会话关联的内存中的记录
            record_to_update = next((r for r in at_records.get(group_id, []) if r['id'] == session['record_id']), None)
            
            if record_to_update:
                # 实时追加消息并保存
                await process_images_in_message(msg_record, group_id, record_to_update['associated_images'])
                record_to_update['messages'].append(msg_record)
                save_at_record(record_to_update)
                logger.debug(f"[谁AT我·追踪] Appended message to record {session['record_id']} and saved.")
                
                session['remaining'] -= 1
                if session['remaining'] > 0:
                    sessions_to_keep.append(session)
                else:
                    logger.info(f"[谁AT我·追踪] AT tracking session for record {session['record_id']} has finished.")
            else:
                 logger.warning(f"[谁AT我·追踪] Could not find record for active tracking session {session['record_id']}. Removing session.")

    if sessions_to_keep:
        active_at_tracking[group_id] = sessions_to_keep
    elif group_id in active_at_tracking:
        del active_at_tracking[group_id]

    # Part 3: 检查当前消息是否需要开启新的追踪会话
    # bot自己被at的不记录
    at_targets = [
        {'qq': item['qq'], 'card': item['card']}
        for item in content
        if item['type'] == 'at' and str(item['qq']) != str(bot.self_id)
    ]

    if at_targets and str(user_id) != bot.self_id:
        is_new_session_needed = True
        current_targets_qq = {str(t.get('qq')) for t in at_targets}

        if group_id in active_at_tracking:
            for session in active_at_tracking[group_id]:
                session_sender_id = str(session['sender_id'])
                session_targets_qq = {str(t.get('qq')) for t in session['targets']}
                if session_sender_id == str(user_id) and session_targets_qq == current_targets_qq:
                    is_new_session_needed = False
                    break
        
        if is_new_session_needed:
            # 寻找上下文
            cache_list = list(message_cache[group_id])
            first_sender_msg_index = -1
            for i, msg in enumerate(cache_list):
                if msg['user_id'] == user_id:
                    first_sender_msg_index = i
                    break
            
            start_index = max(0, first_sender_msg_index - 1) if first_sender_msg_index != -1 else 0
            initial_messages = cache_list[start_index:]
            
            record_id = f"{group_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{hashlib.md5(str(event.message_id).encode()).hexdigest()[:8]}"
            
            # 创建初始记录
            at_record = {
                'id': record_id,
                'group_id': group_id,
                'sender_id': user_id,
                'targets': at_targets,
                'start_time': datetime.now().strftime('%Y%m%d %H:%M:%S'),
                'messages': initial_messages,
                'associated_images': []
            }
            
            # 处理初始消息中的图片
            for msg in initial_messages:
                await process_images_in_message(msg, group_id, at_record['associated_images'])
            
            # 立即写入内存和文件
            if group_id not in at_records:
                at_records[group_id] = []
            at_records[group_id].append(at_record)
            save_at_record(at_record)
            logger.info(f"[谁AT我·追踪] New AT detected. Immediately created and saved record {record_id}.")
            
            # 创建新的追踪会话
            new_session = {
                'record_id': record_id,
                'sender_id': user_id,
                'targets': at_targets,
                'remaining': TRACKING_COUNT,
            }
            
            if group_id not in active_at_tracking:
                active_at_tracking[group_id] = []
            active_at_tracking[group_id].append(new_session)
            logger.info(f"[谁AT我·追踪] Started new AT tracking session for record {record_id}. Tracking next {TRACKING_COUNT} messages.")

    await enforce_disk_limit()

# --- 命令处理器 ---
who_at_me = on_command("谁at我", aliases={"谁艾特我", "谁@我", "xw谁艾特我", "xw谁@我", "xw谁at我", "小维谁艾特我", "小维谁@我", "小维谁at我"}, priority=5, block=True)

@who_at_me.handle()
async def handle_who_at_me(bot: Bot, event: GroupMessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return
    
    group_id = event.group_id
    user_id = str(event.user_id)
    
    group_at_records = at_records.get(group_id, [])
    
    valid_records = []
    cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)
    for record in group_at_records:
        try:
            record_time = datetime.strptime(record['start_time'], '%Y%m%d %H:%M:%S')
            if record_time >= cutoff_date:
                valid_records.append(record)
        except (ValueError, KeyError):
            logger.warning(f"[谁AT我·追踪] 记录 {record.get('id', 'N/A')} 的日期格式不正确，已跳过。")
            continue
    
    user_at_records = [
        record for record in valid_records
        if any(
            str(t.get('qq')) == user_id or t.get('qq') == 'all'
            for t in record.get('targets', [])
        )
    ]
    
    # 规则修改: 结束的session才决定最终要不要展示
    # 结束的定义是，发起者在TRACKING_COUNT条内又说话了，则展示到发起者最后一条的后一条
    finalized_user_records = []
    for record in user_at_records:
        sender_id = str(record['sender_id'])
        messages = record['messages']
        # 查找原始的@消息之后，发送者是否再次发言
        at_msg_index = -1
        for i, msg in enumerate(messages):
            if any(item.get('type') == 'at' for item in msg.get('content', [])) and str(msg.get('user_id')) == sender_id:
                at_msg_index = i
                break

        if at_msg_index == -1: continue

        last_sender_index_after_at = -1
        for i in range(at_msg_index + 1, len(messages)):
            if str(messages[i].get('user_id')) == sender_id:
                last_sender_index_after_at = i

        # 确定最终消息范围
        start_index = 0 # 记录本身就是从上下文开始的
        end_index = len(messages)
        if last_sender_index_after_at != -1:
            end_index = min(last_sender_index_after_at + 2, len(messages))

        finalized_record = record.copy()
        finalized_record['messages'] = record['messages'][start_index:end_index]
        finalized_user_records.append(finalized_record)

    recent_records = sorted(finalized_user_records, key=lambda x: x['start_time'], reverse=True)[:10]

    if not recent_records:
        await who_at_me.finish("最近没有人@你哦")
    
    try:
        images = []
        for record in recent_records: # Already sorted from newest to oldest
            img = await generate_chat_image(bot, record)
            if img:
                images.append(img)
            
        if not images:
            await who_at_me.finish("无法生成AT记录图片，请检查后台日志。")
            return
            
        if len(images) == 1:
            img_byte_arr = io.BytesIO()
            images[0].save(img_byte_arr, format='WEBP')
            await who_at_me.finish(MessageSegment.image(img_byte_arr))
        else:
            messages_to_forward = []
            for img in images:
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='WEBP')
                messages_to_forward.append(Message(MessageSegment.image(img_byte_arr)))
            
            await send_as_forward_msg(bot, event, messages_to_forward)
            
    except FinishedException:
        pass
    except Exception as e:
        logger.error(f"[谁AT我·追踪] 处理who_at_me命令失败: {e}")

async def send_as_forward_msg(bot: Bot, event: GroupMessageEvent, messages: List[Message]):
    """将消息列表作为转发消息发送"""
    nodes = [
        MessageSegment.node_custom(
            user_id=bot.self_id,
            nickname="小维", # 你可以改成你bot的名字
            content=msg
        ) for msg in messages
    ]
    try:
        await bot.send_group_forward_msg(group_id=event.group_id, messages=nodes)
    except Exception as e:
        logger.error(f"[谁AT我·追踪] 发送转发消息失败: {e}")

def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    lines = []
    for paragraph in text.split('\n'):
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

async def generate_chat_image(bot: Bot, record: Dict) -> Optional[Image.Image]:
    try:
        width, padding = 700, 20
        avatar_size, msg_padding = AVATAR_RENDER_SIZE, 15
        bubble_padding, max_bubble_width = 12, 450

        try:
            cjk_font = ImageFont.truetype(str(font_path), 18)
            small_cjk_font = ImageFont.truetype(str(font_path), 14)
            time_cjk_font = ImageFont.truetype(str(font_path), 12)
            
        except Exception as e:
            logger.warning(f"[谁AT我·追踪] 加载字体失败，将使用默认字体: {e}")
            cjk_font = small_cjk_font = time_cjk_font = ImageFont.load_default()
        
        img = Image.new('RGB', (width, 20000), color='#f5f5f5')
        draw = ImageDraw.Draw(img)
        
        draw.rectangle([0, 0, width, 50], fill='#4a90e2')
        title = f"AT记录 by 小维151 - {record.get('start_time', '')}" # 这里的名字留着，不过分吧
        draw.text((padding, 15), title, font=cjk_font, fill='#ffffff')
        
        current_y = 65
        
        messages = record.get('messages', [])
        for msg in messages:
            user_id = str(msg.get('user_id', ''))
            
            avatar_x, avatar_y = padding, current_y
            
            avatar_img = await get_user_avatar(user_id)
            if avatar_img:
                avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
                mask = Image.new('L', (avatar_size, avatar_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse([0, 0, avatar_size, avatar_size], fill=255)
                img.paste(avatar_img, (avatar_x, avatar_y), mask)
            
            content_x = avatar_x + avatar_size + msg_padding
            content_y = current_y
            
            card = msg.get('card', 'Unknown')
            draw.text((content_x, content_y), card, font=small_cjk_font, fill='#666666')
            content_y += 22
            
            inner_content_y = content_y

            for item in msg.get('content', []):
                item_spacing = 8

                if item['type'] == 'text':
                    wrapped_text = wrap_text(item['content'], cjk_font, max_bubble_width)
                    bbox = draw.multiline_textbbox((0, 0), wrapped_text, font=cjk_font, spacing=5)
                    text_width = bbox[2] - bbox[0]
                    text_height = bbox[3] - bbox[1]
                    
                    bubble_rect = (
                        content_x, inner_content_y,
                        content_x + text_width + bubble_padding * 2,
                        inner_content_y + text_height + bubble_padding * 2
                    )
                    draw.rounded_rectangle(bubble_rect, radius=10, fill='#ffffff')
                    draw.multiline_text(
                        (content_x + bubble_padding, inner_content_y + bubble_padding),
                        wrapped_text, font=cjk_font, fill='#333333', spacing=5
                    )
                    inner_content_y += text_height + bubble_padding * 2 + item_spacing

                elif item['type'] == 'image':
                    local_path = item.get('local_path')
                    try:
                        if not (local_path and Path(local_path).exists()): raise FileNotFoundError
                        with Image.open(local_path) as img_content:
                            img_content.thumbnail(IMAGE_MAX_SIZE, Image.Resampling.LANCZOS)
                            img.paste(img_content, (content_x, inner_content_y))
                            inner_content_y += img_content.height + item_spacing
                    except Exception:
                        placeholder_w, placeholder_h = 150, 100
                        draw.rounded_rectangle(
                            (content_x, inner_content_y, content_x + placeholder_w, inner_content_y + placeholder_h),
                            radius=10, fill='#e0e0e0'
                        )
                        draw.text((content_x + 55, inner_content_y + 40), "[图片]", font=cjk_font, fill='#999999')
                        inner_content_y += placeholder_h + item_spacing

                elif item['type'] == 'at':
                    at_text = f"@{item.get('card', item.get('qq', ''))}"
                    bbox = draw.textbbox((0,0), at_text, font=cjk_font)
                    text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    
                    at_rect = (
                        content_x, inner_content_y,
                        content_x + text_width + bubble_padding, inner_content_y + text_height + bubble_padding
                    )
                    draw.rounded_rectangle(at_rect, radius=8, fill='#e3f2fd')
                    draw.text(
                        (content_x + bubble_padding / 2, inner_content_y + bubble_padding / 2),
                        at_text, font=small_cjk_font, fill='#1976d2'
                    )
                    inner_content_y += text_height + bubble_padding + item_spacing
            
            avatar_bottom = avatar_y + avatar_size
            content_bottom = inner_content_y
            
            time_str = msg.get('time', '').split(' ')[1] if ' ' in msg.get('time', '') else ''
            if time_str:
                draw.text((content_x, content_bottom), time_str, font=time_cjk_font, fill='#999999')
                content_bottom += 15
            
            current_y = max(avatar_bottom, content_bottom) + msg_padding

        final_height = current_y + padding
        img = img.crop((0, 0, width, final_height))
        return img
    
    except FinishedException:
        pass
    
    except Exception as e:
        logger.error(f"[谁AT我·追踪] 生成聊天记录图片失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None