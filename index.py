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


def diag():
    """Проверка доступа к сайту из облака (вызывается вручную событием {"diag": true})."""
    import http.cookiejar, re
    out = []
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"}
    page = op.open(urllib.request.Request("https://lk.gubkin.ru/", headers=h), timeout=15).read().decode("utf-8", "replace")
    srcs = re.findall(r'src=["\']?([^"\' >]+\.js)', page)
    out.append(f"srcs {srcs}")
    found = {}
    for src in srcs:
        url = src if src.startswith("http") else "https://lk.gubkin.ru/" + src.lstrip("/")
        try:
            js = op.open(urllib.request.Request(url, headers=h), timeout=20).read().decode("utf-8", "replace")
        except Exception as ex:
            out.append(f"js {src} {ex}")
            continue
        for m in re.finditer(r"module=([A-Za-z_]+)&method=([A-Za-z_]+)", js):
            found.setdefault(m.group(1), set()).add(m.group(2))
        for m in re.finditer(r"(timetable|schedule|raspis)", js, re.I):
            ctx = js[max(0, m.start() - 100): m.end() + 100]
            if "method" in ctx or "module" in ctx:
                found.setdefault("_ctx", set()).add(" ".join(ctx.split())[:200])
    js = op.open(urllib.request.Request("https://lk.gubkin.ru/login/js/" + [x for x in srcs if "app." in x][0].split("/")[-1], headers=h), timeout=20).read().decode("utf-8", "replace")
    for m in list(re.finditer(r"location|redirect|href", js))[:40]:
        c = " ".join(js[max(0, m.start() - 60): m.end() + 90].split())
        if "/" in c and ("login" in c or "success" in c or "href" in c):
            found.setdefault("_loc", []).append(c[:170])
    for c in found.get("_loc", [])[:6]:
        out.append("loc " + c)
    for k in sorted(found):
        if k == "_loc":
            continue
        if k == "_ctx":
            continue
        out.append(f"{k}: {sorted(found[k])}")
    for c in list(found.get("_ctx", []))[:3]:
        out.append("ctx " + c)
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
