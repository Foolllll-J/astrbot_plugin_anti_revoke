import asyncio
import time
import traceback
import json
import base64
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
    except Exception:
        logger.error(f"[AntiRevoke] 删除文件失败 ({path}): {traceback.format_exc()}")

async def _cleanup_local_files(file_paths: List[str]):
    if not file_paths: return
    await asyncio.sleep(1)
    for abs_path in file_paths:
        try:
            os.remove(abs_path)
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
        'Record': Comp.Record,
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
        name = getattr(comp, 'name', f'@{{{qq}}}')
        at_text = f"@{name}({qq})"
        gocq_parts.append({"type": "text", "data": {"text": at_text}})
    elif comp_type_name == 'Image':
        local_path = await _download_and_cache_image(session, comp, temp_path)
        if local_path:
            local_files_to_cleanup.append(local_path)
            try:
                with open(local_path, "rb") as image_file:
                    encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                gocq_parts.append({"type": "image", "data": {"file": f"base64://{encoded_string}"}})
            except Exception as e:
                logger.error(f"[AntiRevoke] ❌ 图片转Base64失败: {e}")
                # 降级重试：如果Base64失败，尝试使用文件路径
                gocq_parts.append({"type": "image", "data": {"file": f"file:///{local_path}"}})
        else:
            gocq_parts.append({"type": "text", "data": {"text": "[图片转发失败]"}})
    elif comp_type_name == 'Video':
        cached_video_path_str = getattr(comp, 'file', None)
        if cached_video_path_str and cached_video_path_str.startswith('[视频过大未缓存:'):
            gocq_parts.append({"type": "text", "data": {"text": cached_video_path_str}})
        elif cached_video_path_str and Path(cached_video_path_str).exists():
            absolute_path = str(Path(cached_video_path_str).absolute())
            gocq_parts.append({"type": "video", "data": {"file": f"file:///{absolute_path}"}})
        else:
            logger.error(f"[AntiRevoke] ❌ 准备发送视频时失败：缓存的视频文件已丢失，路径: {cached_video_path_str}")
            gocq_parts.append({"type": "text", "data": {"text": f"[错误：撤回的视频文件已丢失]"}})
    elif comp_type_name == 'Record':
        cached_voice_path_str = getattr(comp, 'file', None)
        if cached_voice_path_str and Path(cached_voice_path_str).exists():
            absolute_path = str(Path(cached_voice_path_str).absolute())
            try:
                with open(absolute_path, "rb") as voice_file:
                    encoded_string = base64.b64encode(voice_file.read()).decode('utf-8')
                gocq_parts.append({"type": "record", "data": {"file": f"base64://{encoded_string}"}})
            except Exception as e:
                logger.error(f"[AntiRevoke] ❌ 语音转Base64失败: {e}")
                # 降级重试：如果Base64失败，尝试使用文件路径
                gocq_parts.append({"type": "record", "data": {"file": f"file:///{absolute_path}"}})
        else:
            logger.error(f"[AntiRevoke] ❌ 准备发送语音时失败：缓存的语音文件已丢失，路径: {cached_voice_path_str}")
            gocq_parts.append({"type": "text", "data": {"text": f"[错误：撤回的语音文件已丢失]"}})
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
            gocq_parts.append({"type": "file", "data": {"file": f"file:///{absolute_path}", "name": original_filename}})
        else:
            logger.error(f"[AntiRevoke] ❌ 准备发送 File 时失败：缓存的文件已丢失。Key: {unique_key}")
            gocq_parts.append({"type": "text", "data": {"text": f"[错误：撤回的文件 '{original_filename or ''}' 已丢失]"}})
    elif comp_type_name == 'Forward':
        gocq_parts.append({"type": "text", "data": {"text": "[合并转发消息]"}})
    elif comp_type_name == 'Json':
        json_data = getattr(comp, 'data', '{}')
        if isinstance(json_data, dict):
            json_data_str = json.dumps(json_data, ensure_ascii=False)
        else:
            json_data_str = str(json_data)
            
        try:
            json.loads(json_data_str)
            gocq_part = {"type": "json", "data": {"data": json_data_str}}
            gocq_parts.append(gocq_part)
        except Exception as e:
            logger.error(f"[AntiRevoke] ❌ 处理 Json 组件失败，原始数据可能不是有效的 JSON: {e}")
            gocq_parts.append({"type": "text", "data": {"text": "[小程序转发失败，原始数据格式错误]"}})
            
    return gocq_parts

