#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉订阅日历推送 + 多城市天气（高德）GitHub Actions 版
- 根据北京时间 21 点自动切换查询今日/明日日程
- 天气：今日查实时，明日查预报
- 自动识别运行模式，显示对应推送标识
所有配置通过环境变量提供。
"""
import os
import sys
import time
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------- 检测运行模式 ----------
def is_auto_run() -> bool:
    """判断是否在自动化环境（如 GitHub Actions）中运行"""
    # GitHub Actions 设置 GITHUB_ACTIONS=true，常见 CI 设置 CI=true
    return os.getenv("GITHUB_ACTIONS", "").lower() == "true" or os.getenv("CI", "").lower() == "true"

# ---------- 强制环境变量读取 ----------
def get_config(key: str) -> str:
    value = os.getenv(key)
    if not value:
        logger.error("缺少必要环境变量：%s", key)
        sys.exit(1)
    return value

DINGTALK_APP_KEY = get_config("DINGTALK_APP_KEY")
DINGTALK_APP_SECRET = get_config("DINGTALK_APP_SECRET")
USER_ID = get_config("DINGTALK_USER_ID")
CALENDAR_ID = get_config("DINGTALK_CALENDAR_ID")
WEBHOOK_URL = get_config("DINGTALK_WEBHOOK_URL")
WEATHER_API_KEY = get_config("WEATHER_API_KEY")
WEATHER_CITIES = get_config("WEATHER_CITIES")
WEATHER_CITY = os.getenv("WEATHER_CITY", "")

# ---------- 工具函数 ----------
def parse_city_entry(entry: str) -> Tuple[str, Optional[str]]:
    if not entry:
        return "", None
    entry = entry.strip()
    if ":" in entry:
        parts = entry.split(":", 1)
        return parts[0].strip(), parts[1].strip() or None
    return entry, None

# ---------- 钉钉 Token 管理 ----------
class TokenManager:
    _token_info: Dict = {}
    @classmethod
    def get_token(cls) -> str:
        now = time.time()
        if cls._token_info.get("token") and cls._token_info.get("expires_at", 0) > now + 60:
            return cls._token_info["token"]
        logger.info("获取 access_token...")
        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        payload = {"appKey": DINGTALK_APP_KEY, "appSecret": DINGTALK_APP_SECRET}
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        token = data["accessToken"]
        cls._token_info = {"token": token, "expires_at": now + data.get("expireIn", 7200) - 300}
        return token

def get_unionid(user_id: str) -> str:
    url = "https://oapi.dingtalk.com/topapi/v2/user/get"
    params = {"access_token": TokenManager.get_token()}
    payload = {"userid": user_id}
    resp = requests.post(url, params=params, json=payload, timeout=10)
    data = resp.json()
    if data.get("errcode") != 0:
        raise Exception(f"获取 unionId 失败: {data.get('errmsg')}")
    return data["result"]["unionid"]

def get_events(union_id: str, calendar_id: str, time_min: str, time_max: str) -> List[Dict]:
    headers = {
        "x-acs-dingtalk-access-token": TokenManager.get_token(),
        "Content-Type": "application/json",
    }
    params = {"timeMin": time_min, "timeMax": time_max}
    url = f"https://api.dingtalk.com/v1.0/calendar/users/{union_id}/calendars/{calendar_id}/events"
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("events") or data.get("result", {}).get("events", [])

# ---------- 格式化日程（原始版本，无去重） ----------
def format_events(events: List[Dict]) -> str:
    if not events:
        return "✅ 暂无日程安排，祝你顺利！🎉"

    lines = []
    def fmt_datetime(value):
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M")
        except Exception:
            return value

    for i, ev in enumerate(events, 1):
        title = ev.get("summary") or ev.get("title") or "未命名日程"
        start_obj = ev.get("start") or {}
        end_obj = ev.get("end") or {}
        start_dt = start_obj.get("dateTime")
        end_dt = end_obj.get("dateTime")
        start_date = start_obj.get("date")
        end_date = end_obj.get("date")
        if start_dt and end_dt:
            time_range = f"{fmt_datetime(start_dt)}-{fmt_datetime(end_dt)}"
        elif start_date and end_date:
            if start_date == end_date:
                time_range = "全天"
            else:
                time_range = f"全天（{start_date} ~ {end_date}）"
        else:
            time_range = "全天"
        loc = (ev.get("location") or {}).get("displayName", "")
        lines.append(
            f"**{i}. {title}**  \n"
            f"   ⏰ {time_range}  \n"
            f"   📍 {loc or '未指定地点'}"
        )
    return "\n\n".join(lines)

# ---------- 天气查询（支持今日实时 / 明日预报）----------
def get_weather(city: str, api_key: str, display_name: Optional[str] = None, target_date: Optional[datetime.date] = None) -> Optional[str]:
    if not api_key or not city:
        return None
    today = datetime.now().date()
    if target_date is None:
        target_date = today
    try:
        if target_date == today:
            url = "https://restapi.amap.com/v3/weather/weatherInfo"
            params = {"key": api_key, "city": city, "extensions": "base"}
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "1" or not data.get("lives"):
                logger.error("高德实时天气返回错误: %s", data)
                return None
            live = data["lives"][0]
            city_name = display_name if display_name else live.get("city", city)
            weather = live.get("weather", "未知")
            temperature = live.get("temperature", "N/A")
            humidity = live.get("humidity", "N/A")
            wind_direction = live.get("winddirection", "未知")
            wind_power = live.get("windpower", "N/A")
            report_time = live.get("reporttime", "")
            return (
                f"🌤 **{city_name}天气**  \n"
                f"   🌡 温度：{temperature}℃  \n"
                f"   ☁️ 天气：{weather}  \n"
                f"   💧 湿度：{humidity}%  \n"
                f"   🌬 风力：{wind_direction}{wind_power}级  \n"
                f"   🕒 更新：{report_time}"
            )
        else:
            url = "https://restapi.amap.com/v3/weather/weatherInfo"
            params = {"key": api_key, "city": city, "extensions": "all"}
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "1" or not data.get("forecasts"):
                logger.error("高德预报返回错误: %s", data)
                return None
            forecast = data["forecasts"][0]
            casts = forecast.get("casts", [])
            target_str = target_date.strftime("%Y-%m-%d")
            cast = next((c for c in casts if c.get("date") == target_str), None)
            if not cast:
                logger.warning("未找到 %s 的预报数据", target_str)
                return None
            city_name = display_name if display_name else forecast.get("city", city)
            day_weather = cast.get("dayweather", "未知")
            day_temp = cast.get("daytemp", "N/A")
            night_temp = cast.get("nighttemp", "N/A")
            day_wind = cast.get("daywind", "未知")
            day_power = cast.get("daypower", "N/A")
            if target_date == today + timedelta(days=1):
                label = "明日"
            elif target_date == today + timedelta(days=2):
                label = "后天"
            else:
                label = target_str
            return (
                f"🌤 **{city_name}天气 ({label})**  \n"
                f"   🌡 温度：{day_temp}℃（夜间{night_temp}℃）  \n"
                f"   ☁️ 天气：{day_weather}  \n"
                f"   🌬 风力：{day_wind}{day_power}级  \n"
                f"   📅 {target_str}"
            )
    except Exception as e:
        logger.error("获取 %s 天气失败: %s", city, e)
        return None

def get_weather_multi(cities_str: str, api_key: str, target_date: Optional[datetime.date] = None) -> Optional[str]:
    if not api_key or not cities_str:
        return None
    entries = [e.strip() for e in cities_str.split(";") if e.strip()]
    if not entries:
        return None
    weather_parts = []
    for entry in entries:
        city_query, display_name = parse_city_entry(entry)
        if not city_query:
            continue
        weather = get_weather(city_query, api_key, display_name, target_date)
        if weather:
            weather_parts.append(weather)
    return "\n\n".join(weather_parts) if weather_parts else None

# ---------- 推送消息（支持自动/手动标识） ----------
def send_markdown(webhook: str, title: str, content: str, auto: bool = False) -> bool:
    # 根据运行模式设置尾部消息
    footer = "🤖 钉钉日历机器人自动推送" if auto else "🤖 钉钉日历机器人手动推送"
    text = (
        f"### {title}\n\n"
        f"{content}\n\n---\n"
        f"> {footer}\n"
        f"> 📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
        "at": {"atMobiles": [], "isAtAll": False},
    }
    resp = requests.post(webhook, json=payload, timeout=10)
    result = resp.json()
    if result.get("errcode") == 0:
        logger.info("推送成功")
        return True
    else:
        logger.error("推送失败: %s", result.get("errmsg"))
        return False

def get_query_range():
    utc_now = datetime.utcnow()
    beijing_now = utc_now + timedelta(hours=8)
    if beijing_now.hour >= 21:
        target_date = beijing_now.date() + timedelta(days=1)
    else:
        target_date = beijing_now.date()
    beijing_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    beijing_end = beijing_start + timedelta(days=1)
    start_utc = beijing_start - timedelta(hours=8)
    end_utc = beijing_end - timedelta(hours=8)
    return start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), target_date

def main():
    logger.info("开始执行日历推送任务")
    auto = is_auto_run()
    logger.info("运行模式: %s", "自动" if auto else "手动")
    try:
        start_utc, end_utc, target_date = get_query_range()
        union_id = get_unionid(USER_ID)
        events = get_events(union_id, CALENDAR_ID, start_utc, end_utc)

        if WEATHER_CITIES:
            weather_str = get_weather_multi(WEATHER_CITIES, WEATHER_API_KEY, target_date)
        elif WEATHER_CITY:
            city_query, display_name = parse_city_entry(WEATHER_CITY)
            weather_str = get_weather(city_query, WEATHER_API_KEY, display_name, target_date)
        else:
            weather_str = None

        content_parts = []
        if weather_str:
            content_parts.append(weather_str)
            content_parts.append("")
        content_parts.append("**📅 日程安排**  \n" + format_events(events))
        content = "\n".join(content_parts)

        title = f"📅 每日提醒 - {target_date.strftime('%m月%d日')}"
        if not send_markdown(WEBHOOK_URL, title, content, auto):
            sys.exit(1)
        logger.info("推送完成")
    except Exception as e:
        logger.error("任务失败: %s", e, exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
