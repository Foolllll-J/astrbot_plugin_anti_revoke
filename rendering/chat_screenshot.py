import importlib
import io
import random
import re
from pathlib import Path

import aiohttp
from astrbot import logger
from PIL import Image, ImageDraw, ImageFont


RESOURCES_DIR = Path(__file__).parent.parent / "resources"
FONT_DIR: Path | None = None
FONT_PATH = RESOURCES_DIR / "fonts" / "NotoSansSC-Regular.ttf"
FONT_BOLD_PATH = RESOURCES_DIR / "fonts" / "NotoSansSC-Bold.ttf"


def set_font_dir(path: Path) -> None:
    global FONT_DIR
    FONT_DIR = path

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U000024FF"
    "\U00002600-\U000026FF"
    "]+",
    flags=re.UNICODE,
)
EMOJI_EXTRA_PATTERN = re.compile(r"[\u200d\uFE0E\uFE0F]")
FACE_PLACEHOLDER_PATTERN = re.compile(r"\[\s*表情\s*\]")

_PILMOJI_CLASS = None
_PILMOJI_STATUS = "unknown"


def _patch_emoji_unicode_codes() -> None:
    emoji_module = importlib.import_module("emoji")
    unicode_codes = importlib.import_module("emoji.unicode_codes")

    if not hasattr(unicode_codes, "get_emoji_unicode_dict"):
        def get_emoji_unicode_dict(lang: str):
            emoji_data = getattr(emoji_module, "EMOJI_DATA", {}) or {}
            return {
                data[lang]: char
                for char, data in emoji_data.items()
                if isinstance(data, dict) and lang in data
            }

        unicode_codes.get_emoji_unicode_dict = get_emoji_unicode_dict

    if not hasattr(unicode_codes, "EMOJI_UNICODE"):
        unicode_codes.EMOJI_UNICODE = {"en": unicode_codes.get_emoji_unicode_dict("en")}


def _get_pilmoji_class():
    global _PILMOJI_CLASS, _PILMOJI_STATUS

    if _PILMOJI_STATUS == "available":
        return _PILMOJI_CLASS
    if _PILMOJI_STATUS == "unavailable":
        return None

    try:
        _patch_emoji_unicode_codes()
        pilmoji_module = importlib.import_module("pilmoji")
        _PILMOJI_CLASS = getattr(pilmoji_module, "Pilmoji", None)
        if _PILMOJI_CLASS is None:
            raise AttributeError("pilmoji.Pilmoji not found")
        _PILMOJI_STATUS = "available"
        return _PILMOJI_CLASS
    except Exception as exc:
        _PILMOJI_STATUS = "unavailable"
        _PILMOJI_CLASS = None
        logger.warning(
            "[AntiRevoke] emoji 渲染不可用，回退至 emoji 过滤: %s",
            exc,
        )
        return None


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    # 优先级 1: CDN 下载的字体
    if FONT_DIR is not None:
        cdn_path = FONT_DIR / ("NotoSansSC-Bold.ttf" if bold else "NotoSansSC-Regular.ttf")
        if cdn_path.exists():
            try:
                return ImageFont.truetype(str(cdn_path), size)
            except Exception:
                pass

    # 优先级 2: 插件内置字体（旧版兼容）
    target_font_path = FONT_BOLD_PATH if bold else FONT_PATH
    if target_font_path.exists():
        try:
            return ImageFont.truetype(str(target_font_path), size)
        except Exception:
            pass

    # 优先级 3: 相反字重回退
    fallback_font_path = FONT_PATH if bold else FONT_BOLD_PATH
    if fallback_font_path.exists():
        try:
            return ImageFont.truetype(str(fallback_font_path), size)
        except Exception:
            pass

    return ImageFont.load_default()


