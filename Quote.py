from Hyper import Segments
from Hyper.Events import gen_message
from PIL import Image, ImageDraw, ImageFont
import os
import uuid
import asyncio
from io import BytesIO
import httpx
import emoji as emoji_lib

TEMP_DIR = "./temps"
SPECIAL_MASK_UIDS = {1348472639}


async def fetch_image_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def open_image_from_url(url: str) -> Image.Image:
    data = await fetch_image_bytes(url)
    return Image.open(BytesIO(data))


def square_scale(image: Image.Image, height: int) -> Image.Image:
    old_width, old_height = image.size
    if old_height <= 0:
        return image
    ratio = height / old_height
    width = max(1, int(old_width * ratio))
    return image.resize((width, height))


def _is_emoji_char(char: str) -> bool:
    if not char:
        return False
    if char in emoji_lib.EMOJI_DATA:
        return True
    code = ord(char)
    emoji_ranges = (
        (0x1F300, 0x1F5FF),
        (0x1F600, 0x1F64F),
        (0x1F680, 0x1F6FF),
        (0x1F700, 0x1F77F),
        (0x1F900, 0x1F9FF),
        (0x1FA70, 0x1FAFF),
        (0x2600, 0x26FF),
        (0x2700, 0x27BF),
    )
    return any(lo <= code <= hi for lo, hi in emoji_ranges)


def _select_font(char, fonts):
    if char.isdigit() or char == '.':
        return fonts["digit"], (255, 0, 0)
    if _is_emoji_char(char):
        return fonts["emoji"], (255, 255, 255)
    return fonts["title"], (255, 255, 255)


def _render_to_file(quote, head_img, name, uin, out_path):
    mask_path = "assets/quote/mask.png"
    if uin in SPECIAL_MASK_UIDS:
        mask_path = "assets/quote/maskrbc.png"

    mask = Image.open(mask_path).convert("RGBA")
    background = Image.new('RGBA', mask.size, (255, 255, 255, 255))
    head = head_img.convert("RGBA")

    fonts = {
        "title": ImageFont.truetype(r"assets/t.ttf", size=36),
        "desc": ImageFont.truetype(r"assets/n.ttf", size=30),
        "digit": ImageFont.truetype(r"assets/sz.ttf", size=36),
        "emoji": ImageFont.truetype(r"assets/e.ttf", size=36),
    }

    background.paste(square_scale(head, 640), (0, 0))
    background.paste(mask, (0, 0), mask)

    draw = ImageDraw.Draw(background)

    text_left = 640
    text_right = mask.size[0] - 20
    available_width = max(100, text_right - text_left)

    x_offset = text_left
    y_offset = 165
    line_height = 40

    for char in quote:
        if char in ("\n", "\r"):
            x_offset = text_left
            y_offset += line_height
            continue

        font, fill_color = _select_font(char, fonts)
        try:
            char_width = font.getlength(char)
        except Exception:
            char_width = font.size

        if x_offset + char_width > text_left + available_width:
            x_offset = text_left
            y_offset += line_height

        draw.text((x_offset, y_offset), char, font=font, fill=fill_color)
        x_offset += char_width

    name_x = 862 if len(name) >= 7 else 1000
    draw.text((name_x, 465), f"——{name}", font=fonts["desc"], fill=(112, 112, 112))

    nbg = Image.new('RGB', mask.size, (0, 0, 0))
    nbg.paste(background, (0, 0))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    nbg.save(out_path)


async def get_image(quote, ava_url, name, uin, out_path):
    head_img = await open_image_from_url(ava_url)
    await asyncio.to_thread(_render_to_file, quote, head_img, name, uin, out_path)


async def handle(message, actions, images=None):
    if not message or not isinstance(message[0], Segments.Reply):
        return None

    msg_id = message[0].id
    content = await actions.get_msg(msg_id)
    sender = content.data["sender"]
    name = sender.get("card") or sender.get("nickname") or str(sender.get("user_id", ""))
    uin = sender["user_id"]

    raw_message = content.data["message"]
    message_obj = gen_message({"message": raw_message})
    text = str(message_obj).replace("[图片]", "")

    ava_url = images if images else f"http://q2.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640"

    out_path = os.path.join(TEMP_DIR, f"quote_{uuid.uuid4().hex}.png")
    await get_image(text, ava_url, name, uin, out_path)

    return Segments.Image(f"file://{os.path.abspath(out_path)}")
