#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉订阅日历推送机器人 (GitHub Actions 版)
功能: 根据运行时间自动推送当天/次日日程 + 多城市天气（高德）
- 白天（<21点）：当日实时天气 + 当日定时日程（不含全天）
- 晚上（>=21点）：次日天气预报 + 次日定时日程（不含全天）
- 自动识别是否在预设时间（08:00 或 22:00）运行，消息尾部显示对应标识
"""

import os
import sys
import time
import logging
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

# ---------- 环境变量读取 ----------
def get_env_or_fail(key: str) -> str:
    value = os.getenv(key)
    if not value:
        logger.error("环境变量 %s 未设置", key)
        sys.exit(1)
    return value

DINGTALK_APP_KEY = get_env_or_fail("DINGTALK_APP_KEY")
DINGTALK_APP_SECRET = get_env_or_fail("DINGTALK_APP_SECRET")
USER_ID = get_env_or_fail("DINGTALK_USER_ID")
CALENDAR_ID = get_env_or_fail("DINGTALK_CALENDAR_ID")
WEBHOOK_URL = get_env_or_fail("DINGTALK_WEBHOOK_URL")
WEATHER_API_KEY = get_env_or_fail("WEATHER_API_KEY")
WEATHER_CITIES = get_env_or_fail("WEATHER_CITIES")
WEATHER_CITY = os.getenv("WEATHER_CITY", "")

# ---------- 判断是否在预设时间运行 ----------
def is_scheduled_run() -> bool:
    beijing_now = datetime.utcnow() + timedelta(hours=8)
    current_minutes = beijing_now.hour * 60 + beijing_now.minute
    scheduled_times = [8 * 60, 22 * 60]  # 08:00 和 22:00
    tolerance = 5  # 容差分钟数
    for target in scheduled_times:
        if abs(current_minutes - target) <= tolerance:
            logger.info("当前时间 %02d:%02d 在预定时间 %02d:00 附近，视为自动运行",
                        beijing_now.hour, beijing_now.minute, target//60)
            return True
    logger.info("当前时间 %02d:%02d 不在预设自动运行时间内，视为手动运行",
                beijing_now.hour, beijing_now.minute)
    return False

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
class DingTalkTokenManager:
    _token_info: Dict = {}
    @classmethod
    def get_access_token(cls) -> str:
        now = time.time()
        if cls._token_info.get("token") and cls._token_info.get("expires_at", 0) > now + 60:
            return cls._token_info["token"]
        logger.info("获取新版 access_token...")
        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        payload = {"appKey": DINGTALK_APP_KEY, "appSecret": DINGTALK_APP_SECRET}
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "accessToken" not in data:
            raise ValueError(f"响应无 accessToken: {data}")
        token = data["accessToken"]
        expires_in = data.get("expireIn", 7200)
        cls._token_info = {"token": token, "expires_at": now + expires_in - 300}
        logger.info("access_token 获取成功，有效期 %d 秒", expires_in)
        return token

# ---------- 钉钉日历客户端 ----------
class DingTalkCalendarClient:
    BASE_URL = "https://api.dingtalk.com/v1.0"
    def _get_headers(self):
        return {"x-acs-dingtalk-access-token": DingTalkTokenManager.get_access_token(), "Content-Type": "application/json"}

    def get_user_unionid(self, user_id: str) -> str:
        url = "https://oapi.dingtalk.com/topapi/v2/user/get"
        params = {"access_token": DingTalkTokenManager.get_access_token()}
        payload = {"userid": user_id}
        resp = requests.post(url, params=params, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") != 0:
            raise Exception(f"获取 unionId 失败: {data.get('errmsg')}")
        return data["result"]["unionid"]

    def get_events(self, union_id: str, calendar_id: str, time_min: str, time_max: str) -> List[Dict]:
        params = {"timeMin": time_min, "timeMax": time_max}
        url = f"{self.BASE_URL}/calendar/users/{union_id}/calendars/{calendar_id}/events"
        resp = requests.get(url, headers=self._get_headers(), params=params, timeout=15)
        data = resp.json()
        events = data.get("events") or data.get("result", {}).get("events", [])
        return events

# ---------- 日程格式化（过滤全天日程） ----------
def format_events(events: List[Dict]) -> str:
    # 过滤：只保留有 dateTime 的定时日程（排除全天事件）
    filtered = []
    for ev in events:
        start = ev.get("start", {})
        if isinstance(start, dict) and start.get("dateTime"):
            filtered.append(ev)
        else:
            # 跳过全天日程（只有 date 字段）
            logger.debug("跳过全天日程: %s", ev.get("summary", "未命名"))

    if not filtered:
        return "✅ 今日无定时日程安排，祝你顺利！🎉"

    lines = []
    for i, ev in enumerate(filtered, 1):
        title = ev.get("summary") or ev.get("title") or "未命名日程"
        start = ev.get("start", {})
        end = ev.get("end", {})
        def fmt(t):
            try:
                return datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%H:%M")
            except:
                return ""
        start_str = fmt(start.get("dateTime", "")) if isinstance(start, dict) else ""
        end_str = fmt(end.get("dateTime", "")) if isinstance(end, dict) else ""
        time_range = f"{start_str}-{end_str}" if start_str and end_str else "全天"
        loc = ev.get("location", {})
        loc_display = loc.get("displayName", "") if isinstance(loc, dict) else ""
        lines.append(f"**{i}. {title}**  \n⏰ {time_range}  \n📍 {loc_display or '未指定地点'}")
    return "\n\n".join(lines)

# ---------- 天气查询（高德）----------
def get_weather(city: str, api_key: str, display_name: Optional[str] = None,
                target_date: Optional[datetime.date] = None) -> Optional[str]:
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

def get_weather_multi(cities_str: str, api_key: str,
                      target_date: Optional[datetime.date] = None) -> Optional[str]:
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

# ---------- 消息推送（根据是否在预定时间显示不同标识） ----------
def send_markdown(webhook_url: str, title: str, content: str, scheduled: bool = False) -> bool:
    footer = "🤖 钉钉日历机器人自动推送" if scheduled else "🤖 钉钉日历机器人手动推送"
    text = f"### {title}\n\n{content}\n\n---\n> {footer}\n> 📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}, "at": {"atMobiles": [], "isAtAll": False}}
    resp = requests.post(webhook_url, json=payload, timeout=10)
    return resp.json().get("errcode") == 0

# ---------- 查询时间范围（自动判断今日/明日） ----------
def get_query_range():
    utc_now = datetime.utcnow()
    beijing_now = utc_now + timedelta(hours=8)
    if beijing_now.hour >= 21:
        target_date = beijing_now.date() + timedelta(days=1)
        logger.info("当前北京时间 %02d:00，已过21点，查询明日日程", beijing_now.hour)
    else:
        target_date = beijing_now.date()
        logger.info("当前北京时间 %02d:00，未到21点，查询今日日程", beijing_now.hour)
    beijing_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    beijing_end = beijing_start + timedelta(days=1)
    start_utc = beijing_start - timedelta(hours=8)
    end_utc = beijing_end - timedelta(hours=8)
    start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("查询目标日期: %s, UTC 范围: %s ~ %s", target_date, start_str, end_str)
    return start_str, end_str, target_date

# ---------- 主函数 ----------
def main():
    logger.info("开始执行日历推送任务")
    scheduled = is_scheduled_run()
    logger.info("运行模式: %s", "自动（预定时间）" if scheduled else "手动（非预定时间）")

    client = DingTalkCalendarClient()
    start_utc, end_utc, target_date = get_query_range()

    union_id = client.get_user_unionid(USER_ID)
    events = client.get_events(union_id, CALENDAR_ID, start_utc, end_utc)
    logger.info("获取到 %d 条日程（含全天）", len(events))

    weather_str = None
    if WEATHER_CITIES:
        weather_str = get_weather_multi(WEATHER_CITIES, WEATHER_API_KEY, target_date)
    elif WEATHER_CITY:
        city_query, display_name = parse_city_entry(WEATHER_CITY)
        weather_str = get_weather(city_query, WEATHER_API_KEY, display_name, target_date)

    # 格式化日程（自动过滤全天）
    schedule_text = format_events(events)

    content_parts = []
    if weather_str:
        content_parts.append(weather_str)
        content_parts.append("")
    content_parts.append("**📅 日程安排**  \n" + schedule_text)
    content = "\n\n".join(content_parts)

    title = f"📅 日程提醒 - {target_date.strftime('%m月%d日')}"
    if not send_markdown(WEBHOOK_URL, title, content, scheduled):
        sys.exit(1)
    logger.info("推送成功")

if __name__ == "__main__":
    main()
