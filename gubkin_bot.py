#!/usr/bin/env python3
"""
Telegram-бот расписания группы МБ-24-08 (Губкинский университет).

Что умеет:
  * /today, /tomorrow, /week, /next — пары: время, предмет, аудитория, преподаватель;
  * каждое утро в 07:30 (МСК) — сводка пар на день;
  * за 15 минут до пары — напоминание, что, где и кто ведёт;
  * если на сайте поменяли аудиторию или преподавателя, отменили или перенесли пару,
    бот сразу присылает уведомление (сайт проверяется каждые 30 минут).

Запуск:
  1. Создайте бота у @BotFather в Telegram и скопируйте токен.
  2. Положите этот файл рядом с gubkin_schedule.py.
  3. Windows:  set BOT_TOKEN=123:ABC  &&  python gubkin_bot.py
     Linux/Mac: BOT_TOKEN=123:ABC python3 gubkin_bot.py
     (или впишите токен в переменную TOKEN ниже)
  4. Напишите боту /start.
Бот работает, пока запущен скрипт — держите его на компьютере, который не выключается,
или на сервере. Сторонние библиотеки не нужны, только Python 3.8+.
"""

import datetime as dt
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from gubkin_schedule import GROUP_NAME, DAYS, fetch_week, parse

TOKEN = os.environ.get("BOT_TOKEN", "")          # или впишите сюда: "123456:ABC..."
MORNING = dt.time(7, 30)                         # время утренней сводки
REMIND_BEFORE_MIN = 15                           # за сколько минут напоминать
REFRESH_MIN = 30                                 # как часто проверять сайт
STATE_FILE = "bot_state.json"

MSK = dt.timezone(dt.timedelta(hours=3))         # в Москве нет перехода на летнее время
API = f"https://api.telegram.org/bot{TOKEN}/"


def now():
    return dt.datetime.now(MSK).replace(tzinfo=None)


# ---------- Telegram ----------

def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(API + method, data=data, timeout=60) as r:
        return json.loads(r.read().decode())


def send(chat_id, text):
    try:
        tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
           disable_web_page_preview="true")
    except Exception as ex:
        print(f"Не отправилось в {chat_id}: {ex}")


def broadcast(state, text):
    for cid in state["chats"]:
        send(cid, text)


# ---------- состояние ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("chats", [])
    s.setdefault("sent", [])          # ключи уже отправленных напоминаний/сводок
    s.setdefault("snapshot", {})      # последнее известное расписание для поиска замен
    s.setdefault("offset", 0)
    return s


