# -*- coding: utf-8 -*-
"""天气查询插件（从 XcBot 主程序内置功能迁移而来）。

数据源：open-meteo（免费，无需 API Key）。
"""
import re

import aiohttp

# ==================== 主程序版本检查 ====================
# XCBOT_VERSION 由主程序 load_plugins 在 exec_module 之前注入。
# 版本不满足时在模块顶层抛异常，load_plugins 会捕获并计入 failed_plugins，
# WebUI 插件页可以直接看到失败原因。
MIN_XCBOT_VERSION = "v2.4"


def _parse_ver(text) -> tuple:
    """把 "v2.4.1" 解析成 (2, 4, 1)。

    不能直接比较字符串：字典序下 "v2.10" < "v2.4"，结论是错的。
    """
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
TRIGGHT_KEYWORD = "天气"

HELP_MESSAGE = "/天气 [城市] —— 查询天气（插件）"

WEATHER_CODE_MAP = {
    0: "晴朗", 1: "大部晴朗", 2: "局部多云", 3: "阴", 45: "有雾", 48: "冻雾",
    51: "小毛毛雨", 53: "毛毛雨", 55: "强毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒", 80: "阵雨", 81: "中等阵雨",
    82: "强阵雨", 85: "阵雪", 86: "强阵雪", 95: "雷暴", 96: "雷暴伴小冰雹", 99: "强雷暴伴冰雹",
}

GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = 20


def _normalize_location_query(name: str) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    for suffix in ("特别行政区", "自治区", "自治州", "自治县", "省", "市", "区", "县", "镇", "乡"):
        if text.endswith(suffix.lower()):
            text = text[: -len(suffix)]
            break
    return text


def _pick_best_location(results: list, city_name: str):
    if not isinstance(results, list) or not results:
        return None

    target = _normalize_location_query(city_name)

    def exact_match(item: dict) -> bool:
        """只允许精确匹配，避免"南通"被模糊匹配到"通海"。"""
        name = _normalize_location_query(item.get("name", ""))
        admin1 = _normalize_location_query(item.get("admin1", ""))
        country = _normalize_location_query(item.get("country", ""))
        country_code = str(item.get("country_code", "") or "").upper()

        exact_names = {name}
        if admin1 and name:
            exact_names.add(f"{admin1}{name}")
        if country and admin1 and name:
            exact_names.add(f"{country}{admin1}{name}")
        if country_code and admin1 and name:
            exact_names.add(f"{country_code.lower()}{admin1}{name}")
        return bool(target and target in exact_names)

    exact_results = [item for item in results if isinstance(item, dict) and exact_match(item)]
    if not exact_results:
        return None

    def score(item: dict) -> tuple:
        is_cn = 1 if str(item.get("country_code", "") or "").upper() == "CN" else 0
        return (is_cn, float(item.get("population") or 0))

    return max(exact_results, key=score)


def _format_weather(weather_data: dict, city_name: str, clean) -> str:
    try:
        display_city = weather_data.get("display_name") or city_name
        current = weather_data.get("current") or {}
        daily = weather_data.get("daily") or {}
        result = f"🌤️ {display_city} 天气预报\n" + "=" * 45 + "\n"

        temp = current.get("temperature_2m")
        humidity = current.get("relative_humidity_2m")
        code = current.get("weather_code")
        wind = current.get("wind_speed_10m")
        code_text = WEATHER_CODE_MAP.get(code, f"天气代码 {code}") if code is not None else "天气未知"

        if any(v is not None for v in [temp, humidity, code, wind]):
            result += f"📍 实时: {code_text}"
            if temp is not None:
                result += f" | 🌡️ {temp}°C"
            result += "\n"
            if wind is not None:
                result += f"💨 风速: {wind} km/h\n"
            if humidity is not None:
                result += f"💧 湿度: {humidity}%\n"

        dates = daily.get("time") or []
        codes = daily.get("weather_code") or []
        temp_max_list = daily.get("temperature_2m_max") or []
        temp_min_list = daily.get("temperature_2m_min") or []
        shown_days = dates[:3]

        if shown_days:
            result += "─" * 45 + "\n"

        for i, fx_date in enumerate(shown_days):
            date_display = f"{fx_date[5:7]}/{fx_date[8:10]}" if isinstance(fx_date, str) and len(fx_date) >= 10 else str(fx_date)
            day_label = ["今天", "明天", "后天"][i] if i < 3 else date_display
            day_code = codes[i] if i < len(codes) else None
            temp_max = temp_max_list[i] if i < len(temp_max_list) else None
            temp_min = temp_min_list[i] if i < len(temp_min_list) else None
            day_text = WEATHER_CODE_MAP.get(day_code, f"天气代码 {day_code}") if day_code is not None else "天气未知"
            result += f"📅 {day_label} ({date_display})\n"
            if temp_min is not None and temp_max is not None:
                result += f"🌡️ 温度: {temp_min}°C ~ {temp_max}°C\n"
            result += f"☀️ 天气: {day_text}\n"
            if i < len(shown_days) - 1:
                result += "─" * 45 + "\n"

        return clean(result.rstrip())
    except Exception as e:
        return f"❌ 天气查询失败: {clean(str(e))}"


