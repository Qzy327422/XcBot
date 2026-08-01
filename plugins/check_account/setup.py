# -*- coding: utf-8 -*-
"""QQ 资料查询插件（从 XcBot 主程序内置功能迁移而来）。

原内置实现只在群聊生效，但私聊帮助菜单里却列了该命令；
本插件同时支持群聊和私聊，修复了这个不一致。
"""
import datetime
import json
import re
import traceback
import uuid

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
TRIGGHT_KEYWORD = "开"

HELP_MESSAGE = "/开 [QQ号] —— 查询 QQ 资料（插件）"


def _parse_napcat(user_dict, admin_users):
    try:
        avatar = user_dict.get("avatar", "")
        register_time = user_dict.get("reg_time", "")
        try:
            dt = datetime.datetime.strptime(register_time, "%Y-%m-%dT%H:%M:%SZ")
            register_time = dt.strftime("%Y.%m.%d %H:%M:%S")
        except (ValueError, TypeError):
            register_time = "未知时间"

        is_vip = user_dict.get("is_vip", False)
        vip_level = user_dict.get("vip_level", 0)
        is_year_vip = user_dict.get("is_years_vip", False)

        admins = {str(x) for x in (admin_users or [])}
        status_user = "管理员" if str(user_dict.get("user_id", "未知")) in admins else "普通用户"

        result = f"""昵称: {user_dict.get('nickname', '未知')}
状态: (框架不支持)
QQ号: {user_dict.get('uin', '未知')}
QID: {user_dict.get('qid', '未知')}
性别: {'男' if user_dict.get('sex') == 'male' else '女'}
年龄: {user_dict.get('age', '未知')}
权限: {status_user}
QQ等级: {user_dict.get('qqLevel', '未知')}
个性签名: {user_dict.get('longNick', '暂无签名')}
注册时间: {register_time}
超级会员: {'是' if is_vip else '否'}
会员等级: {vip_level}
年费会员: {'是' if is_year_vip else '否'}"""
        return avatar, result
    except Exception:
        print(f"解析失败: {traceback.format_exc()}")
        return "", "无法打开该用户的账户"


def _parse_generic(user_dict, admin_users):
    try:
        avatar = user_dict.get("avatar", "")
        register_time = user_dict.get("RegisterTime", "")
        try:
            dt = datetime.datetime.strptime(register_time, "%Y-%m-%dT%H:%M:%SZ")
            register_time = dt.strftime("%Y.%m.%d %H:%M:%S")
        except (ValueError, TypeError):
            register_time = "未知时间"

        business = user_dict.get("Business", []) or []
        is_vip = any(item.get("type") == 1 for item in business)
        vip_level = next((item.get("level", 0) for item in business if item.get("type") == 1), 0)
        is_year_vip = any(item.get("isyear") == 1 for item in business if item.get("type") == 1)

        status_msg = (user_dict.get("status") or {}).get("message", "暂无状态")
        admins = {str(x) for x in (admin_users or [])}
        status_user = "管理员" if str(user_dict.get("user_id", "未知")) in admins else "普通用户"

        result = f"""昵称: {user_dict.get('nickname', '未知')}
状态: {status_msg}
QQ号: {user_dict.get('user_id', '未知')}
QID: {user_dict.get('q_id', '未知')}
性别: {'男' if user_dict.get('sex') == 'male' else '女'}
年龄: {user_dict.get('age', '未知')}
权限: {status_user}
QQ等级: {user_dict.get('level', '未知')}
个性签名: {user_dict.get('sign', '暂无签名')}
注册时间: {register_time}
超级会员: {'是' if is_vip else '否'}
会员等级: {vip_level}
年费会员: {'是' if is_year_vip else '否'}"""
        return avatar, result
    except Exception:
        print(f"解析失败: {traceback.format_exc()}")
        return "", "无法打开该用户的账户"


async def _fetch_stranger_info(ws_url: str, uid: int):
    """通过 OneBot WebSocket 直接取陌生人资料。"""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            request_id = str(uuid.uuid4())
            await ws.send_str(json.dumps({
                "action": "get_stranger_info",
                "params": {"user_id": uid, "no_cache": True},
                "echo": request_id,
            }))
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("echo") == request_id:
                        return data.get("data")
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
    return None


def _match(order: str) -> bool:
    """精确匹配：order 等于「开」或以「开 」开头。

    「开」是极常见的词首（开心/开始/开车），子串或裸 startswith 匹配
    会让这些闲聊都被当成查资料命令。
    """
    text = str(order or "").strip()
    return text == "开" or text.startswith("开 ")


async def on_message(order, event, actions, Manager, Segments,
                     bot_name, bot_name_en, ONE_SLOGAN, ws_url,
                     is_group=False, group_id=None, user_id=None, ROOT_User=None,
                     is_feature_enabled=None, log=None, **kwargs):
    order = str(order or "").strip()
    if not _match(order):
        return False

    if is_feature_enabled and not is_feature_enabled("check_account", True):
        return False

    header = f"{bot_name} {bot_name_en} - {ONE_SLOGAN}\n————————————————————\n"

    async def send(message):
        if is_group:
            await actions.send(group_id=group_id, message=message)
        else:
            await actions.send(user_id=user_id, message=message)

    async def send_text(text):
        await send(Manager.Message(Segments.Text(text)))

    # 优先取 @ 的对象，其次取命令后的数字，都没有则查自己
    uid = 0
    for segment in getattr(event, "message", []) or []:
        if isinstance(segment, Segments.At):
            try:
                uid = int(segment.qq)
            except (TypeError, ValueError):
                uid = 0
            break

    if uid == 0:
        uid_str = order[len("开"):].strip() or str(user_id)
        try:
            uid = int(uid_str)
        except (ValueError, TypeError):
            await send_text(f"{header}失败: {uid_str} 不是一个有效的用户")
            return True

    if log:
        log(f"查询 {uid}", "HTTP")

    try:
        user_info = await _fetch_stranger_info(ws_url, uid)
    except Exception as e:
        await send_text(f"{header}失败: 获取用户信息时出错: {e}")
        return True

    if not user_info:
        await send_text(f"{header}失败: 未能获取到 {uid} 的信息，可能不是一个有效的用户。")
        return True

    try:
        framework = await actions.get_version_info()
        app_name = str((framework.data.raw or {}).get("app_name") or "")
    except Exception:
        app_name = ""

    if "NapCat" in app_name:
        avatar, text = _parse_napcat(user_info, ROOT_User)
    else:
        avatar, text = _parse_generic(user_info, ROOT_User)

    message = (Manager.Message(Segments.Image(avatar), Segments.Text(text))
               if avatar else Manager.Message(Segments.Text(text)))
    await send(message)
    return True