def save_state(s):
    s["sent"] = s["sent"][-500:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---------- расписание ----------

_cache = {}   # понедельник -> (время загрузки, {дата: [пары]})


def _to_date(s):
    for fmt in ("%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            pass
    return None


def week_lessons(day, force=False):
    """{дата: [пары]} для недели, в которую входит day. Кэш на REFRESH_MIN минут."""
    monday = day - dt.timedelta(days=day.weekday())
    cached = _cache.get(monday)
    if cached and not force and time.time() - cached[0] < REFRESH_MIN * 60:
        return cached[1]
    dates, _week_type, lessons = parse(fetch_week(monday))
    by_date = {}
    for l in lessons:
        d = _to_date(dates.get(l["day"])) or monday + dt.timedelta(days=(l["day"] or 1) - 1)
        by_date.setdefault(d, []).append(l)
    _cache[monday] = (time.time(), by_date)
    return by_date


def day_lessons(day):
    return week_lessons(day).get(day, [])


# ---------- оформление ----------

def fmt_lesson(l):
    e = html.escape
    status = ""
    if l["cancelled"]:
        status = "\n   ❌ <b>Отменена</b>"
    elif l["moved_to"]:
        status = f"\n   ↪️ <b>Перенесена на {e(l['moved_to'])}</b>"
    sub = f" · подгр. {l['subgroup']}" if l["subgroup"] else ""
    kind = f" <i>({e(l['type'])})</i>" if l["type"] else ""
    room = e(l["room"] or "—") + (" ⚠️ замена" if l["room_changed"] else "")
    teacher = e(l["teacher"] or "—") + (" ⚠️ замена" if l["teacher_changed"] else "")
    return (f"🕘 <b>{l['start']}–{l['end']}</b>  {e(l['subject'])}{kind}{sub}\n"
            f"   🚪 {room}\n   👤 {teacher}{status}")


def fmt_day(day, lessons):
    head = f"📅 <b>{DAYS[day.isoweekday()]}, {day:%d.%m}</b>"
    if not lessons:
        return head + "\nПар нет 🎉"
    return head + "\n\n" + "\n\n".join(fmt_lesson(l) for l in lessons)


def fmt_week(day):
    by_date = week_lessons(day)
    if not by_date:
        return "На этой неделе пар нет."
    return "\n\n".join(fmt_day(d, by_date[d]) for d in sorted(by_date))


# ---------- команды ----------

HELP = (f"Расписание группы <b>{GROUP_NAME}</b>\n\n"
        "/today — пары сегодня\n/tomorrow — пары завтра\n"
        "/week — эта неделя\n/next — следующая неделя\n/stop — отписаться от уведомлений\n\n"
        f"Утром в {MORNING:%H:%M} пришлю сводку, за {REMIND_BEFORE_MIN} мин до пары — напоминание, "
        "а при заменах — уведомление.")


def handle(state, msg):
    cid = msg["chat"]["id"]
    cmd = (msg.get("text") or "").split("@")[0].split()[0].lower() if msg.get("text") else ""
    today = now().date()
    try:
        if cmd in ("/start", "/help"):
            if cid not in state["chats"]:
                state["chats"].append(cid)
                save_state(state)
            send(cid, HELP)
        elif cmd == "/stop":
            if cid in state["chats"]:
                state["chats"].remove(cid)
                save_state(state)
            send(cid, "Уведомления выключены. /start — включить снова.")
        elif cmd == "/today":
            send(cid, fmt_day(today, day_lessons(today)))
        elif cmd == "/tomorrow":
            t = today + dt.timedelta(days=1)
            send(cid, fmt_day(t, day_lessons(t)))
        elif cmd == "/week":
            send(cid, fmt_week(today))
        elif cmd == "/next":
            send(cid, fmt_week(today + dt.timedelta(days=7)))
        elif cmd:
            send(cid, HELP)
    except Exception as ex:
        send(cid, f"Не удалось получить расписание с сайта: {html.escape(str(ex))}")


# ---------- фоновые задачи ----------

def lesson_key(d, l):
    return f"{d}|{l['start']}|{l['subject']}|{l['subgroup']}"


def check_changes(state):
    """Перекачивает эту и следующую неделю и сообщает об изменениях."""
    today = now().date()
    new = {}
    for day in (today, today + dt.timedelta(days=7)):
        for d, ls in week_lessons(day, force=True).items():
            if d < today:
                continue
            for l in ls:
                new[lesson_key(d, l)] = {"d": str(d), **{k: l[k] for k in
                                         ("room", "teacher", "cancelled", "moved_to")}}
    old = state["snapshot"]
    if old:
        notes = []
        for k, v in new.items():
            o = old.get(k)
            if o is None:
                continue
            d = dt.date.fromisoformat(v["d"])
            name = k.split("|")[2]
            when = f"{DAYS[d.isoweekday()]} {d:%d.%m} в {k.split('|')[1]}"
            if v["cancelled"] and not o["cancelled"]:
                notes.append(f"❌ <b>{html.escape(name)}</b> ({when}) — отменена")
            if v["moved_to"] and v["moved_to"] != o["moved_to"]:
                notes.append(f"↪️ <b>{html.escape(name)}</b> ({when}) — перенесена на {html.escape(v['moved_to'])}")
            if v["room"] != o["room"]:
                notes.append(f"🚪 <b>{html.escape(name)}</b> ({when}) — аудитория: "
                             f"{html.escape(o['room'] or '—')} → <b>{html.escape(v['room'] or '—')}</b>")
            if v["teacher"] != o["teacher"]:
                notes.append(f"👤 <b>{html.escape(name)}</b> ({when}) — преподаватель: "
                             f"{html.escape(o['teacher'] or '—')} → <b>{html.escape(v['teacher'] or '—')}</b>")
        for k, v in new.items():
            if k not in old:
                d = dt.date.fromisoformat(v["d"])
                if d <= today + dt.timedelta(days=7):
                    notes.append(f"➕ Новая пара: <b>{html.escape(k.split('|')[2])}</b> — "
                                 f"{DAYS[d.isoweekday()]} {d:%d.%m} в {k.split('|')[1]}")
        if notes:
            broadcast(state, "🔔 <b>Изменения в расписании</b>\n\n" + "\n".join(notes))
    state["snapshot"] = new
    save_state(state)


def timed_messages(state):
    t = now()
    today = t.date()
    sent = set(state["sent"])
    changed = False

    # утренняя сводка
    key = f"morning|{today}"
    if key not in sent and MORNING <= t.time() < (dt.datetime.combine(today, MORNING)
                                                    + dt.timedelta(hours=2)).time():
        lessons = day_lessons(today)
        if lessons:
            broadcast(state, "☀️ Доброе утро! Пары сегодня:\n\n" + fmt_day(today, lessons))
        state["sent"].append(key)
        changed = True

    # напоминания перед парами
    for l in day_lessons(today):
        if l["cancelled"] or l["moved_to"] or ":" not in l["start"]:
            continue
        h, m = map(int, l["start"].split(":"))
        start = dt.datetime.combine(today, dt.time(h, m))
        key = "remind|" + lesson_key(today, l)
        if key not in sent and start - dt.timedelta(minutes=REMIND_BEFORE_MIN) <= t < start:
            mins = max(1, round((start - t).total_seconds() / 60))
            broadcast(state, f"⏰ Через {mins} мин:\n\n" + fmt_lesson(l))
            state["sent"].append(key)
            changed = True

    if changed:
        save_state(state)


# ---------- главный цикл ----------

def main():
    if not TOKEN:
        raise SystemExit("Не задан токен бота: укажите BOT_TOKEN (см. инструкцию в начале файла).")
    # --run-for N: проработать N секунд и выйти (для запуска по расписанию на GitHub Actions)
    run_for = None
    if "--run-for" in sys.argv:
        run_for = int(sys.argv[sys.argv.index("--run-for") + 1])
    deadline = time.time() + run_for if run_for else None
    me = tg("getMe")["result"]
    print(f"Бот @{me['username']} запущен. Ctrl+C — остановить.")
    state = load_state()
    last_check = 0.0
    while deadline is None or time.time() < deadline:
        try:
            if time.time() - last_check > REFRESH_MIN * 60:
                last_check = time.time()
                try:
                    check_changes(state)
                except Exception as ex:
                    print(f"Проверка сайта не удалась: {ex}")
            try:
                timed_messages(state)
            except Exception as ex:
                print(f"Напоминания: {ex}")

            upd = tg("getUpdates", offset=state["offset"], timeout=20)
            for u in upd.get("result", []):
                state["offset"] = u["update_id"] + 1
                if "message" in u:
                    handle(state, u["message"])
            save_state(state)
        except KeyboardInterrupt:
            break
        except Exception as ex:
            print(f"Ошибка связи с Telegram: {ex}")
            time.sleep(5)


if __name__ == "__main__":
    main()