def sanitize_display_text(text: str, preserve_emoji: bool = False) -> str:
    cleaned = str(text or "")
    cleaned = FACE_PLACEHOLDER_PATTERN.sub("", cleaned)
    if not preserve_emoji:
        cleaned = EMOJI_PATTERN.sub("", cleaned)
        cleaned = EMOJI_EXTRA_PATTERN.sub("", cleaned)
    return cleaned.strip()


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    paragraphs = text.replace("\r\n", "\n").split("\n")
    for paragraph in paragraphs:
        if not paragraph:
            lines.append("")
            continue

        current_line = ""
        for char in paragraph:
            test_line = current_line + char
            bbox = font.getbbox(test_line)
            width = (bbox[2] - bbox[0]) if bbox else 0
            if width <= max_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = char
        if current_line:
            lines.append(current_line)
    return lines


def draw_rounded_rectangle(draw: ImageDraw.ImageDraw, xy, corner_radius, fill=None, outline=None):
    draw.rounded_rectangle(xy, radius=corner_radius, fill=fill, outline=outline)


def make_italic(image: Image.Image, skew_factor: float = 0.1) -> Image.Image:
    width, height = image.size
    new_width = width + int(height * abs(skew_factor))
    matrix = (1, skew_factor, 0, 0, 1, 0)
    return image.transform((new_width, height), Image.AFFINE, matrix, resample=Image.BICUBIC)


def _draw_text(image: Image.Image, position: tuple[int, int], text: str, font, fill, emoji_offset_y: int = 0) -> None:
    pilmoji_class = _get_pilmoji_class()
    if pilmoji_class:
        with pilmoji_class(image, emoji_position_offset=(0, emoji_offset_y)) as pilmoji:
            pilmoji.text(position, text, font=font, fill=fill)
        return

    draw = ImageDraw.Draw(image)
    draw.text(position, sanitize_display_text(text), font=font, fill=fill)


def make_dialog_box(text: str, name_w: int) -> Image.Image:
    font_size = 55
    font = load_font(font_size, bold=False)
    preserve_emoji = _get_pilmoji_class() is not None
    display_text = sanitize_display_text(text, preserve_emoji=preserve_emoji)
    lines = wrap_text(display_text, font, 900)

    text_width = 0
    text_height = 0
    line_spacing = 4
    ascent, descent = font.getmetrics()
    line_height = ascent + descent

    for line in lines:
        bbox = font.getbbox(line)
        width = (bbox[2] - bbox[0]) if bbox else 0
        text_width = max(text_width, width)
        text_height += line_height + line_spacing
    if lines:
        text_height -= line_spacing

    box_w = max(text_width, name_w) + 130
    box_h = max(text_height + 103, 150)
    box = Image.new("RGBA", (int(box_w), int(box_h)), (0, 0, 0, 0))

    try:
        corner1 = Image.open(RESOURCES_DIR / "corner1.png").convert("RGBA")
        corner2 = Image.open(RESOURCES_DIR / "corner2.png").convert("RGBA")
        corner3 = Image.open(RESOURCES_DIR / "corner3.png").convert("RGBA")
        corner4 = Image.open(RESOURCES_DIR / "corner4.png").convert("RGBA")
    except FileNotFoundError:
        draw = ImageDraw.Draw(box)
        draw.rounded_rectangle((0, 0, box_w, box_h), radius=20, fill="white")
        return box

    box.paste(corner1, (0, 0))
    box.paste(corner2, (0, int(box_h - 75)))
    box.paste(corner3, (int(box_w - 70), 0))
    box.paste(corner4, (int(box_w - 70), int(box_h - 75)))

    fill_draw = ImageDraw.Draw(box)
    fill_draw.rectangle((65, 20, box_w - 65, box_h - 20), fill="white")
    fill_draw.rectangle((26, 75, box_w - 26, box_h - 75), fill="white")

    text_start_x = 65
    text_start_y = 17 + (box_h - 40 - text_height) // 2
    current_y = text_start_y

    emoji_offset_y = max(1, int(descent * 0.9))
    for line in lines:
        _draw_text(box, (text_start_x, current_y), line, font, "black", emoji_offset_y=emoji_offset_y)
        current_y += line_height + line_spacing

    return box