async def _get_weather(city_name: str, clean, log) -> str:
    try:
        city_name = (city_name or "").strip()
        if not city_name:
            return "❌ 请输入要查询的城市名称"

        if log:
            log(f"geo {city_name}", "HTTP")

        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                GEO_API,
                params={"name": city_name, "count": 10, "language": "zh", "format": "json"},
                headers={"Accept": "application/json"},
            ) as response:
                if response.status != 200:
                    return f"❌ 城市查询失败 (HTTP {response.status})"
                geo_data = await response.json()

            location = _pick_best_location(geo_data.get("results") or [], city_name)
            if not location:
                return f"❌ 未找到城市“{city_name}”，请尝试更具体的名称"

            latitude = location.get("latitude")
            longitude = location.get("longitude")
            if latitude is None or longitude is None:
                return f"❌ 未找到城市坐标：{clean(city_name)}"

            display_name = " · ".join([
                str(x) for x in [location.get("name"), location.get("admin1"), location.get("country")] if x
            ]) or city_name

            if log:
                log(f"weather {display_name}", "HTTP")

            async with session.get(
                FORECAST_API,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai",
                    "forecast_days": 3,
                },
                headers={"Accept": "application/json"},
            ) as response:
                if response.status != 200:
                    return f"❌ 天气服务暂时不可用 (HTTP {response.status})"
                weather_data = await response.json()

            weather_data["display_name"] = display_name
            return _format_weather(weather_data, city_name, clean)
    except Exception as e:
        return f"❌ 天气查询失败: {clean(str(e))}"


def _match(order: str) -> bool:
    """精确匹配：order 等于「天气」或以「天气 」开头。

    不能用 `"天气" in order` 子串匹配——@机器人 / 名字触发 / 私聊纯文本这些路径下
    order 是不带命令前缀的裸文本，「天气真好」会被当成查询「真好」的天气。
    """
    text = str(order or "").strip()
    return text == "天气" or text.startswith("天气 ")


async def on_message(order, actions, Manager, Segments, bot_name, reminder,
                     is_group=False, group_id=None, user_id=None,
                     filter_sensitive_content=None, is_feature_enabled=None,
                     log=None, **kwargs):
    order = str(order or "").strip()
    if not _match(order):
        return False

    # 沿用主程序的功能开关，WebUI 里关掉 weather 后插件同样不响应
    if is_feature_enabled and not is_feature_enabled("weather", True):
        return False

    clean = filter_sensitive_content or (lambda x: x)

    async def send(text):
        msg = Manager.Message(Segments.Text(text))
        if is_group:
            await actions.send(group_id=group_id, message=msg)
        else:
            await actions.send(user_id=user_id, message=msg)

    # 精确匹配后参数就是「天气 」之后的部分，不能再用 replace：
    # 城市名本身可能含「天气」二字，replace 会把它一起删掉
    city_name = order[len("天气"):].strip()
    if not city_name:
        await send(f"请指定城市名称，例如：{reminder}天气 北京")
        return True

    await send(f"{bot_name}正在查询 {city_name} 的天气... ☁️")
    await send(await _get_weather(city_name, clean, log))
    return True
