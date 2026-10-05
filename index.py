"""
Точка входа для Yandex Cloud Functions (index.handler).

Одна функция обслуживает два события:
  * HTTP-запрос от Telegram (webhook) — команды /today, /week и т.д.;
  * таймер раз в 5 минут — утренняя сводка, напоминания и проверка замен.
Состояние бота (подписчики, отправленные напоминания, последнее расписание)
хранится в Object Storage в файле state.json.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request

import gubkin_bot as bot

BUCKET = os.environ.get("STATE_BUCKET", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
STATE_KEY = "state.json"

_token = None      # IAM-токен сервисного аккаунта функции
_saved = None      # что последний раз записали, чтобы не писать лишний раз


def _storage(method, body=None):
    req = urllib.request.Request(
        f"https://storage.yandexcloud.net/{BUCKET}/{STATE_KEY}", data=body, method=method,
        headers={"Authorization": f"Bearer {_token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def load_state():
    global _saved
    try:
        raw = _storage("GET").decode()
        s = json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        raw, s = None, {}
    _saved = raw
    for k, v in (("chats", []), ("sent", []), ("snapshot", {}), ("offset", 0), ("last_check", 0)):
        s.setdefault(k, v)
    return s


def save_state(s):
    global _saved
    s["sent"] = s["sent"][-500:]
    raw = json.dumps(s, ensure_ascii=False)
    if raw != _saved:
        _storage("PUT", raw.encode())
        _saved = raw


# gubkin_bot вызывает load_state/save_state по имени — подменяем на облачные
bot.load_state = load_state
bot.save_state = save_state


def _is_timer(event):
    msgs = event.get("messages") if isinstance(event, dict) else None
    return bool(msgs) and "Timer" in str(msgs[0].get("event_metadata", {}).get("event_type", ""))


def tick():
    state = load_state()
    if time.time() - state.get("last_check", 0) > bot.REFRESH_MIN * 60:
        state["last_check"] = time.time()
        try:
            bot.check_changes(state)      # сам сохраняет состояние
        except Exception as ex:
            print(f"Проверка сайта не удалась: {ex}")
            save_state(state)
    try:
        bot.timed_messages(state)
    except Exception as ex:
        print(f"Напоминания: {ex}")


def webhook(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if WEBHOOK_SECRET and headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        return {"statusCode": 403, "body": "forbidden"}
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    update = json.loads(body)
    if "message" in update:
        bot.handle(load_state(), update["message"])
    return {"statusCode": 200, "body": ""}


def handler(event, context):
    global _token
    _token = context.token["access_token"]
    if _is_timer(event):
        tick()
        return {"statusCode": 200, "body": "tick"}
    return webhook(event)
