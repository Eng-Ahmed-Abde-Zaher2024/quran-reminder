#!/usr/bin/env python3
"""تنبيه يومي بورد مراجعة القرآن على جروب تيليجرام.

الاستخدام:
  python quran_reminder.py             التشغيل العادي (بتشغله الجدولة)
  python quran_reminder.py --dry-run   يطبع رسالة اليوم من غير إرسال ومن غير مفتاح
  python quran_reminder.py --force     يبعت رسالة اليوم فورًا للتجربة
  python quran_reminder.py --day 3     رقم يوم معيّن (مع --dry-run أو --force)

المتغير الوحيد المطلوب: TELEGRAM_BOT_TOKEN (مفتاح البوت).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
SURAHS = json.loads((ROOT / "surahs.json").read_text(encoding="utf-8"))  # [[اسم, صفحة البداية], ...]
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}


def die(msg: str, code: int = 1) -> None:
    print(f"خطأ: {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"الملف {path.name} فيه خطأ في الصياغة: {e}")


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- الخطة
@dataclass
class Plan:
    group_name: str
    tz: ZoneInfo
    send_time: dtime
    start: date
    from_surah: int
    to_surah: int
    per_day: int
    rest: int | None
    chat_id: int | None
    completion_msg: bool

    @property
    def first_page(self) -> int:
        return SURAHS[self.from_surah - 1][1]

    @property
    def last_page(self) -> int:
        return end_page(self.to_surah - 1)

    @property
    def total_pages(self) -> int:
        return self.last_page - self.first_page + 1

    @property
    def total_days(self) -> int:
        return math.ceil(self.total_pages / self.per_day)

    @property
    def signature(self) -> str:
        return f"{self.from_surah}-{self.to_surah}-{self.per_day}-{self.start.isoformat()}"


def load_config() -> Plan:
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        die("ملف config.json مش موجود.")
    try:
        hh, mm = (int(x) for x in str(cfg["send_time"]).split(":"))
        rest_name = cfg.get("rest_weekday")
        plan = Plan(
            group_name=str(cfg.get("group_name") or ""),
            tz=ZoneInfo(cfg.get("timezone") or "Africa/Cairo"),
            send_time=dtime(hh, mm),
            start=date.fromisoformat(cfg["start_date"]),
            from_surah=int(cfg["from_surah"]),
            to_surah=int(cfg["to_surah"]),
            per_day=int(cfg["pages_per_day"]),
            rest=WEEKDAYS[rest_name.lower()] if rest_name else None,
            chat_id=int(cfg["chat_id"]) if cfg.get("chat_id") else None,
            completion_msg=bool(cfg.get("send_completion_message", True)),
        )
    except (KeyError, ValueError, TypeError) as e:
        die(f"في قيمة غلط أو ناقصة في config.json: {e!r}")
    if not 1 <= plan.from_surah <= plan.to_surah <= 114:
        die("from_surah لازم يكون أصغر من أو يساوي to_surah، وكلاهما بين 1 و114.")
    if plan.per_day < 1:
        die("pages_per_day لازم يكون 1 أو أكتر.")
    return plan


def end_page(i: int) -> int:
    """آخر صفحة تقريبية للسورة (بداية اللي بعدها، أو 604 للناس)."""
    if i == 113:
        return 604
    return max(SURAHS[i][1], SURAHS[i + 1][1] - 1)


def surah_at(page: int) -> str:
    k = 0
    for i, (_, start) in enumerate(SURAHS):
        if start <= page:
            k = i
        else:
            break
    return SURAHS[k][0]


def day_number(plan: Plan, d: date) -> int | None:
    """0 = قبل البداية، None = يوم راحة، غير كده رقم اليوم في الخطة."""
    if d < plan.start:
        return 0
    if plan.rest is not None and d.weekday() == plan.rest:
        return None
    n, cur = 0, plan.start
    while cur <= d:
        if plan.rest is None or cur.weekday() != plan.rest:
            n += 1
        cur += timedelta(days=1)
    return n


def day_pages(plan: Plan, n: int) -> tuple[int, int]:
    first = plan.first_page + (n - 1) * plan.per_day
    return first, min(plan.last_page, first + plan.per_day - 1)


def build_message(plan: Plan, n: int) -> str:
    first, last = day_pages(plan, n)
    count = last - first + 1
    unit = "صفحة" if count == 1 else "صفحات"
    return (
        "السلام عليكم ورحمة الله وبركاته\n"
        f"ورد المراجعة النهاردة (اليوم {n} من {plan.total_days}):\n"
        f"من صفحة {first} (سورة {surah_at(first)})\n"
        f"إلى صفحة {last} (سورة {surah_at(last)})\n"
        f"يعني {count} {unit}.\n"
        "تقبّل الله منا ومنكم."
    )


def completion_message(plan: Plan) -> str:
    return (
        "ما شاء الله، خلّصنا خطة المراجعة "
        f"من سورة {SURAHS[plan.from_surah - 1][0]} إلى سورة {SURAHS[plan.to_surah - 1][0]}.\n"
        "تقبّل الله منا ومنكم."
    )


# ---------------------------------------------------------------- تيليجرام
class TelegramError(Exception):
    def __init__(self, description: str, params: dict | None = None):
        super().__init__(description)
        self.params = params or {}


def tg(token: str, method: str, payload: dict | None = None):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            body = {"ok": False, "description": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        raise TelegramError(f"مشكلة في الاتصال بتيليجرام: {e.reason}")
    if not body.get("ok"):
        raise TelegramError(body.get("description", "خطأ غير معروف"), body.get("parameters"))
    return body["result"]


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def discover_chat(token: str, group_name: str):
    """يدوّر على الجروب اللي البوت اتضاف له من آخر تحديثات تيليجرام."""
    updates = tg(token, "getUpdates",
                 {"timeout": 0, "allowed_updates": ["message", "my_chat_member", "channel_post"]})
    found: dict[int, str] = {}
    for u in updates:
        for key in ("message", "my_chat_member", "channel_post"):
            chat = (u.get(key) or {}).get("chat") or {}
            if chat.get("type") in ("group", "supergroup"):
                found.pop(chat["id"], None)
                found[chat["id"]] = chat.get("title", "")
    if not found:
        return None
    want = _norm(group_name)
    if want:
        for cid, title in reversed(list(found.items())):
            if _norm(title) == want:
                return cid, title
    if len(found) > 1:
        print("لقيت أكتر من جروب، هاخد آخر واحد:", ", ".join(found.values()))
    cid, title = list(found.items())[-1]
    return cid, title


def send_text(token: str, state: dict, text: str) -> None:
    try:
        tg(token, "sendMessage", {"chat_id": state["chat_id"], "text": text})
    except TelegramError as e:
        new_id = e.params.get("migrate_to_chat_id")
        if not new_id:
            raise
        state["chat_id"] = new_id  # الجروب اتحوّل لسوبر جروب
        save_json(STATE_PATH, state)
        tg(token, "sendMessage", {"chat_id": new_id, "text": text})


# ---------------------------------------------------------------- التشغيل
def main() -> int:
    ap = argparse.ArgumentParser(description="تنبيه يومي لورد مراجعة القرآن")
    ap.add_argument("--dry-run", action="store_true", help="اطبع الرسالة من غير إرسال")
    ap.add_argument("--force", action="store_true", help="ابعت الآن وتجاهل الوقت وعدم التكرار")
    ap.add_argument("--day", type=int, help="رقم يوم معيّن للمعاينة أو التجربة")
    args = ap.parse_args()
    preview = args.dry_run or args.force

    plan = load_config()
    state = load_json(STATE_PATH, {})
    if state.get("plan_sig") != plan.signature:
        state["plan_sig"] = plan.signature
        state["finished"] = False
        if not args.dry_run:
            save_json(STATE_PATH, state)

    now = datetime.now(plan.tz)
    today = now.date()
    total = plan.total_days

    # اختيار رقم اليوم
    if args.day is not None:
        if not 1 <= args.day <= total:
            die(f"--day لازم يكون بين 1 و{total}.")
        n = args.day
    else:
        n = day_number(plan, today)
        if preview and (n in (0, None)):
            print("(تجربة: النهاردة خارج أيام الخطة، هعرض اليوم 1)")
            n = 1

    # مفتاح البوت واكتشاف الجروب (بيتم مبكرًا عشان تحديثات تيليجرام بتتحذف بعد 24 ساعة)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if args.dry_run:
        print(build_message(plan, n if isinstance(n, int) and 1 <= n <= total else 1))
        return 0
    if not token:
        die("مفتاح البوت مش موجود. ضيفه كسر باسم TELEGRAM_BOT_TOKEN في إعدادات GitHub.")

    chat_id = plan.chat_id or state.get("chat_id")
    if not chat_id:
        try:
            hit = discover_chat(token, plan.group_name)
        except TelegramError as e:
            die(f"تيليجرام رفض الطلب: {e} (راجع مفتاح البوت).")
        if hit:
            chat_id, title = hit
            state["chat_id"], state["chat_title"] = chat_id, title
            save_json(STATE_PATH, state)
            print(f"اكتشفت الجروب: {title}")
        else:
            print("لسه مفيش جروب: ضيف البوت للجروب وابعت أي رسالة فيه، وهلاقيه في التشغيل الجاي.")
            return 1 if args.force else 0
    state["chat_id"] = chat_id

    # قرارات الإرسال
    if not args.force and args.day is None:
        if n == 0:
            print(f"الخطة لسه ما بدأتش (بتبدأ {plan.start}).")
            return 0
        if n is None:
            print("النهاردة يوم راحة.")
            return 0
        if now.time() < plan.send_time:
            print(f"لسه بدري. وقت الإرسال {plan.send_time:%H:%M} بتوقيت {plan.tz.key}.")
            return 0

        if n > total:  # الخطة خلصت
            if plan.completion_msg and not state.get("finished"):
                send_text(token, state, completion_message(plan))
                state["finished"] = True
                save_json(STATE_PATH, state)
                print("اتبعتت رسالة إتمام الخطة.")
            else:
                print("الخطة خلصت.")
            return 0

        if state.get("last_sent") == today.isoformat():
            print("رسالة النهاردة اتبعتت قبل كده.")
            return 0

    text = build_message(plan, n)
    try:
        send_text(token, state, text)
    except TelegramError as e:
        die(f"فشل الإرسال: {e}")
    if not args.force and args.day is None:
        state["last_sent"] = today.isoformat()
        save_json(STATE_PATH, state)
    print(f"اتبعت ورد اليوم {n}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