def render_chat_screenshot(
    name: str,
    avatar_bytes: bytes,
    text: str,
    role: str = "member",
    title: str = "",
    level: int = 0,
    show_title: bool = True,
) -> bytes:
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception:
        avatar = Image.new("RGBA", (135, 135), "gray")

    mask = Image.new("L", (135, 135), 0)
    draw_mask = ImageDraw.Draw(mask)
    draw_mask.ellipse((0, 0, 135, 135), fill=255)
    avatar = avatar.resize((135, 135))
    avatar.putalpha(mask)

    preserve_emoji = _get_pilmoji_class() is not None
    safe_name = sanitize_display_text(name, preserve_emoji=preserve_emoji) or "未知用户"
    safe_title = sanitize_display_text(title, preserve_emoji=preserve_emoji)
    safe_text = sanitize_display_text(text, preserve_emoji=preserve_emoji)

    name_font = load_font(35, bold=False)
    name_bbox = name_font.getbbox(safe_name)
    name_w = name_bbox[2] - name_bbox[0]
    name_h = name_bbox[3] - name_bbox[1]

    label_img = None
    label_w = 0

    if show_title:
        label_bg_color = "#9db2e0"
        if role == "owner":
            label_bg_color = "#fdd93f"
        elif role == "admin":
            label_bg_color = "#3fe3d8"

        label_font = load_font(32, bold=False)
        lv_num_font = load_font(32, bold=True)
        lv_prefix_font = load_font(28, bold=True)

        lv_prefix = "LV"
        lv_num = str(level)
        p_bbox = lv_prefix_font.getbbox(lv_prefix)
        n_bbox = lv_num_font.getbbox(lv_num)
        p_w = p_bbox[2] - p_bbox[0]
        p_h = p_bbox[3] - p_bbox[1]
        n_w = n_bbox[2] - n_bbox[0]
        n_h = n_bbox[3] - n_bbox[1]

        lv_w = p_w + n_w + 4
        lv_h = max(p_h, n_h)
        lv_temp_img = Image.new("RGBA", (lv_w + 40, lv_h + 40), (0, 0, 0, 0))
        lv_temp_draw = ImageDraw.Draw(lv_temp_img)

        n_visual_top = (lv_h + 40 - n_h) // 2
        p_visual_top = n_visual_top + n_h - p_h
        lv_temp_draw.text((20 - p_bbox[0], p_visual_top - p_bbox[1]), lv_prefix, font=lv_prefix_font, fill="white")
        lv_temp_draw.text((20 + p_w + 4 - n_bbox[0], n_visual_top - n_bbox[1]), lv_num, font=lv_num_font, fill="white")

        lv_italic_img = make_italic(lv_temp_img, skew_factor=0.1)
        bbox = lv_italic_img.getbbox()
        if bbox:
            lv_italic_img = lv_italic_img.crop(bbox)

        final_title = safe_title
        has_custom_title = bool(safe_title)
        if not final_title:
            if role == "owner":
                final_title = "群主"
            elif role == "admin":
                final_title = "管理员"
            else:
                if 1 <= level <= 10:
                    final_title = "青铜"
                elif 11 <= level <= 20:
                    final_title = "白银"
                elif 21 <= level <= 40:
                    final_title = "黄金"
                elif 41 <= level <= 60:
                    final_title = "铂金"
                elif 61 <= level <= 80:
                    final_title = "钻石"
                elif level >= 81:
                    final_title = "王者"

        if role == "member" and has_custom_title:
            label_bg_color = "#d38ffe"

        title_img = None
        if final_title:
            title_bbox = label_font.getbbox(final_title)
            title_w = title_bbox[2] - title_bbox[0]
            title_h = title_bbox[3] - title_bbox[1]
            title_img = Image.new("RGBA", (title_w + 20, title_h + 20), (0, 0, 0, 0))
            emoji_offset_y = max(1, int(label_font.getmetrics()[1] * 0.6))
            _draw_text(
                title_img,
                (-title_bbox[0] + 10, -title_bbox[1] + 10),
                final_title,
                label_font,
                "white",
                emoji_offset_y=emoji_offset_y,
            )
            bbox = title_img.getbbox()
            if bbox:
                title_img = title_img.crop(bbox)

        content_w = lv_italic_img.width
        content_h = lv_italic_img.height
        spacing = int(label_font.getlength(" ") * 1.5)
        if title_img:
            content_w += spacing + title_img.width
            content_h = max(content_h, title_img.height)

        label_w = content_w + 28
        label_h = content_h + 20
        label_img = Image.new("RGBA", (int(label_w), int(label_h)), (0, 0, 0, 0))
        label_draw = ImageDraw.Draw(label_img)
        draw_rounded_rectangle(label_draw, (0, 0, label_w, label_h), 12, fill=label_bg_color)

        current_x = (label_w - content_w) / 2
        lv_y = (label_h - lv_italic_img.height) / 2
        label_img.paste(lv_italic_img, (int(current_x), int(lv_y)), mask=lv_italic_img)
        current_x += lv_italic_img.width

        if title_img:
            current_x += spacing
            title_y = (label_h - title_img.height) / 2
            label_img.paste(title_img, (int(current_x), int(title_y)), mask=title_img)

    bubble_x = 165
    badge_x = 195
    box_img = make_dialog_box(safe_text, 0)
    name_x = badge_x + label_w + 10 if show_title and label_img else badge_x

    canvas_w = max(name_x + name_w, bubble_x + box_img.width) + 50
    canvas_h = box_img.height + 110
    canvas = Image.new("RGBA", (int(canvas_w), int(canvas_h)), "#eaedf4")
    canvas.paste(avatar, (20, 20), mask=avatar)
    canvas.paste(box_img, (bubble_x, 82), mask=box_img)
    if show_title and label_img:
        canvas.paste(label_img, (badge_x, 25), mask=label_img)

    name_draw_y = 20 + (35 - name_h) // 2
    emoji_offset_y = max(1, int(name_font.getmetrics()[1] * 0.6))
    _draw_text(canvas, (name_x, name_draw_y), safe_name, name_font, "#868894", emoji_offset_y=emoji_offset_y)

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=90)
    return output.getvalue()


