import asyncio
import time
import traceback
import json
import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any
import aiohttp
import os
import shutil

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.api import message_components as Comp
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.platform import MessageType
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType
from .rendering.chat_screenshot import generate_text_recall_screenshot, set_font_dir as set_screenshot_font_dir
from .rendering.font_manager import FontManager


def build_cache_task_key(group_id: str, message_id: str) -> str:
    return f"{group_id}:{message_id}"


@dataclass
class CacheTaskState:
    task: asyncio.Task | None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)


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


def _build_original_image_urls(raw_message: dict) -> list[str | None]:
    """从 raw_message 中提取 Image 段的原始 HTTP URL 列表，顺序与消息段一致。"""
    urls = []
    if isinstance(raw_message, dict):
        msg_list = raw_message.get('message', [])
        if isinstance(msg_list, list):
            for seg in msg_list:
                if isinstance(seg, dict) and seg.get('type') == 'image':
                    data = seg.get('data', {}) or {}
                    url = data.get('url', '') or ''
                    urls.append(url if url.startswith(('http://', 'https://')) else None)
    return urls


async def _download_and_cache_image(session: aiohttp.ClientSession, component: Comp.Image, temp_path: Path) -> str:
    image_url = getattr(component, 'url', None)
    if not image_url: return None

    # 本地路径直接复制，兼容 AstrBot 预处理阶段已将 url 改写为本地缓存路径的场景
    if not image_url.startswith(('http://', 'https://')):
        src = Path(image_url)
        if src.exists():
            file_name = f"forward_{int(time.time() * 1000)}{src.suffix or '.jpg'}"
            dest = temp_path / file_name
            try:
                shutil.copy2(str(src), str(dest))
                return str(dest.absolute())
            except Exception as e:
                logger.error(f"[AntiRevoke] ❌ 图片复制失败 ({image_url}): {e}")
                return None
        else:
            logger.warning(f"[AntiRevoke] 图片路径不存在: {image_url}")
            return None

    file_name = f"forward_{int(time.time() * 1000)}{Path(image_url).suffix or '.jpg'}"
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
    comp, session: aiohttp.ClientSession, temp_path: Path, local_files_to_cleanup: List[str], local_file_map: Dict = None, show_qq_in_at: bool = False
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
        qq = getattr(comp, 'qq', None)
        if qq:
            name = getattr(comp, 'name', f'@{qq}')
            if show_qq_in_at:
                gocq_parts.append({"type": "text", "data": {"text": f"@{name}({qq}) "}})
            else:
                gocq_parts.append({"type": "text", "data": {"text": f"@{name} "}})
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


