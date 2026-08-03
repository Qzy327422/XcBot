# -*- coding: utf-8 -*-
"""生图/搜图插件（从 XcBot 主程序内置功能迁移而来）。

图源：lolicon（Pixiv 插画）、yuanxiapi（动漫/风景），均免费无需 API Key。
"""
import re
import time

import aiohttp

# ==================== 主程序版本检查 ====================
MIN_XCBOT_VERSION = "v2.4"


def _parse_ver(text) -> tuple:
    """把 "v2.4.1" 解析成 (2, 4, 1)；字符串比较会让 v2.10 小于 v2.4。"""
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(x) for x in nums) if nums else (0,)


def _check_version():
    current = globals().get("XCBOT_VERSION")
    if not current:
        raise RuntimeError(
            f"无法获取主程序版本，本插件需要 XcBot >= {MIN_XCBOT_VERSION}，"
            "请升级主程序后重试"
        )
    if _parse_ver(current) < _parse_ver(MIN_XCBOT_VERSION):
        raise RuntimeError(
            f"主程序版本过低：当前 {current}，本插件需要 >= {MIN_XCBOT_VERSION}"
        )


_check_version()

# ==================== 插件元信息 ====================
TRIGGHT_KEYWORD = "生图"

HELP_MESSAGE = "/生图 [搜索词] —— 搜图/发图（插件）"

# 个人冷却，管理员豁免。原主程序用全局 cooldowns，迁移后由插件自管理。
COOLDOWN_SECONDS = 18
_cooldowns: dict[str, float] = {}

SENSITIVE_KEYWORDS = ["r18", "r-18", "成人", "nsfw", "エロ", "h", "性", "汁液", "胖次", "内裤", "内衣"]
SENSITIVE_TAGS = ["r-18", "r-18g", "r18", "成人", "nsfw"]

SEARCH_APIS = [
    {"url": "https://api.lolicon.app/setu/v2", "method": "GET", "key": "data",
     "array_key": True, "subkey": "urls.original", "type": "Pixiv插画", "search_param": "tag"},
    {"url": "https://api.yuanxiapi.cn/api/img", "method": "GET", "key": "imgurl",
     "type": "动漫", "params": {"type": "dongman"}},
    {"url": "https://api.yuanxiapi.cn/api/img", "method": "GET", "key": "imgurl",
     "type": "风景", "params": {"type": "fengjing"}},
]

SEARCH_MAPPING = {
    "动漫": "dongman", "二次元": "dongman", "动画": "dongman", "卡通": "dongman",
    "猫娘": "dongman", "兽耳": "dongman", "白毛": "dongman", "少女": "dongman",
    "萝莉": "dongman", "御姐": "dongman", "原神": "dongman", "东方": "dongman",
    "风景": "fengjing", "景色": "fengjing", "自然": "fengjing", "山水": "fengjing",
    "星空": "fengjing", "天空": "fengjing", "大海": "fengjing", "森林": "fengjing",
}

REQUEST_TIMEOUT = 10


async def _search_image(search_query: str, clean, log):
    """按顺序尝试图源，返回 (是否成功, 图片URL, 附加说明)。"""
    search_query = clean(search_query)
    if log:
        log(f"search {search_query}", "HTTP")

    for api in SEARCH_APIS:
        try:
            params = api.get("params", {}).copy()

            if search_query and search_query != "随机":
                if "lolicon" in api["url"]:
                    params["tag"] = search_query
                    params.update({"num": 1, "r18": 0, "excludeAI": 0})
                elif api.get("search_param"):
                    for keyword, api_type in SEARCH_MAPPING.items():
                        if keyword in search_query and "type" in params:
                            params["type"] = api_type
                            break

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

            # 不能用 TCPConnector(ssl=False)：那样所有 HTTPS 连接都不校验证书，
            # 任何能中间人的网络位置都可以替换图源返回内容。
            # 某个图源证书确实有问题时应该换源，而不是全局关掉校验。
            async with aiohttp.ClientSession() as session:
                if api["method"] == "POST":
                    response = await session.post(api["url"], params=params, headers=headers, timeout=REQUEST_TIMEOUT)
                else:
                    response = await session.get(api["url"], params=params, headers=headers, timeout=REQUEST_TIMEOUT)

                if response.status != 200:
                    continue
                data = await response.json()

                if api.get("array_key", False):
                    items = (data or {}).get("data") or []
                    if not items:
                        continue
                    item = items[0]

                    tags = [str(t).lower() for t in (item.get("tags") or [])]
                    if any(any(s in tag for s in SENSITIVE_TAGS) for tag in tags):
                        continue

                    if api.get("subkey"):
                        value = item
                        for key in api["subkey"].split("."):
                            if value and key in value:
                                value = value[key]
                            else:
                                value = None
                                break
                        if value:
                            info = f"Pixiv作品\n标题：{item.get('title', '未知')}\n作者：{item.get('author', '未知')}"
                            return True, str(value), clean(info)

                elif api["key"] in (data or {}):
                    image_url = data[api["key"]]
                    if image_url:
                        return True, str(image_url), f"来自 {api['type']} API"
        except Exception:
            continue

    return False, "", f"未找到与【{search_query}】相关的图片"


