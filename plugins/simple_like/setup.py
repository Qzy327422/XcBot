import json
import os
from datetime import date

TRIGGHT_KEYWORD = "Any"
HELP_MESSAGE = "赞我 → 群聊/私聊发送赞我，机器人为你点赞"

_STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simple_like_stats.json")
_QQ_DAILY_MAX = 50
_LIKE_TIMES = 10


def _load_stats():
    today = str(date.today())
    try:
        if os.path.exists(_STATS_FILE):
            with open(_STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today:
                return today, data.get("counts", {})
    except Exception:
        pass
    return today, {}


def _save_stats(today, counts):
    try:
        with open(_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today, "counts": counts}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _plain_text(event, fallback=""):
    parts = []
    try:
        for segment in event.message:
            text = getattr(segment, "text", None)
            if text:
                parts.append(str(text))
    except Exception:
        pass
    return "".join(parts).strip() or str(fallback).strip()


async def on_message(event, actions, Manager, Segments, user_message="", is_group=False, get_plugin_config=None):
    if _plain_text(event, user_message) != "赞我":
        return False

    user_id = str(event.user_id)
    today, counts = _load_stats()
    current = int(counts.get(user_id, 0) or 0)

    async def reply(text):
        message = Manager.Message(Segments.Text(text))
        if is_group:
            await actions.send(group_id=event.group_id, message=message)
        else:
            await actions.send(user_id=event.user_id, message=message)

    if current >= _QQ_DAILY_MAX:
        await reply("今天已经点过赞了，请明天再来吧~")
        return True

    configured_times = get_plugin_config("like_times", _LIKE_TIMES) if get_plugin_config else _LIKE_TIMES
    if isinstance(configured_times, bool) or not isinstance(configured_times, (int, float)):
        configured_times = _LIKE_TIMES
    elif isinstance(configured_times, float) and not configured_times.is_integer():
        configured_times = _LIKE_TIMES
    times = min(max(1, min(_LIKE_TIMES, int(configured_times))), _QQ_DAILY_MAX - current)

    try:
        await actions.custom.send_like(user_id=int(user_id), times=times)
        counts[user_id] = current + times
        _save_stats(today, counts)
        await reply("点赞完成~")
    except Exception as e:
        error = str(e)
        if "上限" in error or "点赞失败" in error:
            counts[user_id] = _QQ_DAILY_MAX
            _save_stats(today, counts)
            await reply("今天已经点过赞了，请明天再来吧~")
        elif "No data returned" in error:
            counts[user_id] = current + times
            _save_stats(today, counts)
            await reply("点赞完成~")
        else:
            await reply("点赞失败，请稍后再试")

    return True
