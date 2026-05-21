#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉订阅日历推送机器人 (GitHub Actions 版)
功能: 根据运行时间自动推送当天/次日日程
当北京时间 >= 21 点（如晚上22点）时，推送明日日程；
当北京时间 < 21 点（如早上8点）时，推送今日日程。
"""

import os
import sys
import time
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List

# -------------------- 日志配置 --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# -------------------- 环境变量读取 --------------------
def get_env_or_fail(key: str) -> str:
    value = os.getenv(key)
    if not value:
        logger.error("环境变量 %s 未设置，程序退出。", key)
        sys.exit(1)
    return value

DINGTALK_APP_KEY = get_env_or_fail("DINGTALK_APP_KEY")
DINGTALK_APP_SECRET = get_env_or_fail("DINGTALK_APP_SECRET")
USER_ID = get_env_or_fail("DINGTALK_USER_ID")
CALENDAR_ID = get_env_or_fail("DINGTALK_CALENDAR_ID")
WEBHOOK_URL = get_env_or_fail("DINGTALK_WEBHOOK_URL")

# -------------------- 钉钉 Token 管理 --------------------
class DingTalkTokenManager:
    _token_info: Dict = {}

    @classmethod
    def get_access_token(cls) -> str:
        now = time.time()
        if cls._token_info.get("token") and cls._token_info.get("expires_at", 0) > now + 60:
            return cls._token_info["token"]

        logger.info("正在获取新版 access_token...")
        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        payload = {"appKey": DINGTALK_APP_KEY, "appSecret": DINGTALK_APP_SECRET}
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "accessToken" not in data:
            raise ValueError(f"响应中无 accessToken: {data}")
        token = data["accessToken"]
        expires_in = data.get("expireIn", 7200)
        cls._token_info = {
            "token": token,
            "expires_at": now + expires_in - 300,  # 提前 5 分钟刷新
        }
        logger.info("access_token 获取成功，有效期 %d 秒", expires_in)
        return token

# -------------------- 钉钉日历客户端 --------------------
class DingTalkCalendarClient:
    BASE_URL = "https://api.dingtalk.com/v1.0"

    @staticmethod
    def _get_headers() -> Dict:
        return {
            "x-acs-dingtalk-access-token": DingTalkTokenManager.get_access_token(),
            "Content-Type": "application/json",
        }

    @classmethod
    def get_user_unionid(cls, user_id: str) -> str:
        url = "https://oapi.dingtalk.com/topapi/v2/user/get"
        params = {"access_token": DingTalkTokenManager.get_access_token()}
        payload = {"userid": user_id}
        resp = requests.post(url, params=params, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") != 0:
            raise Exception(f"获取 unionId 失败: {data.get('errmsg')}")
        return data["result"]["unionid"]

    @classmethod
    def get_events(cls, union_id: str, calendar_id: str, time_min: str, time_max: str) -> List[Dict]:
        params = {"timeMin": time_min, "timeMax": time_max}
        url = f"{cls.BASE_URL}/calendar/users/{union_id}/calendars/{calendar_id}/events"
        logger.info("查询日程: %s, params=%s", url, params)
        resp = requests.get(url, headers=cls._get_headers(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events") or data.get("result", {}).get("events", [])
        logger.info("获取到 %d 条日程", len(events))
        return events

# -------------------- Webhook 推送 --------------------
def send_markdown(webhook_url: str, title: str, content: str) -> bool:
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
    resp = requests.post(webhook_url, json=payload, timeout=10)
    result = resp.json()
    if result.get("errcode") == 0:
        logger.info("消息推送成功")
        return True
    else:
        logger.error("消息推送失败: %s", result.get("errmsg"))
        return False

# -------------------- 日程格式化 --------------------
def format_events(events: List[Dict]) -> str:
    if not events:
        return "✅ 暂无日程安排，祝你顺利！🎉"
    lines = []
    for i, ev in enumerate(events, 1):
        title = ev.get("summary") or ev.get("title") or "未命名日程"
        start_obj = ev.get("start", {})
        end_obj = ev.get("end", {})

        def fmt_time(t):
            try:
                return datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%H:%M")
            except:
                return ""

        if isinstance(start_obj, dict):
            start = start_obj.get("dateTime") or start_obj.get("date", "")
        else:
            start = str(start_obj) if start_obj else ""
        if isinstance(end_obj, dict):
            end = end_obj.get("dateTime") or end_obj.get("date", "")
        else:
            end = str(end_obj) if end_obj else ""

        time_range = f"{fmt_time(start)}-{fmt_time(end)}" if start and end else "全天"
        location = ev.get("location", {})
        loc_display = location.get("displayName", "") if isinstance(location, dict) else ""
        lines.append(f"**{i}. {title}**  \n   ⏰ {time_range}  \n   📍 {loc_display or '未指定地点'}")
    return "\n\n".join(lines)

# -------------------- 查询日期逻辑 --------------------
def get_query_range():
    """
    根据当前北京时间决定查询今天还是明天，返回 UTC 时间范围字符串和目标日期。
    规则：北京时间 >= 21 点 -> 查询明天；否则查询今天。
    """
    utc_now = datetime.utcnow()
    beijing_now = utc_now + timedelta(hours=8)

    if beijing_now.hour >= 21:
        target_date = beijing_now.date() + timedelta(days=1)
    else:
        target_date = beijing_now.date()

    # 将北京日期的 00:00:00 转为 UTC 时间
    beijing_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    beijing_end = beijing_start + timedelta(days=1)
    start_utc = beijing_start - timedelta(hours=8)
    end_utc = beijing_end - timedelta(hours=8)

    start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("目标日期: %s, UTC 范围: %s ~ %s", target_date, start_str, end_str)
    return start_str, end_str, target_date

# -------------------- 主流程 --------------------
def main():
    logger.info("开始执行日历推送任务")
    try:
        # 1. 确定查询时间范围
        start_utc, end_utc, target_date = get_query_range()

        # 2. 获取 unionId
        union_id = DingTalkCalendarClient.get_user_unionid(USER_ID)

        # 3. 查询日程
        events = DingTalkCalendarClient.get_events(union_id, CALENDAR_ID, start_utc, end_utc)

        # 4. 格式化并推送
        content = format_events(events)
        title = f"📅 日程提醒 - {target_date.strftime('%m月%d日')}"
        if not send_markdown(WEBHOOK_URL, title, content):
            sys.exit(1)
        logger.info("推送完成")
    except Exception as e:
        logger.error("任务执行失败: %s", e, exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
