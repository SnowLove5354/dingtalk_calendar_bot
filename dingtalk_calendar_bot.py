#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉订阅日历推送 + 多城市天气（高德）GitHub Actions 版
功能：拉取钉钉订阅日历今日/明日日程，获取多个城市天气，通过机器人推送 Markdown 消息。
运行环境：GitHub Actions（或本地设置环境变量）
"""

import os
import sys
import time
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# ---------- 日志配置 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------- 从环境变量读取配置（GitHub Secrets） ----------
def get_env(key: str, required: bool = True) -> str:
    value = os.getenv(key, "")
    if required and not value:
        logger.error("缺少必要环境变量：%s", key)
        sys.exit(1)
    return value

DINGTALK_APP_KEY = get_env("DINGTALK_APP_KEY")
DINGTALK_APP_SECRET = get_env("DINGTALK_APP_SECRET")
USER_ID = get_env("DINGTALK_USER_ID")
CALENDAR_ID = get_env("DINGTALK_CALENDAR_ID")
WEBHOOK_URL = get_env("DINGTALK_WEBHOOK_URL")
WEATHER_API_KEY = get_env("WEATHER_API_KEY", required=False)  # 可选
WEATHER_CITIES = get_env("WEATHER_CITIES", required=False)
WEATHER_CITY = get_env("WEATHER_CITY", required=False)

# ---------- 工具函数：解析城市条目 ----------
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

# ---------- 获取 unionId ----------
def get_unionid(user_id: str) -> str:
    url = "https://oapi.dingtalk.com/topapi/v2/user/get"
    params = {"access_token": TokenManager.get_token()}
    payload = {"userid": user_id}
    resp = requests.post(url, params=params, json=payload, timeout=10)
    data = resp.json()
    if data.get("errcode") != 0:
        raise Exception(f"获取 unionId 失败: {data.get('errmsg')}")
    return data["result"]["unionid"]

# ---------- 查询日程 ----------
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

# ---------- 格式化日程 ----------
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

        # 1. 定时日程
        if start_dt and end_dt:
            time_range = f"{fmt_datetime(start_dt)}-{fmt_datetime(end_dt)}"
        # 2. 全天日程（含跨日）
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

# ---------- 查询单个城市天气（高德版） ----------
def get_weather(city: str, api_key: str, display_name: Optional[str] = None) -> Optional[str]:
    if not api_key or not city:
        return None
    try:
        url = "https://restapi.amap.com/v3/weather/weatherInfo"
        params = {"key": api_key, "city": city, "extensions": "base"}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "1" or not data.get("lives"):
            logger.error("高德天气返回错误: %s", data)
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
    except Exception as e:
        logger.error("获取 %s 天气失败: %s", city, e)
        return None

# ---------- 查询多个城市天气 ----------
def get_weather_multi(cities_str: str, api_key: str) -> Optional[str]:
    if not api_key:
        logger.warning("未配置 WEATHER_API_KEY，跳过天气查询")
        return None
    if not cities_str:
        return None
    entries = [e.strip() for e in cities_str.split(";") if e.strip()]
    if not entries:
        return None
    weather_parts = []
    for entry in entries:
        city_query, display_name = parse_city_entry(entry)
        if not city_query:
            continue
        weather = get_weather(city_query, api_key, display_name)
        if weather:
            weather_parts.append(weather)
    if not weather_parts:
        return None
    return "\n\n".join(weather_parts)

# ---------- 推送消息 ----------
def send_markdown(webhook: str, title: str, content: str) -> bool:
    text = (
        f"### {title}\n\n"
        f"{content}\n\n---\n"
        f"> 🤖 钉钉日历机器人自动推送\n"
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

# ---------- 时间范围逻辑 ----------
def get_query_range():
    """根据北京时间自动判断查询今日或明日"""
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

# ---------- 主流程 ----------
def main():
    logger.info("开始执行钉钉日历推送任务")
    try:
        # 1. 获取日程
        start_utc, end_utc, target_date = get_query_range()
        union_id = get_unionid(USER_ID)
        events = get_events(union_id, CALENDAR_ID, start_utc, end_utc)

        # 2. 获取天气
        if WEATHER_CITIES:
            weather_str = get_weather_multi(WEATHER_CITIES, WEATHER_API_KEY)
        elif WEATHER_CITY:
            city_query, display_name = parse_city_entry(WEATHER_CITY)
            weather_str = get_weather(city_query, WEATHER_API_KEY, display_name)
        else:
            weather_str = None

        # 3. 组装消息
        content_parts = []
        if weather_str:
            content_parts.append(weather_str)
            content_parts.append("")
        content_parts.append("**📅 日程安排**  \n" + format_events(events))
        content = "\n".join(content_parts)

        # 4. 推送
        title = f"📅 每日提醒 - {target_date.strftime('%m月%d日')}"
        if not send_markdown(WEBHOOK_URL, title, content):
            sys.exit(1)
        logger.info("推送完成")
    except Exception as e:
        logger.error("任务失败: %s", e, exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
