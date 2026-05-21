#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉订阅日历推送机器人 (GitHub Actions 版)
功能: 读取钉钉订阅日历今日事件，通过群机器人推送 Markdown 消息
适用于 GitHub Actions 定时调度，单次执行模式。
"""

import os
import sys
import time
import json
import logging
import requests
from datetime import datetime, timedelta
from typing import Optional, List, Dict

# ====================== 日志配置 ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ====================== 配置获取 (纯环境变量) ======================
def get_env_or_fail(key: str) -> str:
    """获取环境变量，若不存在则退出"""
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

# ====================== 钉钉 Token 管理 ======================
class DingTalkTokenManager:
    """管理钉钉新版 access_token，自动刷新"""

    _token_info: Dict = {}

    @classmethod
    def get_access_token(cls) -> str:
        now = time.time()
        cache = cls._token_info
        if cache.get("token") and cache.get("expires_at", 0) > now + 60:
            logger.debug("使用缓存 access_token")
            return cache["token"]

        logger.info("正在获取新版 access_token...")
        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        payload = {"appKey": DINGTALK_APP_KEY, "appSecret": DINGTALK_APP_SECRET}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if "accessToken" not in data:
                raise ValueError(f"响应无 accessToken: {data}")
            token = data["accessToken"]
            expires_in = data.get("expireIn", 7200)
            cls._token_info = {
                "token": token,
                "expires_at": now + expires_in - 300  # 提前 5 分钟刷新
            }
            logger.info("access_token 获取成功，有效期 %d 秒", expires_in)
            return token
        except Exception as e:
            logger.error("获取 access_token 失败: %s", e)
            raise

# ====================== 日历客户端 ======================
class DingTalkCalendarClient:
    BASE_URL = "https://api.dingtalk.com/v1.0"

    def _get_headers(self) -> Dict:
        return {
            "x-acs-dingtalk-access-token": DingTalkTokenManager.get_access_token(),
            "Content-Type": "application/json"
        }

    def _get_user_unionid(self, user_id: str) -> str:
        url = "https://oapi.dingtalk.com/topapi/v2/user/get"
        params = {"access_token": DingTalkTokenManager.get_access_token()}
        payload = {"userid": user_id}
        try:
            resp = requests.post(url, params=params, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") != 0:
                raise Exception(f"获取 unionId 失败: {data.get('errmsg')}")
            union_id = data["result"]["unionid"]
            logger.info("unionId 获取成功: %s", union_id)
            return union_id
        except Exception as e:
            logger.error("获取 unionId 失败: %s", e)
            raise

    def get_events(self, union_id: str, calendar_id: str,
                   time_min: str, time_max: str) -> List[Dict]:
        params = {"timeMin": time_min, "timeMax": time_max}
        url = f"{self.BASE_URL}/calendar/users/{union_id}/calendars/{calendar_id}/events"
        logger.info("查询日程: %s", url)
        try:
            resp = requests.get(url, headers=self._get_headers(), params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            events = data.get("events") or data.get("result", {}).get("events", [])
            logger.info("获取到 %d 条日程", len(events))
            return events
        except Exception as e:
            logger.error("查询日程失败: %s", e)
            raise

    def get_today_events(self) -> List[Dict]:
        union_id = self._get_user_unionid(USER_ID)
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"
        end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"
        return self.get_events(union_id, CALENDAR_ID, start, end)

# ====================== Webhook 推送 ======================
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
        "at": {"atMobiles": [], "isAtAll": False}
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info("消息推送成功")
            return True
        else:
            logger.error("消息推送失败: %s", result.get("errmsg"))
            return False
    except Exception as e:
        logger.error("推送异常: %s", e)
        return False

# ====================== 消息格式化 ======================
def format_events(events: List[Dict]) -> str:
    if not events:
        return "✅ 今日暂无日程安排，祝你工作顺利！🎉"

    lines = []
    for i, ev in enumerate(events, 1):
        title = ev.get("summary") or ev.get("title") or "未命名日程"
        start = ev.get("start", {})
        if isinstance(start, dict):
            start = start.get("dateTime") or start.get("date", "")
        end = ev.get("end", {})
        if isinstance(end, dict):
            end = end.get("dateTime") or end.get("date", "")

        def fmt(t):
            try:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                return dt.strftime("%H:%M")
            except:
                return ""

        time_range = f"{fmt(start)} - {fmt(end)}" if start and end else "全天"
        location = ev.get("location", {})
        loc_display = location.get("displayName", "") if isinstance(location, dict) else ""
        lines.append(
            f"**{i}. {title}**  \n"
            f"   ⏰ {time_range}  \n"
            f"   📍 {loc_display or '未指定地点'}"
        )
    return "\n\n".join(lines)

# ====================== 主逻辑 ======================
def main():
    logger.info("开始执行日历推送任务")
    try:
        client = DingTalkCalendarClient()
        events = client.get_today_events()
        content = format_events(events)
        title = f"📅 每日日程提醒 - {datetime.now().strftime('%m月%d日')}"
        ok = send_markdown(WEBHOOK_URL, title, content)
        if not ok:
            sys.exit(1)
    except Exception as e:
        logger.error("任务执行失败: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()