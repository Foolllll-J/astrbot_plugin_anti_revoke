import asyncio
import time
import traceback
import json
from pathlib import Path
from typing import List, Dict
import aiohttp
import os
import shutil

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.api import message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.platform import MessageType
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType


def get_private_unified_msg_origin(user_id: str, platform: str = "aiocqhttp") -> str:
    return f"{platform}:FriendMessage:{user_id}"

async def delayed_delete(delay: int, path: Path):
    await asyncio.sleep(delay)
    try:
        path.unlink(missing_ok=True)
        logger.debug(f"[AntiRevoke] 缓存清理：已删除过期文件 {path.name}")
    except Exception:
        logger.error(f"[AntiRevoke] 删除文件失败 ({path}): {traceback.format_exc()}")

async def _cleanup_local_files(file_paths: List[str]):
    if not file_paths: return
    await asyncio.sleep(1)
    for abs_path in file_paths:
        try:
            os.remove(abs_path)
            logger.debug(f"[AntiRevoke] 🗑️ 已清理本地文件: {os.path.basename(abs_path)}")
        except Exception as e:
            logger.error(f"[AntiRevoke] ❌ 清理本地文件失败 ({abs_path}): {e}")

def get_value(obj, key, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:
        return default

def _serialize_components(components: list) -> List[Dict]:
    serialized_list = []
    for comp in components:
        try:
            comp_dict = {k: v for k, v in comp.__dict__.items() if not k.startswith('_')}
            comp_type_name = getattr(comp.type, 'name', 'unknown')
            comp_dict['type'] = comp_type_name
            serialized_list.append(comp_dict)
        except:
            serialized_list.append({"type": "Unknown", "data": f"<{str(comp)}>"})
    return serialized_list

def _deserialize_components(comp_dicts: List[Dict]) -> List:
    components = []
    COMPONENT_MAP = {
        'Plain': Comp.Plain,
        'Text': Comp.Plain,
        'Image': Comp.Image,
        'Face': Comp.Face,
        'At': Comp.At,
        'Video': Comp.Video,
        'File': Comp.File,
        'Json': Comp.Json,
    }
    for comp_dict in comp_dicts:
        data_to_construct = comp_dict.copy()
        comp_type_name = data_to_construct.pop('type', None)

        if not comp_type_name:
            logger.warning(f"[AntiRevoke] 反序列化时遇到缺少类型的组件字典，已跳过。")
            continue
        
        cls = COMPONENT_MAP.get(comp_type_name)
        if cls:
            try:
                if 'file_' in data_to_construct:
                    data_to_construct['file'] = data_to_construct.pop('file_')
                
                components.append(cls(**data_to_construct))
            except Exception as e:
                logger.error(f"[AntiRevoke] 反序列化组件 {comp_type_name} 失败: {e}")
        else:
            if comp_type_name != 'Forward':
                logger.warning(f"[AntiRevoke] 反序列化时遇到未知组件类型 '{comp_type_name}'，已跳过。")
    return components


async def _download_and_cache_image(session: aiohttp.ClientSession, component: Comp.Image, temp_path: Path) -> str:
    image_url = getattr(component, 'url', None)
    if not image_url: return None
    file_extension = '.jpg'
    if image_url.lower().endswith('.png'): file_extension = '.png'
    file_name = f"forward_{int(time.time() * 1000)}{file_extension}"
    temp_file_path = temp_path / file_name
    try:
        headers = {'User-Agent': 'Mozilla/5.0 ...', 'Referer': 'https://qzone.qq.com/'}
        async with session.get(image_url, headers=headers, timeout=15) as response:
            response.raise_for_status()
            content_type = response.headers.get('Content-Type', '').lower()
            if 'image' not in content_type and 'octet-stream' not in content_type:
                logger.warning(f"[AntiRevoke] 下载 URL 返回类型非图片: {content_type}"); return None
            image_bytes = await response.read()
            with open(temp_file_path, 'wb') as f: f.write(image_bytes)
        logger.info(f"[AntiRevoke] 图片成功缓存到本地: {temp_file_path.name}")
        return str(temp_file_path.absolute())
    except Exception as e:
        logger.error(f"[AntiRevoke] ❌ 图片下载或保存失败 ({image_url}): {e}")
        if temp_file_path.exists(): os.remove(temp_file_path)
        return None

async def _process_component_and_get_gocq_part(
    comp, session: aiohttp.ClientSession, temp_path: Path, local_files_to_cleanup: List[str], local_file_map: Dict = None
) -> List[Dict]:
    gocq_parts = []
    comp_type_name = getattr(comp.type, 'name', 'unknown')
    if comp_type_name in ['Plain', 'Text']:
        text = getattr(comp, 'text', '')
        if text: gocq_parts.append({"type": "text", "data": {"text": text}})
    elif comp_type_name == 'Face':
        face_id = getattr(comp, 'id', None)
        if face_id is not None: gocq_parts.append({"type": "face", "data": {"id": int(face_id)}})
    elif comp_type_name == 'At':
        qq = getattr(comp, 'qq', '未知QQ')
        name = getattr(comp, 'name', f'@{qq}')
        at_text = f"@{name}({qq})"
        gocq_parts.append({"type": "text", "data": {"text": at_text}})
    elif comp_type_name == 'Image':
        local_path = await _download_and_cache_image(session, comp, temp_path)
        if local_path:
            local_files_to_cleanup.append(local_path)
            gocq_parts.append({"type": "image", "data": {"file": local_path}})
        else:
            gocq_parts.append({"type": "text", "data": {"text": "[图片转发失败]"}})
    elif comp_type_name == 'Video':
        cached_video_path_str = getattr(comp, 'file', None)
        if cached_video_path_str and cached_video_path_str.startswith('[视频过大未缓存:'):
            gocq_parts.append({"type": "text", "data": {"text": cached_video_path_str}})
        elif cached_video_path_str and Path(cached_video_path_str).exists():
            absolute_path = str(Path(cached_video_path_str).absolute())
            logger.info(f"[AntiRevoke] 准备发送已缓存的视频，路径: {absolute_path}")
            gocq_parts.append({"type": "video", "data": {"file": f"file:///{absolute_path}"}})
        else:
            logger.error(f"[AntiRevoke] ❌ 准备发送视频时失败：缓存的视频文件已丢失，路径: {cached_video_path_str}")
            gocq_parts.append({"type": "text", "data": {"text": f"[错误：撤回的视频文件已丢失]"}})
    elif comp_type_name == 'File':
        unique_key = getattr(comp, 'url', None)
        cached_file_path_str = local_file_map.get(unique_key) if local_file_map and unique_key else None
        
        original_filename = None
        if cached_file_path_str:
            if cached_file_path_str.startswith('[文件过大未缓存:'):
                gocq_parts.append({"type": "text", "data": {"text": cached_file_path_str}})
                return gocq_parts
            try:
                original_filename = Path(cached_file_path_str).name.split('_', 1)[1]
            except IndexError:
                original_filename = Path(cached_file_path_str).name

        if cached_file_path_str and Path(cached_file_path_str).exists():
            absolute_path = str(Path(cached_file_path_str).absolute())
            logger.info(f"[AntiRevoke] 准备发送已缓存的 File: {original_filename}，路径: {absolute_path}")
            gocq_parts.append({"type": "file", "data": {"file": f"file:///{absolute_path}", "name": original_filename}})
        else:
            logger.error(f"[AntiRevoke] ❌ 准备发送 File 时失败：缓存的文件已丢失。Key: {unique_key}")
            gocq_parts.append({"type": "text", "data": {"text": f"[错误：撤回的文件 '{original_filename or ''}' 已丢失]"}})
    elif comp_type_name == 'Json':
        json_data_str = getattr(comp, 'data', '{}')
        try:
            json.loads(json_data_str)
            gocq_part = {"type": "json", "data": {"data": json_data_str}}
            gocq_parts.append(gocq_part)
            logger.info("[AntiRevoke] ✅ Json 组件已成功打包。")
        except Exception as e:
            logger.error(f"[AntiRevoke] ❌ 处理 Json 组件失败，原始数据可能不是有效的 JSON: {e}")
            gocq_parts.append({"type": "text", "data": {"text": "[小程序转发失败，原始数据格式错误]"}})
            
    return gocq_parts

@register(
    "astrbot_plugin_anti_revoke", "Foolllll", "监控撤回插件", "1.0",
    "https://github.com/Foolllll-J/astrbot_plugin_anti_revoke",
)
class AntiRevoke(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.monitor_groups = [str(g) for g in config.get("monitor_groups", []) or []]
        self.target_receivers = [str(r) for r in config.get("target_receivers", []) or []]
        self.ignore_senders = [str(s) for s in config.get("ignore_senders", []) or []]
        self.instance_id = "AntiRevoke"
        self.cache_expiration_time = int(config.get("cache_expiration_time", 300))
        self.file_size_threshold_mb = int(config.get("file_size_threshold_mb", 300))
        self.forward_relay_group = str(config.get("forward_relay_group", "") or "")
        self.auto_recall_relay = config.get("auto_recall_relay", True)
        self.context = context
        self.temp_path = Path(StarTools.get_data_dir("astrbot_plugin_anti_revoke"))
        self.temp_path.mkdir(exist_ok=True)
        self.video_cache_path = self.temp_path / "videos"
        self.video_cache_path.mkdir(exist_ok=True)
        self.file_cache_path = self.temp_path / "files"
        self.file_cache_path.mkdir(exist_ok=True)
        self.forward_msg_map = {}
        self._cleanup_cache_on_startup()
    
    async def _download_video_from_url(self, url: str, save_path: Path) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=120) as response:
                    if response.status == 200:
                        content = await response.read()
                        with open(save_path, 'wb') as f:
                            f.write(content)
                        return True
                    else:
                        logger.error(f"[{self.instance_id}] 视频下载失败，HTTP 状态码: {response.status}")
                        return False
        except Exception as e:
            logger.error(f"[{self.instance_id}] 视频下载过程中发生异常: {e}\n{traceback.format_exc()}")
            return False

    def _cleanup_cache_on_startup(self):
        now = time.time()
        expired_count = 0
        for cache_dir in [self.video_cache_path, self.file_cache_path, self.temp_path]:
             for file in cache_dir.glob("*"):
                 if file.is_dir(): continue
                 try:
                     if now - file.stat().st_mtime > self.cache_expiration_time:
                         file.unlink(missing_ok=True)
                         expired_count += 1
                 except Exception:
                     continue
        logger.info(f"[{self.instance_id}] 缓存清理完成，移除了 {expired_count} 个过期文件。")

    async def _auto_recall_relay_msg(self, client, relay_msg_id: int):
        """自动撤回中转群的消息"""
        await asyncio.sleep(self.cache_expiration_time)
        try:
            await client.api.call_action("delete_msg", message_id=relay_msg_id)
            logger.debug(f"[{self.instance_id}] 已自动撤回中转群消息 ID: {relay_msg_id}")
        except Exception as e:
            logger.error(f"[{self.instance_id}] 自动撤回中转群消息失败 (ID: {relay_msg_id}): {e}")

    async def terminate(self):
        logger.info(f"[{self.instance_id}] 插件已卸载/重载。")
        
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def handle_message_cache(self, event: AstrMessageEvent):
        group_id = str(event.get_group_id())
        message_id = str(event.message_obj.message_id)
        if event.get_message_type() != MessageType.GROUP_MESSAGE or group_id not in self.monitor_groups:
            return None
        
        relay_info = None
        try:
            # 检查是否是合并转发消息
            raw_message = event.message_obj.raw_message
            message_list = raw_message.get("message", [])
            is_forward = (message_list and len(message_list) > 0 and message_list[0].get("type") == "forward")
            
            if is_forward and self.forward_relay_group:
                # 转发到中转群
                logger.info(f"[{self.instance_id}] 检测到合并转发消息，准备转发到中转群 {self.forward_relay_group}，原消息ID: {message_id}")
                try:
                    client = event.bot
                    relay_result = await client.api.call_action(
                        "forward_group_single_msg",
                        group_id=int(self.forward_relay_group),
                        message_id=message_id
                    )
                    
                    # 处理不同的返回类型
                    relay_msg_id = None
                    if isinstance(relay_result, dict):
                        relay_msg_id = relay_result.get("message_id")
                    elif isinstance(relay_result, int):
                        relay_msg_id = relay_result
                    
                    # 如果没有获取到 message_id，通过查询群历史消息获取
                    if not relay_msg_id:
                        logger.debug(f"[{self.instance_id}] API 未返回消息ID，尝试通过查询群历史消息获取...")
                        await asyncio.sleep(1)  # 等待1秒确保消息已发送
                        
                        try:
                            # 尝试获取自身ID以过滤消息
                            self_id = None
                            try:
                                login_info = await client.api.call_action("get_login_info")
                                self_id = str(login_info.get("user_id"))
                            except Exception:
                                pass

                            target_timestamp = event.message_obj.timestamp
                            found_msg = None
                            next_seq = 0
                            
                            # 循环获取历史消息，直到找到或超出时间范围
                            for _ in range(5): # 最多尝试5次分页
                                history_result = await client.api.call_action(
                                    "get_group_msg_history",
                                    group_id=int(self.forward_relay_group),
                                    message_seq=next_seq,
                                    count=20
                                )
                                
                                messages = []
                                if isinstance(history_result, dict):
                                    messages = history_result.get("data", {}).get("messages", [])
                                    if not messages:
                                        messages = history_result.get("messages", [])
                                
                                if not messages:
                                    break
                                    
                                # 倒序遍历（从新到旧）
                                for msg in reversed(messages):
                                    # 检查发送者
                                    msg_sender_id = str(msg.get("sender", {}).get("user_id", ""))
                                    if not msg_sender_id:
                                        msg_sender_id = str(msg.get("user_id", ""))
                                        
                                    if self_id and msg_sender_id != self_id:
                                        continue
                                        
                                    # 检查时间 (精确匹配，已确认)
                                    msg_time = int(msg.get("time", 0))
                                    if msg_time == target_timestamp:
                                        found_msg = msg
                                        break
                                
                                if found_msg:
                                    break
                                    
                                # 准备下一次分页
                                oldest_msg = messages[0]
                                next_seq = oldest_msg.get("message_seq")
                                oldest_time = int(oldest_msg.get("time", 0))
                                
                                # 如果获取到的最旧消息时间已经超过缓存过期时间（相对于目标时间），则停止搜索
                                # 这里假设目标消息不会比当前搜索到的最旧消息还旧太多
                                if target_timestamp - oldest_time > self.cache_expiration_time:
                                    logger.warning(f"[{self.instance_id}] 搜索范围已超过缓存过期时间，停止搜索。")
                                    break
                                    
                                if next_seq == 0: # 防止死循环
                                    break

                            if found_msg:
                                relay_msg_id = found_msg.get("message_id")
                                relay_msg_time = found_msg.get("time")
                                logger.debug(f"[{self.instance_id}] 通过时间戳匹配获取到中转消息ID: {relay_msg_id}")
                            else:
                                logger.warning(f"[{self.instance_id}] 未在历史消息中找到匹配的机器人发送记录")
                                
                        except Exception as e:
                            logger.error(f"[{self.instance_id}] 查询历史消息失败: {e}")
                    
                    if relay_msg_id:
                        # 保存映射关系: 原消息ID -> 中转群消息ID
                        relay_info = {
                            "relay_msg_id": relay_msg_id,
                            "sender_id": event.get_sender_id(),
                            "timestamp": event.message_obj.timestamp,
                            "relay_timestamp": relay_msg_time, # 记录中转消息的实际时间戳
                            "group_id": group_id
                        }
                        self.forward_msg_map[message_id] = relay_info
                        logger.info(f"[{self.instance_id}] 合并转发成功，已记录映射关系")
                        
                        # 设置自动撤回任务
                        if self.auto_recall_relay:
                            logger.debug(f"[{self.instance_id}] 启动自动撤回任务，将在 {self.cache_expiration_time} 秒后撤回中转消息")
                            asyncio.create_task(self._auto_recall_relay_msg(client, relay_msg_id))
                    else:
                        logger.warning(f"[{self.instance_id}] 无法获取中转消息ID，该消息的撤回检测将不可用")
                    
                except Exception as e:
                    logger.error(f"[{self.instance_id}] ❌ 转发合并消息到中转群失败: {e}\n{traceback.format_exc()}")
            
            message_obj = event.get_messages()
            timestamp_ms = int(time.time() * 1000)
            components = message_obj.components if isinstance(message_obj, MessageChain) else message_obj if isinstance(message_obj, list) else []
            components = [comp for comp in components if getattr(comp.type, 'name', 'unknown') != 'Reply']
            if not components: return None

            raw_file_names = []
            raw_file_sizes = {}
            raw_video_sizes = {}
            try:
                message_list = raw_message.get("message", [])
                for segment in message_list:
                    if segment.get("type") == "file":
                        file_name = segment.get("data", {}).get("file")
                        file_size = segment.get("data", {}).get("file_size")
                        if file_name:
                            raw_file_names.append(file_name)
                        if file_size:
                            try:
                                raw_file_sizes[file_name] = int(file_size) if isinstance(file_size, str) else file_size
                            except ValueError:
                                logger.warning(f"[AntiRevoke] 无法解析文件大小: {file_size}")
                    elif segment.get("type") == "video":
                        file_id = segment.get("data", {}).get("file")
                        file_size = segment.get("data", {}).get("file_size")
                        if file_id and file_size:
                            try:
                                raw_video_sizes[file_id] = int(file_size) if isinstance(file_size, str) else file_size
                            except ValueError:
                                logger.warning(f"[AntiRevoke] 无法解析视频大小: {file_size}")
            except Exception as e:
                logger.warning(f"[AntiRevoke] 解析 raw_message 失败: {e}")
            
            local_file_map = {}
            has_downloadable_content = any(getattr(comp.type, 'name', '') in ['Video', 'File'] for comp in components)

            if has_downloadable_content:
                client = event.bot
                for comp in components:
                    comp_type_name = getattr(comp.type, 'name', 'unknown')
                    
                    if comp_type_name == 'Video':
                        file_id = getattr(comp, 'file', None)
                        if not file_id: continue
                        
                        video_size = raw_video_sizes.get(file_id)
                        if video_size and self.file_size_threshold_mb > 0:
                            video_size_mb = video_size / (1024 * 1024)
                            if video_size_mb > self.file_size_threshold_mb:
                                logger.info(f"[{self.instance_id}] 视频大小 ({video_size_mb:.2f} MB) 超过阈值 ({self.file_size_threshold_mb} MB)，跳过缓存。")
                                setattr(comp, 'file', f"[视频过大未缓存: {video_size_mb:.2f} MB]")
                                continue
                        
                        try:
                            ret = await client.api.call_action('get_file', **{"file_id": file_id})
                            download_url = ret.get('url')
                            if not download_url:
                                setattr(comp, 'file', "Error: API did not return a URL.")
                                continue
                            
                            file_size_from_api = ret.get('file_size')
                            if file_size_from_api and self.file_size_threshold_mb > 0:
                                try:
                                    file_size_int = int(file_size_from_api) if isinstance(file_size_from_api, str) else file_size_from_api
                                    api_size_mb = file_size_int / (1024 * 1024)
                                    if api_size_mb > self.file_size_threshold_mb:
                                        logger.info(f"[{self.instance_id}] 视频大小 ({api_size_mb:.2f} MB) 超过阈值 ({self.file_size_threshold_mb} MB)，跳过缓存。")
                                        setattr(comp, 'file', f"[视频过大未缓存: {api_size_mb:.2f} MB]")
                                        continue
                                except (ValueError, TypeError) as e:
                                    logger.warning(f"[{self.instance_id}] 无法解析API返回的文件大小: {file_size_from_api}")
                            
                            original_filename = getattr(comp, 'name', file_id.split('/')[-1])
                            if not original_filename or len(original_filename) < 5:
                                original_filename = f"{timestamp_ms}.mp4"
                            
                            dest_path = self.video_cache_path / f"{timestamp_ms}_{original_filename}"
                            if await self._download_video_from_url(download_url, dest_path):
                                setattr(comp, 'file', str(dest_path.absolute()))
                                asyncio.create_task(delayed_delete(self.cache_expiration_time, dest_path))
                            else:
                                setattr(comp, 'file', f"Error: Download failed from {download_url}")
                        except Exception as e:
                            logger.error(f"[{self.instance_id}] ❌ 处理视频缓存时发生错误: {e}\n{traceback.format_exc()}")
                            setattr(comp, 'file', "Error: Exception during cache process.")

                    elif comp_type_name == 'File':
                        try:
                            original_filename = None
                            if raw_file_names:
                                original_filename = raw_file_names[0]
                            
                            file_size = raw_file_sizes.get(original_filename) if original_filename else None
                            if file_size and self.file_size_threshold_mb > 0:
                                file_size_mb = file_size / (1024 * 1024)
                                if file_size_mb > self.file_size_threshold_mb:
                                    logger.info(f"[{self.instance_id}] 文件 '{original_filename}' 大小 ({file_size_mb:.2f} MB) 超过阈值 ({self.file_size_threshold_mb} MB)，跳过缓存。")
                                    unique_key = getattr(comp, 'url', None)
                                    if unique_key:
                                        local_file_map[unique_key] = f"[文件过大未缓存: {file_size_mb:.2f} MB, 文件名: {original_filename}]"
                                    if raw_file_names:
                                        raw_file_names.pop(0)
                                    continue
                            
                            temp_file_path = await comp.get_file()
                            if not temp_file_path or not os.path.exists(temp_file_path):
                                logger.error(f"[{self.instance_id}] [File处理] ❌ 框架未能提供有效的临时文件路径。")
                                continue

                            if not original_filename and raw_file_names:
                                original_filename = raw_file_names.pop(0)
                            
                            if not original_filename:
                                original_filename = getattr(comp, 'name', Path(temp_file_path).name)
                                logger.warning(f"[AntiRevoke] [File处理] raw_message 中无可用文件名，回退为: {original_filename}")

                            if not original_filename or original_filename == Path(temp_file_path).name:
                                original_filename = f"未知文件_{timestamp_ms}.dat"

                            permanent_path = self.file_cache_path / f"{timestamp_ms}_{original_filename}"
                            shutil.copy(temp_file_path, permanent_path)

                            unique_key = getattr(comp, 'url', None)
                            if unique_key:
                                local_file_map[unique_key] = str(permanent_path)
                                asyncio.create_task(delayed_delete(self.cache_expiration_time, permanent_path))
                            else:
                                logger.warning(f"[{self.instance_id}] ⚠️ File 组件缺少 URL，无法为其创建映射。")

                        except Exception as e:
                            logger.error(f"[{self.instance_id}] ❌ 处理 File 缓存时发生错误: {e}\n{traceback.format_exc()}")
            
            file_path = self.temp_path / f'{timestamp_ms}_{group_id}_{message_id}.json'
            with open(file_path, 'w', encoding='utf-8') as f:
                data_to_save = {
                    "components": _serialize_components(components),
                    "sender_id": event.get_sender_id(),
                    "timestamp": event.message_obj.timestamp,
                    "local_file_map": local_file_map,
                    "relay_info": relay_info
                }
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)

            asyncio.create_task(delayed_delete(self.cache_expiration_time, file_path))
            logger.debug(f"[{self.instance_id}] 缓存消息成功 (ID: {message_id})，群: {group_id}")
        except Exception as e:
            logger.error(f"[{self.instance_id}] 缓存消息失败 (ID: {message_id})：{e}\n{traceback.format_exc()}")
        return None

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def handle_recall_event(self, event: AstrMessageEvent):
        raw_message = event.message_obj.raw_message
        post_type = get_value(raw_message, "post_type")
        if post_type == "notice" and get_value(raw_message, "notice_type") == "group_recall":
            group_id = str(get_value(raw_message, "group_id"))
            message_id = str(get_value(raw_message, "message_id"))
            if group_id not in self.monitor_groups or not message_id: return None
            
            # 尝试获取消息数据（优先内存映射，其次本地缓存）
            relay_info = self.forward_msg_map.pop(message_id, None)
            cached_data = None
            file_path = next(self.temp_path.glob(f"*_{group_id}_{message_id}.json"), None)
            
            if not relay_info and file_path and file_path.exists():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        cached_data = json.load(f)
                    relay_info = cached_data.get("relay_info")
                except Exception as e:
                    logger.warning(f"[{self.instance_id}] 读取本地缓存失败: {e}")

            # 如果是合并转发消息（存在 relay_info）
            if relay_info:
                logger.info(f"[{self.instance_id}] 检测到合并转发消息被撤回，原消息ID: {message_id}")
                sender_id = relay_info["sender_id"]
                
                if str(sender_id) in self.ignore_senders:
                    logger.debug(f"[{self.instance_id}] 发送者 {sender_id} 在忽略列表中，跳过处理")
                    return None
                
                relay_msg_id = relay_info["relay_msg_id"]
                timestamp = relay_info["timestamp"]
                
                try:
                    client = event.bot
                    
                    # 获取群名和用户名
                    group_name, member_nickname = str(group_id), str(sender_id)
                    try:
                        group_info = await client.api.call_action('get_group_info', group_id=int(group_id))
                        group_name = group_info.get('group_name', group_name)
                    except: pass
                    try:
                        member_info = await client.api.call_action('get_group_member_info', group_id=int(group_id), user_id=int(sender_id))
                        card, nickname = member_info.get('card', ''), member_info.get('nickname', '')
                        member_nickname = card or nickname or member_nickname
                    except: pass
                    
                    message_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) if timestamp else "未知时间"
                    logger.info(f"[{self.instance_id}] 合并转发撤回 - 群: {group_name}, 发送者: {member_nickname}")
                    
                    # 向每个接收者转发
                    for index, target_id in enumerate(self.target_receivers, 1):
                        target_id_str = str(target_id)
                        
                        # 先发送通知
                        notification_text = f"【撤回提醒】\n群聊：{group_name} ({group_id})\n发送者：{member_nickname} ({sender_id})\n时间：{message_time_str}\n--------------------\n以下是撤回的聊天记录："
                        try:
                            await client.send_private_msg(user_id=int(target_id_str), message=notification_text)
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            logger.error(f"[{self.instance_id}] 发送合并转发通知失败到 {target_id_str}: {e}")
                            continue
                        
                        # 从中转群转发到接收者
                        try:
                            forward_result = await client.api.call_action(
                                "forward_friend_single_msg",
                                user_id=int(target_id_str),
                                message_id=relay_msg_id
                            )
                            logger.debug(f"[{self.instance_id}] 已将合并转发消息转发给 {target_id_str}")
                        except Exception as e:
                            logger.error(f"[{self.instance_id}] 转发合并消息失败到 {target_id_str}: {e}")
                    
                    # 立即撤回中转群的消息
                    if self.auto_recall_relay:
                        try:
                            await client.api.call_action("delete_msg", message_id=relay_msg_id)
                            logger.debug(f"[{self.instance_id}] 已撤回中转群消息 ID: {relay_msg_id}")
                        except Exception as e:
                            logger.error(f"[{self.instance_id}] 撤回中转群消息失败: {e}")
                    
                except Exception as e:
                    logger.error(f"[{self.instance_id}] 处理合并转发撤回失败: {e}")
                
                return None
            
            if cached_data or (file_path and file_path.exists()):
                local_files_to_cleanup = [] 
                try:
                    if not cached_data:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            cached_data = json.load(f)
                    
                    sender_id = cached_data["sender_id"]
                    local_file_map = cached_data.get("local_file_map", {})
                    if str(sender_id) in self.ignore_senders: return None
                    
                    cached_components_data = cached_data.get("components", [])
                    
                    unsupported_types = set()
                    supported_types_set = {'Plain', 'Text', 'Image', 'Face', 'At', 'Video', 'Json', 'File'}
                    for comp_dict in cached_components_data:
                        comp_type_name = comp_dict.get('type')
                        if comp_type_name not in supported_types_set:
                            unsupported_types.add(comp_type_name)
                    
                    components = _deserialize_components(cached_components_data)

                    timestamp = cached_data.get("timestamp")
                    message_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) if timestamp else "未知时间"
                    client = event.bot
                    
                    group_name, member_nickname = str(group_id), str(sender_id)
                    try:
                        group_info = await client.api.call_action('get_group_info', group_id=int(group_id)); group_name = group_info.get('group_name', group_name)
                    except: pass
                    try:
                        member_info = await client.api.call_action('get_group_member_info', group_id=int(group_id), user_id=int(sender_id)); card, nickname = member_info.get('card', ''), member_info.get('nickname', ''); member_nickname = card or nickname or member_nickname
                    except: pass

                    logger.info(f"[{self.instance_id}] 发现撤回。群: {group_name} ({group_id}), 发送者: {member_nickname} ({sender_id})")
                    
                    special_components = [comp for comp in components if getattr(comp.type, 'name', 'unknown') in ['Video', 'Json', 'File']]
                    other_components = [comp for comp in components if getattr(comp.type, 'name', 'unknown') not in ['Video', 'Json', 'File']]
                    
                    async with aiohttp.ClientSession() as session:
                        for target_id in self.target_receivers:
                            target_id_str = str(target_id)
                            
                            notification_prefix = f"【撤回提醒】\n群聊：{group_name} ({group_id})\n发送者：{member_nickname} ({sender_id})\n时间：{message_time_str}"
                            warning_text = f"\n⚠️ 注意：包含不支持的组件：{', '.join(unsupported_types)}" if unsupported_types else ""
                            
                            if not special_components:
                                message_parts = []
                                for comp in other_components:
                                    converted_parts = await _process_component_and_get_gocq_part(comp, session, self.temp_path, local_files_to_cleanup, local_file_map)
                                    message_parts.extend(converted_parts)
                                
                                has_inserted_prefix, final_message_parts = False, []
                                for part in message_parts:
                                    if not has_inserted_prefix and (part['type'] in ['text', 'image', 'face']):
                                        final_message_parts.append({"type": "text", "data": {"text": f"{member_nickname}："}})
                                        has_inserted_prefix = True
                                        if part['type'] == 'text': final_message_parts[-1]['data']['text'] += part['data']['text']; continue
                                    final_message_parts.append(part)
                                
                                final_prefix_text = f"{notification_prefix}{warning_text}\n--------------------\n"
                                gocq_content_array = [{"type": "text", "data": {"text": final_prefix_text}}]
                                gocq_content_array.extend(final_message_parts)

                                if len(gocq_content_array) > 1 or warning_text:
                                    try:
                                        await client.send_private_msg(user_id=int(target_id_str), message=gocq_content_array)
                                    except Exception as e: logger.error(f"[{self.instance_id}] ❌ 合并消息转发失败到 {target_id_str}：{e}\n{traceback.format_exc()}")
                            else:
                                final_notification_text = f"{notification_prefix}{warning_text}\n--------------------\n内容将分条发送。"
                                try:
                                    await client.send_private_msg(user_id=int(target_id_str), message=final_notification_text)
                                except Exception as e: logger.error(f"[{self.instance_id}] ❌ 发送通知头失败到 {target_id_str}：{e}\n{traceback.format_exc}"); continue
                                
                                await asyncio.sleep(0.5)
                                
                                if other_components:
                                    message_parts = []
                                    for comp in other_components:
                                        converted_parts = await _process_component_and_get_gocq_part(comp, session, self.temp_path, local_files_to_cleanup, local_file_map)
                                        message_parts.extend(converted_parts)
                                    if message_parts:
                                        content_message = [{"type": "text", "data": {"text": f"{member_nickname}："}}]
                                        if message_parts and message_parts[0]['type'] == 'text':
                                            content_message[0]['data']['text'] += message_parts[0]['data']['text']
                                            content_message.extend(message_parts[1:])
                                        else:
                                            content_message.extend(message_parts)
                                        try:
                                            await client.send_private_msg(user_id=int(target_id_str), message=content_message)
                                        except Exception as e: logger.error(f"[{self.instance_id}] ❌ 发送非特殊内容失败到 {target_id_str}：{e}\n{traceback.format_exc()}")
                                
                                for comp in special_components:
                                    await asyncio.sleep(0.5)
                                    comp_type_name = getattr(comp.type, 'name', 'unknown')
                                    content_parts = await _process_component_and_get_gocq_part(comp, session, self.temp_path, local_files_to_cleanup, local_file_map)
                                    final_parts_to_send = content_parts
                                    if not other_components:
                                        prefix_part = [{"type": "text", "data": {"text": f"{member_nickname}："}}]
                                        final_parts_to_send = prefix_part + content_parts
                                    
                                    try:
                                        await client.send_private_msg(user_id=int(target_id_str), message=final_parts_to_send)
                                    except Exception as e: 
                                        logger.error(f"[{self.instance_id}] ❌ 发送特殊内容 ({comp_type_name}) 失败到 {target_id_str}：{e}\n{traceback.format_exc()}")
                
                finally:
                    if local_files_to_cleanup: asyncio.create_task(_cleanup_local_files(local_files_to_cleanup))
                    asyncio.create_task(delayed_delete(0, file_path))
            else:
                logger.warning(f"[{self.instance_id}] 找不到消息记录 (ID: {message_id})，可能已过期或未缓存。")
        return None
