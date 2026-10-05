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


def diag():
    """Проверка доступа к сайту из облака (вызывается вручную событием {"diag": true})."""
    import http.cookiejar
    out = []
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    h = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36",
         "Referer": "https://lk.gubkin.ru/schedule/", "Accept": "application/json, text/plain, */*"}
    d = bot.now().date()
    h.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
              "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7", "Sec-Fetch-Site": "same-origin",
              "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty", "Connection": "keep-alive",
              "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
              "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'})
    import re
    page = op.open(urllib.request.Request("https://lk.gubkin.ru/schedule/", headers=h), timeout=10).read().decode()
    src = re.findall(r'src="(main[^"]+)"', page)[0]
    js = op.open(urllib.request.Request("https://lk.gubkin.ru/schedule/" + src, headers=h), timeout=20).read().decode("utf-8", "replace")

    steps = [("cis-meta", "https://cis.gubkin.ru/lk_schedule/api/api.php?act=meta"),
             ("cis-schedule", f"https://cis.gubkin.ru/lk_schedule/api/api.php?act=schedule&date={d.day}-{d.month}-{d.year}&groupId=9685"),
                          ("lk-api", "https://lk.gubkin.ru/api/api.php?module=auth&method=check")]
    for name, url in steps:
        t0 = time.time()
        try:
            with op.open(urllib.request.Request(url, headers=h), timeout=30) as r:
                code, body = r.status, r.read(250)
                if name == 'captcha':
                    body = (r.headers.get('Content-Type', '') + ' ' + str(len(body))).encode()
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read(250)
        except Exception as ex:
            code, body = type(ex).__name__, str(ex).encode()
        out.append(f"{name} {code} {time.time() - t0:.1f}s cookies={[c.name for c in jar]} body={body.decode('utf-8', 'replace')!r}")
    return out


def handler(event, context):
    global _token
    if isinstance(event, dict) and event.get("diag"):
        return {"statusCode": 200, "body": json.dumps(diag(), ensure_ascii=False)}
    _token = context.token["access_token"]
    if _is_timer(event):
        tick()
        return {"statusCode": 200, "body": "tick"}
    return webhook(event)
