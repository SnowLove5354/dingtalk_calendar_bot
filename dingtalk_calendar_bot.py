#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉订阅日历推送机器人 (GitHub Actions 版)
功能: 根据运行时间自动推送当天/次日日程
"""

import os
import sys
import time
import json
import logging
import requests
from datetime import datetime, timedelta
from typing import List, Dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

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

def send_markdown(webhook_url: str, title: str, content: str) -> bool:
    text = f"### {title}\n\n{content}\n\n---\n> 🤖 钉钉日历机器人自动推送\n> 📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}, "at": {"atMobiles": [], "isAtAll": False}}
    resp = requests.post(webhook_url, json=payload, timeout=10)
    return resp.json().get("errcode") == 0

def format_events(events: List[Dict]) -> str:
    if not events:
        return "✅ 暂无日程安排，祝你顺利！🎉"
    lines = []
    for i, ev in enumerate(events, 1):
        title = ev.get("summary") or ev.get("title") or "未命名日程"
        start = ev.get("start", {})
        end = ev.get("end", {})
        def fmt(t):
            try: return datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%H:%M")
            except: return ""
        start_str = fmt(start.get("dateTime", start.get("date", ""))) if isinstance(start, dict) else ""
        end_str = fmt(end.get("dateTime", end.get("date", ""))) if isinstance(end, dict) else ""
        time_range = f"{start_str}-{end_str}" if start_str and end_str else "全天"
        loc = ev.get("location", {})
        loc_display = loc.get("displayName", "") if isinstance(loc, dict) else ""
        lines.append(f"**{i}. {title}**  \n⏰ {time_range}  \n📍 {loc_display or '未指定地点'}")
    return "\n\n".join(lines)

def get_query_range():
    """根据北京时间自动判断查询今日或明日，返回 UTC 时间范围字符串"""
    utc_now = datetime.utcnow()
    beijing_now = utc_now + timedelta(hours=8)
    # 如果北京时间 >= 21 点，则查询明天；否则查询今天
    if beijing_now.hour >= 21:
        target_date = beijing_now.date() + timedelta(days=1)
    else:
        target_date = beijing_now.date()
    # 北京时间 target_date 00:00:00 对应 UTC 时间 (target_date - 1) 16:00:00
    beijing_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    beijing_end = beijing_start + timedelta(days=1)
    start_utc = beijing_start - timedelta(hours=8)
    end_utc = beijing_end - timedelta(hours=8)
    start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("查询目标日期: %s, UTC 范围: %s ~ %s", target_date, start_str, end_str)
    return start_str, end_str, target_date

def main():
    logger.info("开始执行日历推送任务")
    client = DingTalkCalendarClient()
    start_utc, end_utc, target_date = get_query_range()
    union_id = client.get_user_unionid(USER_ID)
    events = client.get_events(union_id, CALENDAR_ID, start_utc, end_utc)
    content = format_events(events)
    title = f"📅 日程提醒 - {target_date.strftime('%m月%d日')}"
    if not send_markdown(WEBHOOK_URL, title, content):
        sys.exit(1)

if __name__ == "__main__":
    main()
