"""
Точка входа для Yandex Cloud Functions (index.handler).

Одна функция обслуживает два события:
  * HTTP-запрос от Telegram (webhook) — команды /today, /week и т.д.;
  * таймер раз в 5 минут — утренняя сводка, напоминания и проверка замен.
Состояние бота (подписчики, отправленные напоминания, последнее расписание)
хранится в Object Storage в файле state.json.
"""

import base64
import urllib.parse
import json
import os
import time
import urllib.error
import urllib.request

import gubkin_bot as bot

BUCKET = os.environ.get("STATE_BUCKET", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
STATE_KEY = "state.json"
PAUSED = True   # сайт расписания не пускает облачные IP — временно не ходим на него

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


def lk_session():
    """Вход в личный кабинет lk.gubkin.ru. Возвращает opener с cookie сессии."""
    import http.cookiejar
    pad = os.environ.get("LK_AUTH", "")
    login, password = json.loads(base64.urlsafe_b64decode(pad + "=" * (-len(pad) % 4)).decode()) if pad else ("", "")
    if not login or not password:
        raise RuntimeError("не заданы LK_LOGIN / LK_PASSWORD")
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
                     ("Accept", "application/json, text/plain, */*")]
    body = json.dumps({"login": int(login) if login.isdigit() else login,
                       "password": password, "rememberMe": 1}).encode()
    req = urllib.request.Request("https://lk.gubkin.ru/api/api.php?module=auth&method=login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = json.loads(op.open(req, timeout=20).read().decode())
    except urllib.error.HTTPError as e:
        resp = json.loads(e.read().decode() or "{}")
    if resp.get("success") is not True:
        raise RuntimeError(f"вход в ЛК не удался: {resp.get('reason')}")
    return op


def diag():
    """Ищет в личном кабинете метод API с расписанием (вызывается событием {"diag": true})."""
    import re
    out = []
    try:
        op = lk_session()
    except Exception as ex:
        return [f"login: {ex}"]
    out.append("login ok")
    page = op.open("https://lk.gubkin.ru/", timeout=15).read().decode("utf-8", "replace")
    srcs = re.findall(r'src=["\']?([^"\' >]+\.js)', page)
    out.append(f"srcs {srcs[:6]}")
    def api(params):
        url = "https://lk.gubkin.ru/api/api.php?" + urllib.parse.urlencode(params)
        try:
            return op.open(url, timeout=25).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return f"HTTP {e.code} " + e.read().decode("utf-8", "replace")
    out.append("getUrl-noid " + api({"module": "study", "resource": "Schedule", "method": "getUrl", "type": "activities"})[:300])
    js = op.open("https://lk.gubkin.ru/" + [x for x in srcs if x.startswith("main-es2015")][0], timeout=25).read().decode("utf-8", "replace")
    calls = set(re.findall(r'module:"(\w+)",resource:"(\w+)",method:"(\w+)"', js))
    out.append(f"calls {len(calls)}")
    line = ""
    for mod, res_, meth in sorted(calls):
        item = f"{mod}.{res_}.{meth} "
        if len(line) + len(item) > 900:
            out.append("calls " + line)
            line = ""
        line += item
    out.append("calls " + line)
    return out


def handler(event, context):
    global _token
    if isinstance(event, dict) and event.get("diag"):
        return {"statusCode": 200, "body": json.dumps(diag(), ensure_ascii=False)}
    _token = context.token["access_token"]
    if PAUSED:
        if not _is_timer(event):
            try:
                msg = json.loads(event.get("body") or "{}").get("message") or {}
                if msg.get("chat"):
                    bot.send(msg["chat"]["id"], "🛠 Бот настраивается — скоро заработает.")
            except Exception:
                pass
        return {"statusCode": 200, "body": "paused"}
    if _is_timer(event):
        tick()
        return {"statusCode": 200, "body": "tick"}
    return webhook(event)
