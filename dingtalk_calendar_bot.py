#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉 + 企业微信 订阅日历推送机器人 (GitHub Actions 版)
功能: 根据运行时间自动推送当天/次日日程 + 多城市天气（高德）
- 白天（<21点）：当日实时天气 + 当日定时日程（不含全天）
- 晚上（>=21点）：次日天气预报 + 次日定时日程（不含全天）
- 自动识别是否在预设时间（08:00 或 22:00）运行，消息尾部显示对应标识
- 同时推送钉钉机器人 与 企业微信机器人
- 钉钉使用 markdown，企业微信使用纯文本
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

# 企业微信机器人 Webhook（可选；未配置则跳过企业微信推送）
WECHAT_WORK_WEBHOOK_URL = (os.getenv("WECHAT_WORK_WEBHOOK_URL") or "").strip()

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

# ---------- 日程数据抽取（供两种格式复用） ----------
def extract_events(events: List[Dict]) -> List[Dict]:
    """过滤：只保留有 dateTime 的定时日程（排除全天事件），返回结构化列表。"""
    filtered = []
    for ev in events:
        start = ev.get("start", {})
        if isinstance(start, dict) and start.get("dateTime"):
            title = ev.get("summary") or ev.get("title") or "未命名日程"
            end = ev.get("end", {})
            def fmt(t):
                try:
                    return datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%H:%M")
                except:
                    return ""
            start_str = fmt(start.get("dateTime", ""))
            end_str = fmt(end.get("dateTime", "")) if isinstance(end, dict) else ""
            time_range = f"{start_str}-{end_str}" if start_str and end_str else "全天"
            loc = ev.get("location", {})
            loc_display = loc.get("displayName", "") if isinstance(loc, dict) else ""
            filtered.append({"title": title, "time_range": time_range, "location": loc_display})
        else:
            logger.debug("跳过全天日程: %s", ev.get("summary", "未命名"))
    return filtered

# ---------- 日程格式化：钉钉 markdown ----------
def format_events_markdown(items: List[Dict]) -> str:
    if not items:
        return "✅ 今日无定时日程安排，祝你顺利！🎉"
    lines = []
    for i, ev in enumerate(items, 1):
        lines.append(
            f"**{i}. {ev['title']}**  \n"
            f"⏰ {ev['time_range']}  \n"
            f"📍 {ev['location'] or '未指定地点'}"
        )
    return "\n\n".join(lines)

# ---------- 日程格式化：企业微信纯文本 ----------
def format_events_plain(items: List[Dict]) -> str:
    if not items:
        return "✅ 今日无定时日程安排，祝你顺利！🎉"
    lines = []
    for i, ev in enumerate(items, 1):
        lines.append(
            f"{i}. {ev['title']}\n"
            f"   ⏰ {ev['time_range']}\n"
            f"   📍 {ev['location'] or '未指定地点'}"
        )
    return "\n\n".join(lines)

# ---------- 天气查询（高德）----------
def get_weather(city: str, api_key: str, display_name: Optional[str] = None,
                target_date: Optional[datetime.date] = None) -> Optional[Dict]:
    """返回结构化天气 dict，由调用方决定渲染成 markdown 或纯文本。"""
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
            return {
                "city_name": display_name if display_name else live.get("city", city),
                "label": None,
                "temperature": live.get("temperature", "N/A"),
                "weather": live.get("weather", "未知"),
                "humidity": live.get("humidity", "N/A"),
                "wind_direction": live.get("winddirection", "未知"),
                "wind_power": live.get("windpower", "N/A"),
                "report_time": live.get("reporttime", ""),
                "night_temp": None,
                "date_str": None,
            }
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
            if target_date == today + timedelta(days=1):
                label = "明日"
            elif target_date == today + timedelta(days=2):
                label = "后天"
            else:
                label = target_str
            return {
                "city_name": display_name if display_name else forecast.get("city", city),
                "label": label,
                "temperature": cast.get("daytemp", "N/A"),
                "weather": cast.get("dayweather", "未知"),
                "humidity": None,
                "wind_direction": cast.get("daywind", "未知"),
                "wind_power": cast.get("daypower", "N/A"),
                "report_time": None,
                "night_temp": cast.get("nighttemp", "N/A"),
                "date_str": target_str,
            }
    except Exception as e:
        logger.error("获取 %s 天气失败: %s", city, e)
        return None

def get_weather_multi(cities_str: str, api_key: str,
                      target_date: Optional[datetime.date] = None) -> List[Dict]:
    if not api_key or not cities_str:
        return []
    entries = [e.strip() for e in cities_str.split(";") if e.strip()]
    result = []
    for entry in entries:
        city_query, display_name = parse_city_entry(entry)
        if not city_query:
            continue
        weather = get_weather(city_query, api_key, display_name, target_date)
        if weather:
            result.append(weather)
    return result