@register(
    "astrbot_plugin_anti_revoke", "Foolllll", "QQ 防撤回", "1.2.2",
    "https://github.com/Foolllll-J/astrbot_plugin_anti_revoke",
)
class AntiRevoke(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.monitor_groups = [str(g) for g in config.get("monitor_groups", []) or []]
        self.target_receivers = [str(r) for r in config.get("target_receivers", []) or []]
        self.target_groups = [str(g) for g in config.get("target_groups", []) or []]
        self.ignore_senders = [str(s) for s in config.get("ignore_senders", []) or []]
        self.instance_id = "AntiRevoke"
        self.cache_expiration_time = int(config.get("cache_expiration_time", 300))
        self.file_size_threshold_mb = int(config.get("file_size_threshold_mb", 300))
        self.forward_relay_group = str(config.get("forward_relay_group", "") or "")
        self.forward_to_self = config.get("forward_to_self", False)
        self.auto_recall_relay = config.get("auto_recall_relay", True)
        self.context = context
        self.temp_path = Path(StarTools.get_data_dir("astrbot_plugin_anti_revoke"))
        self.temp_path.mkdir(exist_ok=True)
        self.video_cache_path = self.temp_path / "videos"
        self.video_cache_path.mkdir(exist_ok=True)
        self.voice_cache_path = self.temp_path / "voices"
        self.voice_cache_path.mkdir(exist_ok=True)
        self.file_cache_path = self.temp_path / "files"
        self.file_cache_path.mkdir(exist_ok=True)
        self._cleanup_cache_on_startup()
        asyncio.create_task(self._cleanup_kv_data())
    
    async def _cleanup_kv_data(self):
        """清理 KV 存储中不再监控的群组配置"""
        kv_targets = await self.get_kv_data("forward_targets", {})
        if not kv_targets:
            return
        
        cleaned_targets = {}
        changed = False
        for group_id, targets in kv_targets.items():
            if group_id in self.monitor_groups:
                cleaned_targets[group_id] = targets
            else:
                changed = True
                logger.info(f"[{self.instance_id}] 清理已不再监控的群组转发配置: {group_id}")
        
        if changed:
            await self.put_kv_data("forward_targets", cleaned_targets)

    async def _get_targets_for_group(self, group_id: str) -> List[tuple]:
        """获取群组的转发目标配置，优先使用 KV 存储"""
        kv_targets = await self.get_kv_data("forward_targets", {})
        group_targets = kv_targets.get(group_id, [])
        
        if group_targets:
            targets = []
            for t in group_targets:
                if t.startswith("@"):
                    targets.append(("private", t[1:]))
                elif t.startswith("#"):
                    targets.append(("group", t[1:]))
            return targets
        else:
            return [("private", tid) for tid in self.target_receivers] + [("group", tid) for tid in self.target_groups]

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
        for cache_dir in [self.video_cache_path, self.voice_cache_path, self.file_cache_path, self.temp_path]:
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
        except Exception as e:
            logger.error(f"[{self.instance_id}] 自动撤回中转群消息失败 (ID: {relay_msg_id}): {e}")

    async def terminate(self):
        logger.info(f"[{self.instance_id}] 插件已卸载/重载。")
        
    @filter.command("撤回转发")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def add_forward(self, event: AstrMessageEvent, group_id: str, target: str):
        """设置撤回消息的转发目标。格式：撤回转发 群号 @私聊或#群聊"""
        if group_id not in self.monitor_groups:
            yield event.plain_result(f"❌ 群号 {group_id} 不在监控列表中，请先在配置中添加。")
            return
        
        if not (target.startswith("@") or target.startswith("#")):
            yield event.plain_result("❌ 目标会话格式错误。使用 @数字 表示私聊，#数字 表示群聊。")
            return
        
        if not target[1:].isdigit():
            yield event.plain_result("❌ 目标会话 ID 必须为数字。")
            return

        kv_targets = await self.get_kv_data("forward_targets", {})
        if group_id not in kv_targets:
            kv_targets[group_id] = []
        
        if target not in kv_targets[group_id]:
            kv_targets[group_id].append(target)
            await self.put_kv_data("forward_targets", kv_targets)
            yield event.plain_result(f"✅ 已添加转发目标: 群 {group_id} -> {target}")
        else:
            yield event.plain_result(f"ℹ️ 该转发目标已存在: 群 {group_id} -> {target}")

    @filter.command("取消撤回转发")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def remove_forward(self, event: AstrMessageEvent, group_id: str, target: str = None):
        """取消撤回消息的转发目标。格式：取消撤回转发 群号 [目标]"""
        kv_targets = await self.get_kv_data("forward_targets", {})
        if group_id not in kv_targets:
            yield event.plain_result(f"❌ 未找到群 {group_id} 的特定转发配置。")
            return
        
        if target:
            if target in kv_targets[group_id]:
                kv_targets[group_id].remove(target)
                if not kv_targets[group_id]:
                    del kv_targets[group_id]
                await self.put_kv_data("forward_targets", kv_targets)
                yield event.plain_result(f"✅ 已取消转发目标: 群 {group_id} -> {target}")
            else:
                yield event.plain_result(f"❌ 群 {group_id} 的配置中不包含目标 {target}。")
        else:
            del kv_targets[group_id]
            await self.put_kv_data("forward_targets", kv_targets)
            yield event.plain_result(f"✅ 已重置群 {group_id} 的所有特定转发目标。")

    @filter.command("查看撤回转发")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_forward(self, event: AstrMessageEvent):
        """查看所有自定义的撤回转发配置"""
        kv_targets = await self.get_kv_data("forward_targets", {})
        if not kv_targets:
            yield event.plain_result("ℹ️ 当前没有自定义的撤回转发配置。")
            return
        
        msg = "📋 自定义撤回转发配置：\n"
        for gid, targets in kv_targets.items():
            msg += f"群 {gid} -> {', '.join(targets)}\n"
        
        yield event.plain_result(msg.strip())

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def handle_message_cache(self, event: AstrMessageEvent):
        """处理消息缓存，包括合并转发消息的中转"""
        group_id = str(event.get_group_id())
        message_id = str(event.message_obj.message_id)
        if event.get_message_type() != MessageType.GROUP_MESSAGE or group_id not in self.monitor_groups:
            return None
        
        relay_info = None
        try:
            raw_message = event.message_obj.raw_message
            if not isinstance(raw_message, dict):
                raw_message = {}
            message_list = raw_message.get("message", [])
            
            is_forward = False
            if isinstance(message_list, list) and len(message_list) > 0:
                first_segment = message_list[0]
                if isinstance(first_segment, dict) and first_segment.get("type") == "forward":
                    is_forward = True
            
            if is_forward and self.forward_to_self:
                try:
                    client = event.bot
                    login_info = await client.api.call_action("get_login_info")
                    self_id = login_info.get("user_id")
                    if self_id:
                        logger.info(f"[{self.instance_id}] 检测到合并转发消息，准备转发给机器人自身 ({self_id})，原消息ID: {message_id}")
                        await client.api.call_action(
                            "forward_friend_single_msg",
                            user_id=int(self_id),
                            message_id=message_id
                        )
                        
                        relay_msg_id = None
                        await asyncio.sleep(1)
                        try:
                            history_result = await client.api.call_action(
                                "get_friend_msg_history",
                                user_id=int(self_id),
                                count=10
                            )
                            messages = []
                            if isinstance(history_result, dict):
                                messages = history_result.get("messages", []) or history_result.get("data", {}).get("messages", [])
                            
                            target_timestamp = event.message_obj.timestamp
                            for msg in reversed(messages):
                                msg_time = int(msg.get("time", 0))
                                if abs(msg_time - target_timestamp) <= 2:
                                    relay_msg_id = msg.get("message_id")
                                    break
                        except Exception as e:
                            logger.warning(f"[{self.instance_id}] 查询自身私聊历史消息失败: {e}")
                                
                        if relay_msg_id:
                            relay_info = {
                                "relay_msg_id": relay_msg_id,
                                "sender_id": event.get_sender_id(),
                                "timestamp": event.message_obj.timestamp,
                                "group_id": group_id,
                                "is_private_relay": True # 标记为私聊中转
                            }
                            logger.info(f"[{self.instance_id}] 转发给自身成功，已记录映射关系 (ID: {relay_msg_id})")
                except Exception as e:
                    logger.error(f"[{self.instance_id}] ❌ 转发合并消息给机器人自身失败: {e}")

            if not relay_info and is_forward and self.forward_relay_group:
                logger.info(f"[{self.instance_id}] 检测到合并转发消息，准备转发到中转群 {self.forward_relay_group}，原消息ID: {message_id}")
                try:
                    client = event.bot
                    await client.api.call_action(
                        "forward_group_single_msg",
                        group_id=int(self.forward_relay_group),
                        message_id=message_id
                    )
                    
                    relay_msg_id = None
                    logger.info(f"[{self.instance_id}] 转发到中转群完成，准备通过查询群历史消息获取 ID...")
                    await asyncio.sleep(1)
                    
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
                                await asyncio.sleep(1)
                                continue
                                
                            # 倒序遍历（从新到旧）
                            for msg in reversed(messages):
                                msg_sender_id = str(msg.get("sender", {}).get("user_id", ""))
                                if not msg_sender_id:
                                    msg_sender_id = str(msg.get("user_id", ""))
                                    
                                if self_id and msg_sender_id != self_id:
                                    continue
                                    
                                msg_time = int(msg.get("time", 0))
                                if abs(msg_time - target_timestamp) <= 1:
                                    found_msg = msg
                                    break
                            
                            if found_msg:
                                break
                                
                            # 准备下一次分页
                            oldest_msg = messages[0]
                            next_seq = oldest_msg.get("message_seq")
                            oldest_time = int(oldest_msg.get("time", 0))
                            
                            # 如果获取到的最旧消息时间已经超过缓存过期时间（相对于目标时间），则停止搜索
                            if target_timestamp - oldest_time > self.cache_expiration_time:
                                logger.warning(f"[{self.instance_id}] 搜索范围已超过缓存过期时间，停止搜索。")
                                break
                                
                            if next_seq == 0: # 防止死循环
                                break

                        if found_msg:
                            relay_msg_id = found_msg.get("message_id")
                            relay_msg_time = found_msg.get("time")
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
                        logger.info(f"[{self.instance_id}] 合并转发成功，已记录映射关系")
                        
                        # 设置自动撤回任务
                        if self.auto_recall_relay:
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
            raw_record_urls = {}
            try:
                if not isinstance(raw_message, dict):
                    raw_message = {}
                message_list = raw_message.get("message", [])
                if isinstance(message_list, list):
                    for segment in message_list:
                        if not isinstance(segment, dict):
                            continue
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
                        elif segment.get("type") == "record":
                            file_id = segment.get("data", {}).get("file")
                            url = segment.get("data", {}).get("url")
                            if file_id:
                                if url: raw_record_urls[file_id] = url
            except Exception as e:
                logger.warning(f"[AntiRevoke] 解析 raw_message 失败: {e}")
            
            local_file_map = {}
            has_downloadable_content = any(getattr(comp.type, 'name', '') in ['Video', 'Record', 'File'] for comp in components)

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

                    elif comp_type_name == 'Record':
                        file_id = getattr(comp, 'file', None)
                        if not file_id: continue
                        
                        try:
                            # 1. 尝试从组件属性或原始消息中获取 url 并下载
                            record_url = getattr(comp, 'url', None) or raw_record_urls.get(file_id)
                            if record_url:
                                try:
                                    original_suffix = '.amr'
                                    # 尝试从 url 或文件名推断后缀
                                    if getattr(comp, 'file', '').endswith('.slk'):
                                        original_suffix = '.slk'
                                    
                                    permanent_path = self.voice_cache_path / f"{timestamp_ms}{original_suffix}"
                                    
                                    headers = {'User-Agent': 'Mozilla/5.0 ...'}
                                    async with aiohttp.ClientSession() as session:
                                        async with session.get(record_url, headers=headers, timeout=15) as response:
                                            response.raise_for_status()
                                            voice_bytes = await response.read()
                                            with open(permanent_path, 'wb') as f:
                                                f.write(voice_bytes)
                                    
                                    os.chmod(permanent_path, 0o644)

                                    setattr(comp, 'file', str(permanent_path.absolute()))
                                    asyncio.create_task(delayed_delete(self.cache_expiration_time, permanent_path))
                                    continue
                                except Exception as e:
                                    logger.warning(f"[{self.instance_id}] [Record处理] 尝试通过 URL 下载失败: {e}，将尝试使用本地路径兜底。")
                            
                            # 2. 如果 URL 失败或不存在，尝试通过 API 获取本地路径并拷贝
                            ret = await client.api.call_action('get_file', **{"file_id": file_id})
                            local_path = ret.get('file')
                            
                            if not local_path or not os.path.exists(local_path):
                                logger.error(f"[{self.instance_id}] [Record处理] ❌ API未能提供有效的本地文件路径。返回: {ret}")
                                setattr(comp, 'file', "Error: API did not return a valid file path.")
                                continue
                            
                            original_suffix = Path(local_path).suffix or '.amr'
                            permanent_path = self.voice_cache_path / f"{timestamp_ms}{original_suffix}"
                            shutil.copy(local_path, permanent_path)
                            # ========== 添加权限设置 ==========
                            os.chmod(permanent_path, 0o644)  # 设置文件权限为 rw-r--r--
                            # ================================

                            setattr(comp, 'file', str(permanent_path.absolute()))
                            asyncio.create_task(delayed_delete(self.cache_expiration_time, permanent_path))
                            
                        except Exception as e:
                            logger.error(f"[{self.instance_id}] ❌ 处理 Record 缓存时发生错误: {e}\n{traceback.format_exc()}")
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
        except Exception as e:
            logger.error(f"[{self.instance_id}] 缓存消息失败 (ID: {message_id})：{e}\n{traceback.format_exc()}")
        return None

    def _create_recall_notification_header(self, group_name: str, group_id: str, member_nickname: str, sender_id: str, operator_nickname: str, operator_id: str, timestamp: int) -> str:
        """生成统一的撤回通知消息头"""
        message_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) if timestamp else "未知时间"
        if operator_id == sender_id:
            return f"【撤回提醒】\n群聊：{group_name} ({group_id})\n发送者：{member_nickname} ({sender_id})\n时间：{message_time_str}"
        else:
            return f"【撤回提醒】\n群聊：{group_name} ({group_id})\n发送者：{member_nickname} ({sender_id})\n操作者：{operator_nickname} ({operator_id})\n时间：{message_time_str}"

    async def _send_to_target(self, client, target_type: str, target_id_str: str, message):
        """按目标类型发送消息。"""
        if target_type == "private":
            await client.send_private_msg(user_id=int(target_id_str), message=message)
        else:
            await client.send_group_msg(group_id=int(target_id_str), message=message)

    def _message_to_plain_text(self, message) -> str:
        """将 go-cqhttp 消息数组压缩成便于兜底通知的纯文本。"""
        if isinstance(message, str):
            return message.strip()
        if not isinstance(message, list):
            return str(message).strip()

        type_map = {
            "image": "[图片]",
            "face": "[表情]",
            "video": "[视频]",
            "record": "[语音]",
            "file": "[文件]",
            "json": "[小程序]",
            "forward": "[合并转发]",
            "at": "[@]",
        }
        parts = []
        for part in message:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            data = part.get("data", {}) or {}
            if part_type == "text":
                text = str(data.get("text", "")).strip()
                if text:
                    parts.append(text)
            else:
                parts.append(type_map.get(part_type, f"[{part_type or '未知内容'}]"))

        merged = "\n".join(part for part in parts if part).strip()
        return merged

    async def _notify_send_failure(self, client, target_type: str, target_id_str: str, context: str, original_error: Exception, fallback_text: str):
        """发送失败后的文本兜底通知，尽量保证目标端能收到异常说明。"""
        original_trace = "".join(
            traceback.format_exception(type(original_error), original_error, original_error.__traceback__)
        )
        logger.error(f"[{self.instance_id}] ❌ {context}失败到 {target_type} {target_id_str}：{original_error}\n{original_trace}")
        try:
            await self._send_to_target(client, target_type, target_id_str, fallback_text)
            logger.warning(f"[{self.instance_id}] ⚠️ {context}失败，已向 {target_type} {target_id_str} 发送文本兜底通知。")
        except Exception as fallback_error:
            fallback_trace = "".join(
                traceback.format_exception(type(fallback_error), fallback_error, fallback_error.__traceback__)
            )
            logger.error(
                f"[{self.instance_id}] ❌ {context}的文本兜底通知也失败到 {target_type} {target_id_str}："
                f"{fallback_error}\n{fallback_trace}"
            )

    async def _send_with_text_fallback(self, client, target_type: str, target_id_str: str, message, context: str, fallback_text: str):
        """优先发送原始内容，失败时降级为纯文本提醒。"""
        try:
            await self._send_to_target(client, target_type, target_id_str, message)
            return True
        except Exception as error:
            await self._notify_send_failure(client, target_type, target_id_str, context, error, fallback_text)
            return False

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def handle_recall_event(self, event: AstrMessageEvent):
        """处理群聊撤回事件"""
        raw_message = event.message_obj.raw_message
        post_type = get_value(raw_message, "post_type")
        if post_type == "notice" and get_value(raw_message, "notice_type") == "group_recall":
            group_id = str(get_value(raw_message, "group_id"))
            message_id = str(get_value(raw_message, "message_id"))
            operator_id = str(get_value(raw_message, "operator_id"))
            if group_id not in self.monitor_groups or not message_id: return None
            
            file_path = next(self.temp_path.glob(f"*_{group_id}_{message_id}.json"), None)

            # 最大等待时间为缓存过期时间，优化大文件消息以及秒撤回的使用场景
            if not file_path or not file_path.exists():
                max_retries = self.cache_expiration_time  # 1s 一次
                for i in range(max_retries): 
                    await asyncio.sleep(1) 
                    file_path = next(self.temp_path.glob(f"*_{group_id}_{message_id}.json"), None)
                    if file_path and file_path.exists():
                        break
                else:
                    logger.warning(f"[{self.instance_id}] 等待 {self.cache_expiration_time} 秒后仍未找到消息记录 (ID: {message_id})，停止等待。")

            cached_data = None

            if file_path and file_path.exists():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        cached_data = json.load(f)
                except Exception as e:
                    logger.warning(f"[{self.instance_id}] 读取或解析本地缓存失败: {e}")
            
            # 如果没有找到缓存数据，则无法恢复
            if not cached_data:
                logger.warning(f"[{self.instance_id}] 找不到消息记录 (ID: {message_id})，可能已过期或未缓存。")
                return None
            
            # 从缓存数据中提取 relay_info，如果存在
            relay_info = cached_data.get("relay_info")

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
                    
                    # 获取群名和用户名和操作员
                    group_name, member_nickname, operator_nickname = str(group_id), str(sender_id), str(operator_id)
                    try:
                        group_info = await client.api.call_action('get_group_info', group_id=int(group_id))
                        group_name = group_info.get('group_name', group_name)
                    except: pass
                    try:
                        member_info = await client.api.call_action('get_group_member_info', group_id=int(group_id), user_id=int(sender_id))
                        card, nickname = member_info.get('card', ''), member_info.get('nickname', '')
                        member_nickname = card or nickname or member_nickname
                    except: pass
                    try:
                        operator_info = await client.api.call_action('get_group_member_info', group_id=int(group_id), user_id=int(operator_id))
                        card, nickname = operator_info.get('card', ''), operator_info.get('nickname', '')
                        operator_nickname = card or nickname or operator_nickname
                    except: pass
                    
                    logger.info(f"[{self.instance_id}] 合并转发撤回 - 群: {group_name}, 发送者: {member_nickname}, 操作者: {operator_nickname} ({operator_id})")
                    
                    # 准备所有通知目标
                    targets = await self._get_targets_for_group(group_id)
                    
                    # 向每个目标转发
                    for target_type, target_id in targets:
                    # 循环已在上方替换
                        target_id_str = str(target_id)
                        
                        header = self._create_recall_notification_header(group_name, group_id, member_nickname, sender_id, operator_nickname, operator_id, timestamp)
                        notification_text = f"{header}\n--------------------\n以下是撤回的聊天记录："
                        try:
                            await self._send_to_target(client, target_type, target_id_str, notification_text)
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            logger.error(f"[{self.instance_id}] 发送合并转发通知失败到 {target_type} {target_id_str}: {e}")
                            continue
                        
                        try:
                            action = "forward_friend_single_msg" if target_type == "private" else "forward_group_single_msg"
                            params = {"user_id": int(target_id_str)} if target_type == "private" else {"group_id": int(target_id_str)}
                            await client.api.call_action(action, message_id=relay_msg_id, **params)
                        except Exception as e:
                            fallback_text = (
                                f"{header}\n--------------------\n"
                                f"原消息为合并转发记录，但转发到当前目标时失败。\n"
                                f"可能原因：平台超时、风控或消息内容暂不可复现。\n"
                                f"错误：{e}"
                            )
                            await self._notify_send_failure(
                                client, target_type, target_id_str, "转发合并消息", e, fallback_text
                            )
                    
                    # 立即撤回中转群的消息 (如果是私聊中转则不撤回)
                    if self.auto_recall_relay and not relay_info.get("is_private_relay"):
                        try:
                            await client.api.call_action("delete_msg", message_id=relay_msg_id)
                        except Exception as e:
                            logger.error(f"[{self.instance_id}] 撤回中转群消息失败: {e}")
                    
                except Exception as e:
                    logger.error(f"[{self.instance_id}] 处理合并转发撤回失败: {e}")
                
                return None
            
            if cached_data:
                local_files_to_cleanup = [] 
                try:
                    sender_id = cached_data["sender_id"]
                    local_file_map = cached_data.get("local_file_map", {})
                    if str(sender_id) in self.ignore_senders: return None
                    
                    cached_components_data = cached_data.get("components", [])
                    
                    unsupported_types = set()
                    supported_types_set = {'Plain', 'Text', 'Image', 'Face', 'At', 'Video', 'Record', 'Json', 'File', 'Forward'}
                    for comp_dict in cached_components_data:
                        comp_type_name = comp_dict.get('type')
                        if comp_type_name not in supported_types_set:
                            unsupported_types.add(comp_type_name)
                    
                    components = _deserialize_components(cached_components_data)

                    timestamp = cached_data.get("timestamp")
                    client = event.bot
                    
                    group_name, member_nickname, operator_nickname = str(group_id), str(sender_id), str(operator_id)
                    try:
                        group_info = await client.api.call_action('get_group_info', group_id=int(group_id)); group_name = group_info.get('group_name', group_name)
                    except: pass
                    try:
                        member_info = await client.api.call_action('get_group_member_info', group_id=int(group_id), user_id=int(sender_id)); card, nickname = member_info.get('card', ''), member_info.get('nickname', ''); member_nickname = card or nickname or member_nickname
                    except: pass
                    try:
                        operator_info = await client.api.call_action('get_group_member_info', group_id=int(group_id), user_id=int(operator_id)); card, nickname = operator_info.get('card', ''), operator_info.get('nickname', ''); operator_nickname = card or nickname or operator_nickname
                    except: pass

                    logger.info(f"[{self.instance_id}] 发现撤回。群: {group_name} ({group_id}), 发送者: {member_nickname} ({sender_id})")
                    
                    special_components = [comp for comp in components if getattr(comp.type, 'name', 'unknown') in ['Video', 'Record', 'Json', 'File', 'Forward']]
                    other_components = [comp for comp in components if getattr(comp.type, 'name', 'unknown') not in ['Video', 'Record', 'Json', 'File', 'Forward']]
                    
                    async with aiohttp.ClientSession() as session:
                        targets = await self._get_targets_for_group(group_id)
                        for target_type, target_id in targets:
                            target_id_str = str(target_id)
                            
                            notification_prefix = self._create_recall_notification_header(group_name, group_id, member_nickname,sender_id, operator_nickname, operator_id, timestamp)
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
                                    fallback_text = self._message_to_plain_text(gocq_content_array)
                                    if not fallback_text:
                                        fallback_text = f"{notification_prefix}{warning_text}\n--------------------\n[原消息发送失败，且无法提取可展示内容]"
                                    await self._send_with_text_fallback(
                                        client, target_type, target_id_str, gocq_content_array, "合并消息转发", fallback_text
                                    )
                            else:
                                final_notification_text = f"{notification_prefix}{warning_text}\n--------------------\n内容将分条发送。"
                                header_ok = await self._send_with_text_fallback(
                                    client,
                                    target_type,
                                    target_id_str,
                                    final_notification_text,
                                    "发送通知头",
                                    f"{notification_prefix}\n[原始通知头发送失败，已切换为简化文本通知]",
                                )
                                if not header_ok:
                                    continue
                                
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
                                        fallback_text = self._message_to_plain_text(content_message)
                                        if not fallback_text:
                                            fallback_text = f"{member_nickname}：[非特殊内容发送失败，且无法提取文本]"
                                        await self._send_with_text_fallback(
                                            client, target_type, target_id_str, content_message, "发送非特殊内容", fallback_text
                                        )
                            
                            for comp in special_components:
                                await asyncio.sleep(0.5)
                                comp_type_name = getattr(comp.type, 'name', 'unknown')
                                content_parts = await _process_component_and_get_gocq_part(comp, session, self.temp_path, local_files_to_cleanup, local_file_map)
                                final_parts_to_send = content_parts
                                if not other_components:
                                    prefix_part = [{"type": "text", "data": {"text": f"{member_nickname}："}}]
                                    final_parts_to_send = prefix_part + content_parts
                                
                                type_map = {
                                    "Image": "图片", "Video": "视频", "Record": "语音",
                                    "File": "文件", "Forward": "合并转发", "Json": "小程序"
                                }
                                cn_type = type_map.get(comp_type_name, comp_type_name)
                                fallback_text = self._message_to_plain_text(final_parts_to_send)
                                if not fallback_text or fallback_text == f"{member_nickname}：":
                                    fallback_text = f"{member_nickname}：[发送失败: {cn_type} 消息可能包含无法上传的内容、过大或平台暂时超时]"
                                await self._send_with_text_fallback(
                                    client,
                                    target_type,
                                    target_id_str,
                                    final_parts_to_send,
                                    f"发送特殊内容 ({comp_type_name})",
                                    fallback_text,
                                )
                
                finally:
                    if local_files_to_cleanup: asyncio.create_task(_cleanup_local_files(local_files_to_cleanup))
                    if file_path:
                        asyncio.create_task(delayed_delete(0, file_path))
            else:
                logger.warning(f"[{self.instance_id}] 找不到消息记录 (ID: {message_id})，可能已过期或未缓存。")
        return None