class AntiRevoke(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        config = config or {}
        self.instance_id = "AntiRevoke"
        monitor = config.get("monitor", {}) or {}
        target = config.get("target", {}) or {}
        cache = config.get("cache", {}) or {}
        forward = config.get("forward", {}) or {}
        enhance = config.get("enhance", {}) or {}
        self.monitor_groups = [str(g) for g in monitor.get("monitor_groups", []) or []]
        self.target_receivers = [str(r) for r in target.get("target_receivers", []) or []]
        self.target_groups = [str(g) for g in target.get("target_groups", []) or []]
        self.ignore_senders = [str(s) for s in monitor.get("ignore_senders", []) or []]
        self.ignore_operators = [str(o) for o in monitor.get("ignore_operators", []) or []]
        self.cache_expiration_time = int(cache.get("cache_expiration_time", 300))
        self.cache_pending_timeout = min(self.cache_expiration_time, 2)
        self.file_download_retry_attempts = 3
        self.file_download_retry_delay = 0.2
        self.file_size_threshold_mb = int(cache.get("file_size_threshold_mb", 300))
        self.forward_relay_group = str(forward.get("forward_relay_group", "") or "")
        self.forward_to_self = forward.get("forward_to_self", False)
        self.auto_recall_relay = forward.get("auto_recall_relay", True)
        self.enable_text_screenshot = enhance.get("enable_text_screenshot", False)
        self.show_title = enhance.get("show_title", True)
        self.enable_fake_forward = enhance.get("enable_fake_forward", False)
        self.context = context
        self.temp_path = Path(StarTools.get_data_dir("astrbot_plugin_anti_revoke"))
        self.temp_path.mkdir(exist_ok=True)
        self.video_cache_path = self.temp_path / "videos"
        self.video_cache_path.mkdir(exist_ok=True)
        self.voice_cache_path = self.temp_path / "voices"
        self.voice_cache_path.mkdir(exist_ok=True)
        self.file_cache_path = self.temp_path / "files"
        self.file_cache_path.mkdir(exist_ok=True)

        # 字体 CDN 下载初始化
        self.font_cache_path = self.temp_path / "fonts"
        if self.enable_text_screenshot:
            self.font_cache_path.mkdir(parents=True, exist_ok=True)
            set_screenshot_font_dir(self.font_cache_path)
        self._font_manager = FontManager(self.temp_path) if self.enable_text_screenshot else None
        self._font_task: asyncio.Task | None = None

        self._cache_tasks: dict[str, CacheTaskState] = {}
        self._cleanup_cache_on_startup()
        asyncio.create_task(self._cleanup_kv_data())

    async def initialize(self) -> None:
        if self._font_manager is not None:
            self._font_task = asyncio.create_task(
                self._ensure_fonts(),
                name="anti-revoke-字体下载",
            )

    async def terminate(self) -> None:
        if self._font_task is not None and not self._font_task.done():
            self._font_task.cancel()
            try:
                await self._font_task
            except asyncio.CancelledError:
                pass
        self._font_task = None

    async def _ensure_fonts(self) -> None:
        try:
            ok = await self._font_manager.ensure_fonts()
            if ok:
                set_screenshot_font_dir(self.font_cache_path)
            else:
                logger.warning(f"[{self.instance_id}] 字体下载未完成，将使用内置字体回退")
        except Exception as e:
            logger.error(f"[{self.instance_id}] 字体下载过程异常: {e}")
        finally:
            self._font_task = None

    async def _cleanup_kv_data(self):
        """清理 KV 存储中不再监控的群组配置"""
        kv_targets = await self.get_kv_data("forward_targets", {})
        if not kv_targets:
            return
        
        cleaned_targets = {}
        changed = False
        for group_id, targets in kv_targets.items():
            if self._is_monitored(group_id):
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

    def _is_monitored(self, group_id: str) -> bool:
        return not self.monitor_groups or group_id in self.monitor_groups

    def _should_skip_cache(self, group_id: str, sender_id: str, message_type) -> bool:
        return (
            message_type != MessageType.GROUP_MESSAGE
            or not self._is_monitored(group_id)
            or str(sender_id) in self.ignore_senders
        )

    def _get_cache_file_path(self, group_id: str, message_id: str) -> Path:
        return self.temp_path / f"cache_{group_id}_{message_id}.json"

    def _find_cache_file(self, group_id: str, message_id: str) -> Path | None:
        file_path = self._get_cache_file_path(group_id, message_id)
        if file_path.exists():
            return file_path
        return next(self.temp_path.glob(f"*_{group_id}_{message_id}.json"), None)

    def _write_cache_record(self, file_path: Path, data: dict[str, Any]) -> None:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def _get_file_with_retry(self, comp) -> str:
        last_error = None
        for attempt in range(1, self.file_download_retry_attempts + 1):
            try:
                return await comp.get_file()
            except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError) as e:
                last_error = e
                logger.warning(
                    f"[{self.instance_id}] [File处理] 下载文件失败，第 {attempt}/{self.file_download_retry_attempts} 次重试: {e}"
                )
                if attempt >= self.file_download_retry_attempts:
                    break
                await asyncio.sleep(self.file_download_retry_delay)
        if last_error:
            raise last_error
        return ""

    async def _wait_for_cache_task_result(self, cache_key: str, timeout: float | None = None) -> dict[str, Any] | None:
        state = self._cache_tasks.get(cache_key)
        if not state:
            return None
        if not state.done.is_set():
            try:
                await asyncio.wait_for(state.done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug(f"[{self.instance_id}] 缓存任务仍在进行中: {cache_key}")
                return None
        return state.result

    async def _expire_cache_task_state(self, cache_key: str, state: CacheTaskState):
        await asyncio.sleep(self.cache_expiration_time)
        current_state = self._cache_tasks.get(cache_key)
        if current_state is state:
            self._cache_tasks.pop(cache_key, None)

    async def _run_cache_task(self, cache_key: str, state: CacheTaskState, payload: dict[str, Any]):
        file_path = payload.get("file_path")
        try:
            state.result = await self._cache_message_worker(payload)
        except Exception as e:
            state.error = str(e)
            if file_path:
                self._write_cache_record(
                    file_path,
                    {
                        "status": "failed",
                        "group_id": payload.get("group_id"),
                        "message_id": payload.get("message_id"),
                        "sender_id": payload.get("sender_id"),
                        "timestamp": payload.get("timestamp"),
                        "error": str(e),
                    },
                )
                asyncio.create_task(delayed_delete(self.cache_expiration_time, file_path))
            logger.error(f"[{self.instance_id}] 缓存任务失败 ({cache_key}): {e}\n{traceback.format_exc()}")
        finally:
            state.task = None
            state.done.set()
            asyncio.create_task(self._expire_cache_task_state(cache_key, state))

    def _schedule_cache_task(self, cache_key: str, payload: dict[str, Any]) -> CacheTaskState:
        existing_state = self._cache_tasks.get(cache_key)
        if existing_state and not existing_state.done.is_set():
            return existing_state

        state = CacheTaskState(task=None)
        self._cache_tasks[cache_key] = state
        state.task = asyncio.create_task(self._run_cache_task(cache_key, state, payload))
        return state

    def _load_cached_data(self, file_path: Path | None) -> dict[str, Any] | None:
        if not file_path or not file_path.exists():
            return None
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[{self.instance_id}] 读取或解析本地缓存失败: {e}")
            return None

    async def _get_group_context_names(
        self,
        client,
        group_id: str,
        sender_id: str | None = None,
        operator_id: str | None = None,
    ) -> tuple[str, str, str]:
        """统一获取群名、发送者昵称、操作者昵称，失败时自动降级到原始ID。"""
        group_name = str(group_id)
        sender_name = str(sender_id) if sender_id else "未知发送者"
        operator_name = str(operator_id) if operator_id else ""

        group_id_int: int | None
        try:
            group_id_int = int(group_id)
        except Exception:
            group_id_int = None

        if group_id_int is not None:
            try:
                group_info = await client.api.call_action('get_group_info', group_id=group_id_int)
                if isinstance(group_info, dict):
                    group_name = group_info.get('group_name', group_name)
            except Exception:
                pass

        async def _get_member_name(user_id: str | None, default_name: str) -> str:
            if not user_id or group_id_int is None:
                return default_name
            try:
                user_id_int = int(user_id)
            except Exception:
                return default_name
            try:
                member_info = await client.api.call_action(
                    'get_group_member_info',
                    group_id=group_id_int,
                    user_id=user_id_int,
                )
                if isinstance(member_info, dict):
                    card, nickname = member_info.get('card', ''), member_info.get('nickname', '')
                    return card or nickname or default_name
            except Exception:
                pass
            return default_name

        sender_name = await _get_member_name(sender_id, sender_name)
        operator_name = await _get_member_name(operator_id, operator_name)
        return group_name, sender_name, operator_name

    async def _poll_cache_record(
        self,
        group_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        short_poll_interval = 0.1
        long_poll_interval = 1.0
        file_path = self._get_cache_file_path(group_id, message_id)
        cached_data = None
        started_at = time.monotonic()
        pending_deadline = time.monotonic() + self.cache_pending_timeout
        logger.debug(f"[{self.instance_id}] 开始轮询缓存（群: {group_id}, 消息ID: {message_id}）")

        while time.monotonic() < pending_deadline:
            cached_data = self._load_cached_data(file_path)
            if cached_data and str(cached_data.get("message_id", "")) == message_id:
                break
            await asyncio.sleep(short_poll_interval)
        else:
            return None

        if not cached_data:
            elapsed = time.monotonic() - started_at
            logger.warning(f"[{self.instance_id}] 缓存文件读取为空（ID: {message_id}, 耗时: {elapsed:.2f}s）")
            return None

        wait_deadline = started_at + self.cache_expiration_time
        while time.monotonic() < wait_deadline:
            cached_data = self._load_cached_data(file_path)
            if not cached_data:
                await asyncio.sleep(long_poll_interval)
                continue
            status = cached_data.get("status")
            if status in ("done", "failed"):
                elapsed = time.monotonic() - started_at
                logger.debug(f"[{self.instance_id}] 轮询结束（ID: {message_id}, 状态: {status}, 耗时: {elapsed:.2f}s）")
                return cached_data
            await asyncio.sleep(long_poll_interval)

        elapsed = time.monotonic() - started_at
        logger.warning(f"[{self.instance_id}] 长轮询超时（ID: {message_id}, 上限: {self.cache_expiration_time}s, 耗时: {elapsed:.2f}s）")
        return None

    async def terminate(self):
        logger.info(f"[{self.instance_id}] 插件已卸载/重载。")
        
    @filter.command("撤回转发")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def add_forward(self, event: AstrMessageEvent, group_id: str, target: str):
        """设置撤回消息的转发目标。格式：撤回转发 群号 @私聊或#群聊"""
        if not self._is_monitored(group_id):
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

    def _extract_text_for_screenshot(self, components: list) -> str:
        if not components:
            return ""

        allowed_types = {"Plain", "Text", "At", "Face"}
        parts: list[str] = []
        has_text_content = False

        for comp in components:
            comp_type_name = getattr(comp.type, "name", "unknown")
            if comp_type_name not in allowed_types:
                return ""

            if comp_type_name in {"Plain", "Text"}:
                text = str(getattr(comp, "text", "") or "")
                if text:
                    has_text_content = True
                    parts.append(text)
            elif comp_type_name == "At":
                qq = getattr(comp, "qq", "")
                name = getattr(comp, "name", "") or qq
                if name:
                    has_text_content = True
                    parts.append(f"@{name} ")
            elif comp_type_name == "Face":
                continue

        if not has_text_content:
            return ""
        return "".join(parts).strip()

    async def _send_text_recall_screenshot(
        self,
        client,
        target_type: str,
        target_id_str: str,
        group_id: str,
        sender_id: str,
        member_nickname: str,
        text: str,
    ) -> None:
        if not self.enable_text_screenshot or not text:
            return

        try:
            image_bytes = await generate_text_recall_screenshot(
                client=client,
                group_id=int(group_id),
                user_id=int(sender_id),
                text=text,
                fallback_name=member_nickname,
                show_title=self.show_title,
            )
            if not image_bytes:
                logger.warning(f"[{self.instance_id}] 截图生成失败（返回空）")
                return

            image_message = [
                {
                    "type": "image",
                    "data": {
                        "file": f"base64://{base64.b64encode(image_bytes).decode('utf-8')}",
                    },
                }
            ]
            await self._send_with_text_fallback(
                client,
                target_type,
                target_id_str,
                image_message,
                "发送文本撤回聊天截图",
                "[聊天截图发送失败]",
            )
        except Exception as exc:
            logger.error(f"[{self.instance_id}] 发送截图异常: {exc}")

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def handle_message_cache(self, event: AstrMessageEvent):
        """处理消息缓存，后台执行缓存任务"""
        group_id = str(event.get_group_id())
        message_id = str(event.message_obj.message_id)
        sender_id = str(event.get_sender_id())
        if self._should_skip_cache(group_id, sender_id, event.get_message_type()):
            return None

        message_obj = event.get_messages()
        components = message_obj.components if isinstance(message_obj, MessageChain) else message_obj if isinstance(message_obj, list) else []
        components = [comp for comp in components if getattr(comp.type, 'name', 'unknown') != 'Reply']
        if not components:
            return None
        component_types = [getattr(comp.type, "name", "unknown") for comp in components]

        cache_key = build_cache_task_key(group_id, message_id)
        cache_file = self._get_cache_file_path(group_id, message_id)
        if cache_file and cache_file.exists():
            return None

        existing_state = self._cache_tasks.get(cache_key)
        if existing_state and not existing_state.done.is_set():
            return None

        self._write_cache_record(
            cache_file,
            {
                "status": "pending",
                "group_id": group_id,
                "message_id": message_id,
                "sender_id": sender_id,
                "timestamp": event.message_obj.timestamp,
                "component_types": component_types,
            },
        )

        payload = {
            'group_id': group_id,
            'message_id': message_id,
            'sender_id': sender_id,
            'timestamp': event.message_obj.timestamp,
            'raw_message': event.message_obj.raw_message,
            'components': components,
            'client': event.bot,
            'file_path': cache_file,
        }
        self._schedule_cache_task(cache_key, payload)
        return None

    async def _cache_message_worker(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        group_id = payload['group_id']
        message_id = payload['message_id']
        sender_id = payload['sender_id']
        timestamp = payload['timestamp']
        cache_file = payload.get('file_path')
        raw_message = payload.get('raw_message')
        if not isinstance(raw_message, dict):
            raw_message = {}
        components = payload.get('components', [])
        client = payload['client']

        relay_info = None
        message_list = raw_message.get('message', [])
        is_forward = False
        if isinstance(message_list, list) and message_list:
            first_segment = message_list[0]
            if isinstance(first_segment, dict) and first_segment.get('type') == 'forward':
                is_forward = True

        if is_forward and self.forward_to_self:
            try:
                login_info = await client.api.call_action('get_login_info')
                self_id = login_info.get('user_id')
                if self_id:
                    await client.api.call_action(
                        'forward_friend_single_msg',
                        user_id=int(self_id),
                        message_id=message_id,
                    )

                    relay_msg_id = None
                    await asyncio.sleep(1)
                    try:
                        history_result = await client.api.call_action(
                            'get_friend_msg_history',
                            user_id=int(self_id),
                            count=10,
                        )
                        messages = []
                        if isinstance(history_result, dict):
                            messages = history_result.get('messages', []) or history_result.get('data', {}).get('messages', [])
                        for msg in reversed(messages):
                            msg_time = int(msg.get('time', 0))
                            if abs(msg_time - timestamp) <= 2:
                                relay_msg_id = msg.get('message_id')
                                break
                    except Exception as exc:
                        logger.warning(f'[{self.instance_id}] 查询自身私聊历史消息失败: {exc}')

                    if relay_msg_id:
                        relay_info = {
                            'relay_msg_id': relay_msg_id,
                            'sender_id': sender_id,
                            'timestamp': timestamp,
                            'group_id': group_id,
                            'is_private_relay': True,
                        }
            except Exception as exc:
                logger.error(f'[{self.instance_id}] ❌ 转发合并消息给机器人自身失败: {exc}')

        if not relay_info and is_forward and self.forward_relay_group:
            try:
                await client.api.call_action(
                    'forward_group_single_msg',
                    group_id=int(self.forward_relay_group),
                    message_id=message_id,
                )

                relay_msg_id = None
                relay_msg_time = None
                await asyncio.sleep(1)
                try:
                    self_id = None
                    try:
                        login_info = await client.api.call_action('get_login_info')
                        self_id = str(login_info.get('user_id'))
                    except Exception:
                        pass

                    found_msg = None
                    next_seq = 0
                    for _ in range(5):
                        history_result = await client.api.call_action(
                            'get_group_msg_history',
                            group_id=int(self.forward_relay_group),
                            message_seq=next_seq,
                            count=20,
                        )
                        messages = []
                        if isinstance(history_result, dict):
                            messages = history_result.get('data', {}).get('messages', [])
                            if not messages:
                                messages = history_result.get('messages', [])
                        if not messages:
                            await asyncio.sleep(1)
                            continue

                        for msg in reversed(messages):
                            msg_sender_id = str(msg.get('sender', {}).get('user_id', '')) or str(msg.get('user_id', ''))
                            if self_id and msg_sender_id != self_id:
                                continue
                            msg_time = int(msg.get('time', 0))
                            if abs(msg_time - timestamp) <= 1:
                                found_msg = msg
                                break

                        if found_msg:
                            break

                        oldest_msg = messages[0]
                        next_seq = oldest_msg.get('message_seq')
                        oldest_time = int(oldest_msg.get('time', 0))
                        if timestamp - oldest_time > self.cache_expiration_time:
                            break
                        if next_seq == 0:
                            break

                    if found_msg:
                        relay_msg_id = found_msg.get('message_id')
                        relay_msg_time = found_msg.get('time')
                except Exception as exc:
                    logger.error(f'[{self.instance_id}] 查询历史消息失败: {exc}')

                if relay_msg_id:
                    relay_info = {
                        'relay_msg_id': relay_msg_id,
                        'sender_id': sender_id,
                        'timestamp': timestamp,
                        'relay_timestamp': relay_msg_time,
                        'group_id': group_id,
                    }
                    if self.auto_recall_relay:
                        asyncio.create_task(self._auto_recall_relay_msg(client, relay_msg_id))
            except Exception as exc:
                logger.error(f'[{self.instance_id}] ❌ 转发合并消息到中转群失败: {exc}\n{traceback.format_exc()}')

        timestamp_ms = int(time.time() * 1000)
        raw_file_names = []
        raw_file_sizes = {}
        raw_video_sizes = {}
        raw_record_urls = {}
        try:
            if isinstance(message_list, list):
                for segment in message_list:
                    if not isinstance(segment, dict):
                        continue
                    if segment.get('type') == 'file':
                        file_name = segment.get('data', {}).get('file')
                        file_size = segment.get('data', {}).get('file_size')
                        if file_name:
                            raw_file_names.append(file_name)
                        if file_size:
                            try:
                                raw_file_sizes[file_name] = int(file_size) if isinstance(file_size, str) else file_size
                            except ValueError:
                                logger.warning(f'[AntiRevoke] 无法解析文件大小: {file_size}')
                    elif segment.get('type') == 'video':
                        file_id = segment.get('data', {}).get('file')
                        file_size = segment.get('data', {}).get('file_size')
                        if file_id and file_size:
                            try:
                                raw_video_sizes[file_id] = int(file_size) if isinstance(file_size, str) else file_size
                            except ValueError:
                                logger.warning(f'[AntiRevoke] 无法解析视频大小: {file_size}')
                    elif segment.get('type') == 'record':
                        file_id = segment.get('data', {}).get('file')
                        url = segment.get('data', {}).get('url')
                        if file_id and url:
                            raw_record_urls[file_id] = url
        except Exception as exc:
            logger.warning(f'[AntiRevoke] 解析 raw_message 失败: {exc}')

        local_file_map = {}
        original_image_urls = _build_original_image_urls(raw_message)
        original_image_idx = -1
        has_downloadable_content = any(getattr(comp.type, 'name', '') in ['Image', 'Video', 'Record', 'File'] for comp in components)
        if has_downloadable_content:
            for comp in components:
                comp_type_name = getattr(comp.type, 'name', 'unknown')

                if comp_type_name == 'Video':
                    file_id = getattr(comp, 'file', None)
                    if not file_id:
                        continue

                    video_size = raw_video_sizes.get(file_id)
                    if video_size and self.file_size_threshold_mb > 0:
                        video_size_mb = video_size / (1024 * 1024)
                        if video_size_mb > self.file_size_threshold_mb:
                            setattr(comp, 'file', f'[视频过大未缓存: {video_size_mb:.2f} MB]')
                            continue

                    try:
                        ret = await client.api.call_action('get_file', **{'file_id': file_id})
                        download_url = ret.get('url')
                        if not download_url:
                            setattr(comp, 'file', 'Error: API did not return a URL.')
                            continue

                        file_size_from_api = ret.get('file_size')
                        if file_size_from_api and self.file_size_threshold_mb > 0:
                            try:
                                file_size_int = int(file_size_from_api) if isinstance(file_size_from_api, str) else file_size_from_api
                                api_size_mb = file_size_int / (1024 * 1024)
                                if api_size_mb > self.file_size_threshold_mb:
                                    setattr(comp, 'file', f'[视频过大未缓存: {api_size_mb:.2f} MB]')
                                    continue
                            except (ValueError, TypeError):
                                logger.warning(f'[{self.instance_id}] 无法解析API返回的文件大小: {file_size_from_api}')

                        original_filename = getattr(comp, 'name', file_id.split('/')[-1])
                        if not original_filename or len(original_filename) < 5:
                            original_filename = f'{timestamp_ms}.mp4'

                        dest_path = self.video_cache_path / f'{timestamp_ms}_{original_filename}'
                        if await self._download_video_from_url(download_url, dest_path):
                            setattr(comp, 'file', str(dest_path.absolute()))
                            asyncio.create_task(delayed_delete(self.cache_expiration_time, dest_path))
                        else:
                            setattr(comp, 'file', f'Error: Download failed from {download_url}')
                    except Exception as exc:
                        logger.error(f'[{self.instance_id}] ❌ 处理视频缓存时发生错误: {exc}\n{traceback.format_exc()}')
                        setattr(comp, 'file', 'Error: Exception during cache process.')

                elif comp_type_name == 'Record':
                    file_id = getattr(comp, 'file', None)
                    if not file_id:
                        continue

                    try:
                        record_url = getattr(comp, 'url', None) or raw_record_urls.get(file_id)
                        if record_url:
                            try:
                                original_suffix = '.amr'
                                if getattr(comp, 'file', '').endswith('.slk'):
                                    original_suffix = '.slk'
                                permanent_path = self.voice_cache_path / f'{timestamp_ms}{original_suffix}'
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
                            except Exception as exc:
                                logger.warning(f'[{self.instance_id}] [Record处理] 尝试通过 URL 下载失败: {exc}，将尝试使用本地路径兜底。')

                        ret = await client.api.call_action('get_file', **{'file_id': file_id})
                        local_path = ret.get('file')
                        if not local_path or not os.path.exists(local_path):
                            setattr(comp, 'file', 'Error: API did not return a valid file path.')
                            continue

                        original_suffix = Path(local_path).suffix or '.amr'
                        permanent_path = self.voice_cache_path / f'{timestamp_ms}{original_suffix}'
                        shutil.copy(local_path, permanent_path)
                        os.chmod(permanent_path, 0o644)
                        setattr(comp, 'file', str(permanent_path.absolute()))
                        asyncio.create_task(delayed_delete(self.cache_expiration_time, permanent_path))
                    except Exception as exc:
                        logger.error(f'[{self.instance_id}] ❌ 处理 Record 缓存时发生错误: {exc}\n{traceback.format_exc()}')
                        setattr(comp, 'file', 'Error: Exception during cache process.')

                elif comp_type_name == 'File':
                    try:
                        original_filename = raw_file_names[0] if raw_file_names else None
                        file_size = raw_file_sizes.get(original_filename) if original_filename else None
                        if file_size and self.file_size_threshold_mb > 0:
                            file_size_mb = file_size / (1024 * 1024)
                            if file_size_mb > self.file_size_threshold_mb:
                                unique_key = getattr(comp, 'url', None)
                                if unique_key:
                                    local_file_map[unique_key] = f'[文件过大未缓存: {file_size_mb:.2f} MB, 文件名: {original_filename}]'
                                if raw_file_names:
                                    raw_file_names.pop(0)
                                continue

                        temp_file_path = await self._get_file_with_retry(comp)
                        if not temp_file_path or not os.path.exists(temp_file_path):
                            continue

                        if not original_filename and raw_file_names:
                            original_filename = raw_file_names.pop(0)
                        if not original_filename:
                            original_filename = getattr(comp, 'name', Path(temp_file_path).name)
                        if not original_filename or original_filename == Path(temp_file_path).name:
                            original_filename = f'未知文件_{timestamp_ms}.dat'

                        permanent_path = self.file_cache_path / f'{timestamp_ms}_{original_filename}'
                        shutil.copy(temp_file_path, permanent_path)

                        unique_key = getattr(comp, 'url', None)
                        if unique_key:
                            local_file_map[unique_key] = str(permanent_path)
                            asyncio.create_task(delayed_delete(self.cache_expiration_time, permanent_path))
                    except Exception as exc:
                        logger.error(f'[{self.instance_id}] ❌ 处理 File 缓存时发生错误: {exc}\n{traceback.format_exc()}')

                elif comp_type_name == 'Image':
                    image_url = getattr(comp, 'url', None)
                    if not image_url:
                        continue

                    original_image_idx += 1
                    orig_url = original_image_urls[original_image_idx] if original_image_idx < len(original_image_urls) else None

                    if orig_url:
                        try:
                            dest_name = f"image_{int(time.time() * 1000)}{Path(orig_url).suffix or '.jpg'}"
                            dest = self.temp_path / dest_name
                            headers = {'User-Agent': 'Mozilla/5.0 ...', 'Referer': 'https://qzone.qq.com/'}
                            async with aiohttp.ClientSession() as session:
                                async with session.get(orig_url, headers=headers, timeout=15) as response:
                                    response.raise_for_status()
                                    image_bytes = await response.read()
                                    with open(dest, 'wb') as f:
                                        f.write(image_bytes)
                            setattr(comp, 'url', str(dest.absolute()))
                            asyncio.create_task(delayed_delete(self.cache_expiration_time, dest))
                            continue
                        except Exception as exc:
                            logger.warning(f'[{self.instance_id}] 原始URL下载失败，使用当前URL: {exc}')

                    if not image_url.startswith(('http://', 'https://')):
                        src = Path(image_url)
                        if src.exists():
                            suffix = src.suffix or '.jpg'
                            dest_name = f"image_{int(time.time() * 1000)}{suffix}"
                            dest = self.temp_path / dest_name
                            try:
                                shutil.copy2(str(src), str(dest))
                                setattr(comp, 'url', str(dest.absolute()))
                                asyncio.create_task(delayed_delete(self.cache_expiration_time, dest))
                            except Exception as exc:
                                logger.error(f'[{self.instance_id}] ❌ 图片缓存复制失败 ({image_url}): {exc}')
                        else:
                            logger.warning(f'[{self.instance_id}] 图片路径不存在，跳过缓存: {image_url}')
                    else:
                        try:
                            dest_name = f"image_{int(time.time() * 1000)}{Path(image_url).suffix or '.jpg'}"
                            dest = self.temp_path / dest_name
                            headers = {'User-Agent': 'Mozilla/5.0 ...', 'Referer': 'https://qzone.qq.com/'}
                            async with aiohttp.ClientSession() as session:
                                async with session.get(image_url, headers=headers, timeout=15) as response:
                                    response.raise_for_status()
                                    image_bytes = await response.read()
                                    with open(dest, 'wb') as f:
                                        f.write(image_bytes)
                            setattr(comp, 'url', str(dest.absolute()))
                            asyncio.create_task(delayed_delete(self.cache_expiration_time, dest))
                        except Exception as exc:
                            logger.error(f'[{self.instance_id}] ❌ 图片缓存下载失败 ({image_url}): {exc}')

        file_path = cache_file if isinstance(cache_file, Path) else self._get_cache_file_path(group_id, message_id)
        self._write_cache_record(
            file_path,
            {
                "status": "done",
                "group_id": group_id,
                "message_id": message_id,
                "sender_id": sender_id,
                "timestamp": timestamp,
                "components": _serialize_components(components),
                "local_file_map": local_file_map,
                "relay_info": relay_info,
            },
        )

        asyncio.create_task(delayed_delete(self.cache_expiration_time, file_path))
        return {"file_path": str(file_path), "relay_info": relay_info}

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

    async def _send_fake_forward(
        self,
        client,
        target_type: str,
        target_id_str: str,
        sender_id: str,
        member_nickname: str,
        timestamp: int,
        components: list,
        local_file_map: dict,
        local_files_to_cleanup: list,
    ):
        """基于缓存的组件列表构造伪造的合并转发消息（聊天记录样式）。"""
        try:
            async with aiohttp.ClientSession() as session:
                segments = []
                for comp in components:
                    parts = await _process_component_and_get_gocq_part(
                        comp, session, self.temp_path, local_files_to_cleanup, local_file_map
                    )
                    segments.extend(parts)
            if not segments:
                return
            node = {
                "type": "node",
                "data": {
                    "name": member_nickname,
                    "uin": int(sender_id),
                    "time": int(timestamp or 0),
                    "content": segments,
                },
            }
            action = "send_private_forward_msg" if target_type == "private" else "send_group_forward_msg"
            params = {"user_id": int(target_id_str)} if target_type == "private" else {"group_id": int(target_id_str)}
            await client.api.call_action(action, messages=[node], **params)
        except Exception as e:
            logger.warning(f"[{self.instance_id}] 伪造合并转发发送失败 ({target_type}/{target_id_str}): {e}")

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
            recall_sender_id = str(get_value(raw_message, "user_id"))
            if not self._is_monitored(group_id) or not message_id: return None
            if operator_id in self.ignore_operators:
                logger.debug(f"[{self.instance_id}] 操作者 {operator_id} 在忽略列表中，跳过处理")
                return None

            client = event.bot
            early_group_name, early_member_nickname, _ = await self._get_group_context_names(
                client,
                group_id,
                sender_id=recall_sender_id,
                operator_id=None,
            )
            logger.info(
                f"[{self.instance_id}] 发现撤回。群: {early_group_name} ({group_id}), "
                f"发送者: {early_member_nickname} ({recall_sender_id or '未知'})"
            )
            
            cache_key = build_cache_task_key(group_id, message_id)
            file_path = self._get_cache_file_path(group_id, message_id)
            cached_data = await self._poll_cache_record(group_id, message_id)

            if not cached_data and cache_key in self._cache_tasks:
                task_result = await self._wait_for_cache_task_result(cache_key, timeout=self.cache_expiration_time)
                if task_result and task_result.get("file_path"):
                    file_path = Path(task_result["file_path"])
                    cached_data = self._load_cached_data(file_path)

            if not cached_data:
                file_path = self._find_cache_file(group_id, message_id)
                cached_data = self._load_cached_data(file_path)

            if cached_data and cached_data.get("status") == "failed":
                logger.warning(f"[{self.instance_id}] 消息缓存失败，跳过撤回恢复（ID: {message_id}）。")
                return None
            if cached_data and cached_data.get("status") == "pending":
                logger.warning(f"[{self.instance_id}] 消息缓存仍在进行中，跳过撤回恢复（ID: {message_id}）。")
                return None

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
                    group_name, member_nickname, operator_nickname = await self._get_group_context_names(
                        client,
                        group_id,
                        sender_id=sender_id,
                        operator_id=operator_id,
                    )
                    
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
                    if str(sender_id) in self.ignore_senders:
                        logger.debug(f"[{self.instance_id}] 发送者 {sender_id} 在忽略列表中，跳过处理")
                        return None
                    if operator_id in self.ignore_operators:
                        logger.debug(f"[{self.instance_id}] 操作人 {operator_id} 在忽略列表中，跳过处理")
                        return None
                    
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
                    group_name, member_nickname, operator_nickname = await self._get_group_context_names(
                        client,
                        group_id,
                        sender_id=sender_id,
                        operator_id=operator_id,
                    )

                    special_components = [comp for comp in components if getattr(comp.type, 'name', 'unknown') in ['Video', 'Record', 'Json', 'File', 'Forward']]
                    other_components = [comp for comp in components if getattr(comp.type, 'name', 'unknown') not in ['Video', 'Record', 'Json', 'File', 'Forward']]
                    
                    async with aiohttp.ClientSession() as session:
                        targets = await self._get_targets_for_group(group_id)
                        for target_type, target_id in targets:
                            target_id_str = str(target_id)
                            text_screenshot_content = self._extract_text_for_screenshot(other_components)
                            
                            notification_prefix = self._create_recall_notification_header(group_name, group_id, member_nickname,sender_id, operator_nickname, operator_id, timestamp)
                            warning_text = f"\n⚠️ 注意：包含不支持的组件：{', '.join(unsupported_types)}" if unsupported_types else ""
                            
                            if not special_components:
                                message_parts = []
                                for comp in other_components:
                                    converted_parts = await _process_component_and_get_gocq_part(comp, session, self.temp_path, local_files_to_cleanup, local_file_map, show_qq_in_at=True)
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
                                    await self._send_text_recall_screenshot(
                                        client,
                                        target_type,
                                        target_id_str,
                                        group_id,
                                        sender_id,
                                        member_nickname,
                                        text_screenshot_content,
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
                                        converted_parts = await _process_component_and_get_gocq_part(comp, session, self.temp_path, local_files_to_cleanup, local_file_map, show_qq_in_at=True)
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
                                content_parts = await _process_component_and_get_gocq_part(comp, session, self.temp_path, local_files_to_cleanup, local_file_map, show_qq_in_at=True)
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
                            
                            if self.enable_fake_forward:
                                await self._send_fake_forward(
                                    client,
                                    target_type,
                                    target_id_str,
                                    sender_id,
                                    member_nickname,
                                    timestamp,
                                    components,
                                    local_file_map,
                                    local_files_to_cleanup,
                                )
                
                finally:
                    if local_files_to_cleanup: asyncio.create_task(_cleanup_local_files(local_files_to_cleanup))
                    if file_path:
                        asyncio.create_task(delayed_delete(0, file_path))
            else:
                logger.warning(f"[{self.instance_id}] 找不到消息记录 (ID: {message_id})，可能已过期或未缓存。")
        return None