async def get_avatar(user_id: str) -> bytes | None:
    if not user_id.isdigit():
        user_id = "".join(random.choices("0123456789", k=9))

    avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(avatar_url, timeout=10) as resp:
                resp.raise_for_status()
                return await resp.read()
    except Exception as exc:
        logger.error(f"[AntiRevoke] 获取头像失败: {exc}")
        return None


async def get_member_rich_info(client, group_id: int, user_id: int, fallback_name: str = "") -> dict:
    info = None
    try:
        if hasattr(client, "get_group_member_info"):
            info = await client.get_group_member_info(group_id=group_id, user_id=user_id, no_cache=True)
    except TypeError:
        info = await client.get_group_member_info(group_id=group_id, user_id=user_id)
    except Exception:
        info = None

    if not isinstance(info, dict):
        try:
            info = await client.api.call_action(
                "get_group_member_info",
                group_id=group_id,
                user_id=user_id,
                no_cache=True,
            )
        except Exception as exc:
            logger.warning(f"[AntiRevoke] 获取群成员信息失败: {exc}")
            info = None

    info = info or {}
    return {
        "role": info.get("role", "member"),
        "level": int(info.get("level", 0) or 0),
        "title": info.get("title", "") or "",
        "nickname": info.get("card") or info.get("nickname") or fallback_name or str(user_id),
    }


async def generate_text_recall_screenshot(
    client,
    group_id: int,
    user_id: int,
    text: str,
    fallback_name: str = "",
    show_title: bool = True,
) -> bytes | None:
    preserve_emoji = _get_pilmoji_class() is not None
    text = sanitize_display_text(text, preserve_emoji=preserve_emoji)
    if not text:
        logger.warning("[AntiRevoke] 截图: 文本净化后为空")
        return None

    avatar = await get_avatar(str(user_id))
    if not avatar:
        logger.warning(f"[AntiRevoke] 截图: 获取头像失败 (user_id={user_id})")
        return None

    info = await get_member_rich_info(client, group_id, user_id, fallback_name=fallback_name)
    try:
        return render_chat_screenshot(
            name=info["nickname"],
            avatar_bytes=avatar,
            text=text,
            role=info["role"],
            title=info["title"],
            level=info["level"],
            show_title=show_title,
        )
    except Exception as exc:
        logger.exception(f"[AntiRevoke] 渲染文本撤回截图失败: {exc}")
        return None
