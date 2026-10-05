#!/usr/bin/env python3
"""
Расписание группы МБ-24-08 (РГУ нефти и газа им. Губкина).

Берёт данные из того же публичного API, что и страница
https://lk.gubkin.ru/schedule/  (логин не нужен), и:
  * печатает пары недели в консоль: время, предмет, аудитория, преподаватель;
  * сохраняет красивую страницу schedule.html (сама обновляется в браузере);
  * повторяет запрос каждые REFRESH_MINUTES минут, чтобы данные были свежими.

Запуск:   python gubkin_schedule.py           — работать постоянно с обновлением
          python gubkin_schedule.py --once    — один раз и выйти
          python gubkin_schedule.py --next    — показать следующую неделю
Нужен только Python 3.8+, сторонние библиотеки не требуются.
"""

import datetime as dt
import html
import http.cookiejar
import json
import sys
import time
import urllib.request

GROUP_ID = "9685"          # МБ-24-08 (из ссылки groupId=9685)
GROUP_NAME = "МБ-24-08"
REFRESH_MINUTES = 30
HTML_FILE = "schedule.html"

BASE = "https://lk.gubkin.ru/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
DAYS = ["", "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def _get(path, accept="application/json, text/plain, */*"):
    req = urllib.request.Request(BASE + path, headers={
        "User-Agent": UA, "Accept": accept, "Referer": BASE + "schedule/"})
    with _opener.open(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def fetch_week(date):
    """JSON недели, в которую входит date. Сайт сначала требует «визит» за cookie."""
    path = (f"schedule/api/api.php?act=schedule&date="
            f"{date.day}-{date.month}-{date.year}&groupId={GROUP_ID}")
    for attempt in range(2):
        if attempt or not len(_jar):
            _get("schedule/", accept="text/html,*/*")   # получаем PHPSESSID
        try:
            text = _get(path)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RuntimeError("Сайт просит капчу — откройте его в браузере и попробуйте позже")
            if attempt:
                raise
            continue
        if text.lstrip().startswith("{"):
            data = json.loads(text)
            if data.get("state") is False:
                raise RuntimeError(f"Сайт вернул ошибку: {data.get('reason') or data.get('message')}")
            return data
    raise RuntimeError("Сайт вернул страницу вместо данных")


# ---------- разбор ответа ----------

def _rooms(v):
    out = []
    for r in v or []:
        if isinstance(r, dict):
            n = r.get("number") or r.get("num") or r.get("name") or r.get("title")
            if n:
                out.append(str(n))
        elif r:
            out.append(str(r))
    return ", ".join(out)


def _teachers(v):
    out = []
    for t in v or []:
        if not isinstance(t, dict):
            out.append(str(t)); continue
        last = (t.get("lastName") or "").strip()
        first = (t.get("firstName") or "").strip()
        mid = (t.get("middleName") or t.get("patronymic") or t.get("secondName") or "").strip()
        if last:
            out.append(" ".join(x for x in (last, first, mid) if x))
        elif t.get("fio") or t.get("fullName"):
            out.append(t.get("fio") or t.get("fullName"))
    return ", ".join(out)


def parse(data):
    rows = data.get("rows") or {}
    week = (rows.get("week") or {}).get("weekRussia") or {}
    dates = {d["weekDayNumber"]: d["date"] for d in week.get("days", [])
             if "weekDayNumber" in d and "date" in d}
    week_type = {"upper": "верхняя", "lower": "нижняя"}.get(week.get("type"), week.get("type") or "")

    lessons = []
    for org in rows.get("organizations") or []:
        chunks = org.get("lessonsTimeChunks") or []
        for l in org.get("lessons") or []:
            groups = l.get("groups") or []
            if groups and not any(str(g.get("id")) == GROUP_ID for g in groups if isinstance(g, dict)):
                continue
            tc = [c for c in (l.get("timeChunks") or []) if isinstance(c, int) and c < len(chunks)]
            start = chunks[min(tc)].split("-")[0] if tc else "?"
            end = chunks[max(tc)].split("-")[-1] if tc else "?"
            ch = l.get("changes") if isinstance(l.get("changes"), dict) else {}
            subgroup = 0
            for g in groups:
                if isinstance(g, dict) and str(g.get("id")) == GROUP_ID:
                    subgroup = g.get("subgroup") or 0
            lessons.append({
                "day": l.get("weekDayNumber"),
                "start": start, "end": end,
                "subject": ((l.get("course") or {}).get("name") or l.get("name") or "—").strip(),
                "type": l.get("type") or "",
                "room": _rooms(ch.get("rooms")) or _rooms(l.get("rooms")),
                "room_changed": bool(ch.get("rooms")),
                "teacher": _teachers(ch.get("teachers")) or _teachers(l.get("teachers")),
                "teacher_changed": bool(ch.get("teachers")),
                "cancelled": bool(l.get("isCanceled") or l.get("isCancelled")),
                "moved_to": l.get("movedTo") or "",
                "subgroup": subgroup,
            })
    lessons.sort(key=lambda x: (x["day"] or 0, x["start"]))
    return dates, week_type, lessons


# ---------- вывод ----------

def print_console(dates, week_type, lessons):
    print(f"\n=== {GROUP_NAME} · неделя {week_type} · обновлено {dt.datetime.now():%d.%m %H:%M} ===")
    if not lessons:
        print("Пар на этой неделе нет.")
    cur = None
    for l in lessons:
        if l["day"] != cur:
            cur = l["day"]
            print(f"\n{DAYS[cur] if cur and cur < 8 else cur}  {dates.get(cur, '')}")
        mark = ""
        if l["cancelled"]:
            mark = "  [ОТМЕНЕНА]"
        elif l["moved_to"]:
            mark = f"  [ПЕРЕНЕСЕНА на {l['moved_to']}]"
        sub = f" (подгр. {l['subgroup']})" if l["subgroup"] else ""
        print(f"  {l['start']}–{l['end']}  {l['subject']} {('· ' + l['type']) if l['type'] else ''}{sub}{mark}")
        print(f"      ауд.: {l['room'] or '—'}{' (замена)' if l['room_changed'] else ''}"
              f"   преп.: {l['teacher'] or '—'}{' (замена)' if l['teacher_changed'] else ''}")


def write_html(dates, week_type, lessons):
    e = html.escape
    parts, cur = [], None
    for l in lessons:
        if l["day"] != cur:
            if cur is not None:
                parts.append("</div>")
            cur = l["day"]
            parts.append(f'<div class="day"><h2>{e(DAYS[cur] if cur and cur < 8 else str(cur))}'
                         f' <span>{e(dates.get(cur, ""))}</span></h2>')
        cls = "lesson" + (" off" if l["cancelled"] or l["moved_to"] else "")
        note = "Отменена" if l["cancelled"] else (f"Перенесена на {l['moved_to']}" if l["moved_to"] else "")
        parts.append(
            f'<div class="{cls}"><div class="time">{e(l["start"])}<br>{e(l["end"])}</div><div>'
            f'<div class="subj">{e(l["subject"])}</div>'
            f'<div class="meta">{e(l["type"])}{" · подгр. " + str(l["subgroup"]) if l["subgroup"] else ""}</div>'
            f'<div class="row">🚪 <span class="{"chg" if l["room_changed"] else ""}">{e(l["room"] or "—")}</span></div>'
            f'<div class="row">👤 <span class="{"chg" if l["teacher_changed"] else ""}">{e(l["teacher"] or "—")}</span></div>'
            f'{f"<div class=chg>{e(note)}</div>" if note else ""}</div></div>')
    if cur is not None:
        parts.append("</div>")
    body = "".join(parts) or "<p>Пар на этой неделе нет.</p>"
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_MINUTES * 60}">
<title>Расписание {GROUP_NAME}</title><style>
body{{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1d1f23;margin:0;padding:16px;max-width:720px;margin:auto}}
h1{{font-size:22px;margin:4px 0}} .sub{{color:#666;font-size:13px;margin-bottom:16px}}
.day{{background:#fff;border-radius:12px;padding:12px 14px;margin-bottom:12px;box-shadow:0 1px 3px #0001}}
h2{{font-size:17px;margin:0 0 8px}} h2 span{{color:#888;font-weight:400;font-size:14px}}
.lesson{{display:flex;gap:12px;padding:8px 0;border-top:1px solid #eee}} .lesson:first-of-type{{border:0}}
.time{{min-width:48px;font-variant-numeric:tabular-nums;color:#555;font-size:14px}}
.subj{{font-weight:600}} .meta{{color:#888;font-size:13px}} .row{{font-size:14px;margin-top:2px}}
.chg{{color:#d22;font-weight:600}} .off .subj{{text-decoration:line-through;color:#999}}
@media (prefers-color-scheme:dark){{body{{background:#16171a;color:#e8e8e8}}.day{{background:#222327}}.lesson{{border-color:#333}}}}
</style></head><body><h1>{GROUP_NAME}</h1>
<div class="sub">Неделя {e(week_type)} · обновлено {dt.datetime.now():%d.%m.%Y %H:%M}</div>{body}</body></html>"""
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(page)


def update(date):
    dates, week_type, lessons = parse(fetch_week(date))
    print_console(dates, week_type, lessons)
    write_html(dates, week_type, lessons)
    print(f"\nСтраница сохранена: {HTML_FILE}")


def main():
    once = "--once" in sys.argv
    shift = 7 if "--next" in sys.argv else 0
    while True:
        try:
            update(dt.date.today() + dt.timedelta(days=shift))
        except Exception as ex:
            print(f"Не удалось обновить: {ex}")
        if once:
            break
        print(f"Следующее обновление через {REFRESH_MINUTES} мин. (Ctrl+C — выход)")
        time.sleep(REFRESH_MINUTES * 60)


if __name__ == "__main__":
    main()