# ---------- 天气格式化：钉钉 markdown ----------
def format_weather_markdown(w: Dict) -> str:
    header = f"🌤 **{w['city_name']}天气**"
    if w.get("label"):
        header = f"🌤 **{w['city_name']}天气 ({w['label']})**"
    lines = [header]
    if w.get("night_temp"):
        lines.append(f"   🌡 温度：{w['temperature']}℃（夜间{w['night_temp']}℃）")
    else:
        lines.append(f"   🌡 温度：{w['temperature']}℃")
    lines.append(f"   ☁️ 天气：{w['weather']}")
    if w.get("humidity"):
        lines.append(f"   💧 湿度：{w['humidity']}%")
    lines.append(f"   🌬 风力：{w['wind_direction']}{w['wind_power']}级")
    if w.get("report_time"):
        lines.append(f"   🕒 更新：{w['report_time']}")
    if w.get("date_str"):
        lines.append(f"   📅 {w['date_str']}")
    return "  \n".join(lines)

# ---------- 天气格式化：企业微信纯文本 ----------
def format_weather_plain(w: Dict) -> str:
    header = f"🌤 {w['city_name']}天气"
    if w.get("label"):
        header = f"🌤 {w['city_name']}天气 ({w['label']})"
    lines = [header]
    if w.get("night_temp"):
        lines.append(f"   🌡 温度：{w['temperature']}℃（夜间{w['night_temp']}℃）")
    else:
        lines.append(f"   🌡 温度：{w['temperature']}℃")
    lines.append(f"   ☁️ 天气：{w['weather']}")
    if w.get("humidity"):
        lines.append(f"   💧 湿度：{w['humidity']}%")
    lines.append(f"   🌬 风力：{w['wind_direction']}{w['wind_power']}级")
    if w.get("report_time"):
        lines.append(f"   🕒 更新：{w['report_time']}")
    if w.get("date_str"):
        lines.append(f"   📅 {w['date_str']}")
    return "\n".join(lines)

# ---------- 消息推送：钉钉（markdown） ----------
def send_dingtalk(webhook_url: str, title: str, text: str) -> bool:
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
        "at": {"atMobiles": [], "isAtAll": False},
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            logger.info("钉钉推送成功")
            return True
        logger.error("钉钉推送失败: %s", data)
        return False
    except Exception as e:
        logger.error("钉钉推送异常: %s", e)
        return False

# ---------- 消息推送：企业微信（纯文本） ----------
def send_wechat_work(webhook_url: str, text: str) -> bool:
    if not webhook_url:
        logger.info("未配置 WECHAT_WORK_WEBHOOK_URL，跳过企业微信推送")
        return True
    # 企业微信 text 消息内容上限 2048 字节，超出则安全截断
    encoded = text.encode("utf-8")
    if len(encoded) > 2048:
        text = encoded[:2000].decode("utf-8", errors="ignore")
        logger.warning("企业微信消息过长，已截断")
    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            logger.info("企业微信推送成功")
            return True
        logger.error("企业微信推送失败: %s", data)
        return False
    except Exception as e:
        logger.error("企业微信推送异常: %s", e)
        return False

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

    # 天气（结构化）
    weather_list: List[Dict] = []
    if WEATHER_CITIES:
        weather_list = get_weather_multi(WEATHER_CITIES, WEATHER_API_KEY, target_date)
    elif WEATHER_CITY:
        city_query, display_name = parse_city_entry(WEATHER_CITY)
        w = get_weather(city_query, WEATHER_API_KEY, display_name, target_date)
        if w:
            weather_list = [w]

    # 日程（结构化，自动过滤全天）
    event_items = extract_events(events)

    title = f"📅 日程提醒 - {target_date.strftime('%m月%d日')}"
    footer = "🤖 日历机器人自动推送" if scheduled else "🤖 日历机器人手动推送"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---------- 钉钉 markdown 版本 ----------
    ding_parts = []
    if weather_list:
        ding_parts.append("\n\n".join(format_weather_markdown(w) for w in weather_list))
        ding_parts.append("")
    ding_parts.append("**📅 日程安排**  \n" + format_events_markdown(event_items))
    ding_content = "\n\n".join(ding_parts)
    ding_text = (
        f"### {title}\n\n"
        f"{ding_content}\n\n"
        f"---\n"
        f"> {footer}\n"
        f"> 📅 {now_str}"
    )

    # ---------- 企业微信纯文本版本 ----------
    wechat_parts = []
    if weather_list:
        wechat_parts.append("\n\n".join(format_weather_plain(w) for w in weather_list))
        wechat_parts.append("")
    wechat_parts.append("📅 日程安排\n" + format_events_plain(event_items))
    wechat_content = "\n\n".join(wechat_parts)
    wechat_text = (
        f"{title}\n"
        f"{'=' * 30}\n\n"
        f"{wechat_content}\n\n"
        f"{'-' * 30}\n"
        f"{footer}\n"
        f"📅 {now_str}"
    )

    # ---------- 同时推送 ----------
    ding_ok = send_dingtalk(WEBHOOK_URL, title, ding_text)
    wechat_ok = send_wechat_work(WECHAT_WORK_WEBHOOK_URL, wechat_text)

    if not ding_ok:
        logger.error("钉钉推送失败")
        sys.exit(1)
    if WECHAT_WORK_WEBHOOK_URL and not wechat_ok:
        logger.error("企业微信推送失败")
        sys.exit(1)
    logger.info("全部推送成功")

if __name__ == "__main__":
    main()