def _is_admin(user_id, admins) -> bool:
    return str(user_id) in {str(x) for x in (admins or [])}


def _match(order: str) -> bool:
    """精确匹配：order 等于「生图」或以「生图 」开头。

    子串匹配会让「生图好玩吗」这类闲聊误触发搜图。
    """
    text = str(order or "").strip()
    return text == "生图" or text.startswith("生图 ")


async def on_message(order, actions, Manager, Segments, bot_name,
                     is_group=False, group_id=None, user_id=None, ADMINS=None,
                     filter_sensitive_content=None, is_feature_enabled=None,
                     log=None, **kwargs):
    order = str(order or "").strip()
    if not _match(order):
        return False

    if is_feature_enabled and not is_feature_enabled("image_generation", True):
        return False

    clean = filter_sensitive_content or (lambda x: x)

    async def send(*segments):
        msg = Manager.Message(*segments)
        if is_group:
            return await actions.send(group_id=group_id, message=msg)
        return await actions.send(user_id=user_id, message=msg)

    async def send_text(text):
        return await send(Segments.Text(text))

    # 精确匹配后参数就是「生图 」之后的部分，不用 replace：
    # 搜索词本身可能含「生图」二字
    search_query = order[len("生图"):].strip() or "随机"
    cd_key = str(user_id)
    now = time.time()

    last = _cooldowns.get(cd_key)
    if last is not None and now - last < COOLDOWN_SECONDS and not _is_admin(user_id, ADMINS):
        remaining = COOLDOWN_SECONDS - (now - last)
        await send_text(f"{COOLDOWN_SECONDS}秒个人CD，请等待 {remaining:.1f} 秒后重试")
        return True

    if any(keyword in search_query.lower() for keyword in SENSITIVE_KEYWORDS):
        await send_text("搜索词包含敏感内容，请更换其他搜索词 (╥﹏╥)")
        return True

    tip = await send_text(f"{bot_name}正在搜索图片【{search_query}】 ヾ(≧▽≦*)o")

    try:
        success, image_url, image_info = await _search_image(search_query, clean, log)

        if success and image_url:
            parts = []
            try:
                parts.append(Segments.Image(image_url))
            except Exception:
                try:
                    parts.append(Segments.Image(file=image_url))
                except Exception:
                    pass

            if image_info:
                short_info = "\n".join(image_info.split("\n")[:2])
                parts.append(Segments.Text(f"\n{clean(short_info)}"))
            parts.append(Segments.Text(f"\n✨ 图片生成完成！【{search_query}】"))

            await send(*parts)
            _cooldowns[cd_key] = now
        else:
            await send_text(f"未找到与【{search_query}】相关的图片，请尝试其他搜索词 (╥﹏╥)")
    except Exception as e:
        if log:
            log(f"生图失败: {e}", "ERROR")
        await send_text(f"图片搜索出错了，请稍后再试 (╥﹏╥)")
    finally:
        # 撤回"正在搜索"提示，失败无所谓
        try:
            await actions.del_message(tip.data.message_id)
        except Exception:
            pass

    return True
