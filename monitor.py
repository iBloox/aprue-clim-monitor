#!/usr/bin/env python3
"""
مراقب حصص برنامج استبدال المكيفات (APRUE / PNME - Clim 2000).

الفكرة:
  1. تحميل صفحة حالة برنامج المكيفات (Clim_2000_1.php) مباشرة، بدون
     تسجيل دخول — تم التأكد أن الصفحة تظهر بشكل طبيعي حتى بدون جلسة
     مسجّلة (اختُبر يدويًا في نافذة تصفح خاصة/incognito).
  2. التحقق: هل ما زالت رسالة "التسجيلات مغلقة" موجودة؟
     - إذا اختفت ⇒ يُفترض أن حصة جديدة فُتحت ⇒ نرسل إشعار.
  3. حفظ آخر حالة معروفة في state.json حتى لا نكرر نفس الإشعار
     في كل تشغيلة (كل 15 دقيقة).

ملاحظة: لو تغيّر سلوك الموقع مستقبلًا (مثلاً صار يطلب تسجيل دخول فعلًا
بعد فتح الحصص)، الكود يتعرف على صفحة تسجيل الدخول ويتجاهل تلك الدورة
بدل ما يرسل إشعار خاطئ أو يفشل بشكل صامت.
"""

import os
import sys
import json
import datetime

import requests
import urllib3

# الموقع لا يرسل سلسلة شهادة SSL كاملة (ينقصه intermediate certificate) —
# متصفحات الويب تتجاوز هذا تلقائيًا، لكن مكتبات بايثون القياسية لا تفعل.
# نعطّل التحقق الصارم لهذا الموقع تحديدًا فقط: نحن فقط نقرأ صفحة عامة
# بدون تسجيل دخول أو بيانات حساسة، فالمخاطرة هنا منخفضة جدًا (أسوأ سيناريو:
# إشعار خاطئ، والمستخدم يتحقق يدويًا من الرابط قبل التسجيل).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://pnme.aprue.org.dz"
STATUS_URL = f"{BASE_URL}/Clim_2000_1.php"

# النص المميز الذي يظهر فقط أثناء إغلاق التسجيلات.
# إذا غيّرت APRUE صياغة الرسالة مستقبلًا، حدّث هذا النص.
CLOSED_MARKER = "تم غلق التسجيلات"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"is_open": False, "last_checked": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_status() -> bool | None:
    """
    يرجع:
      True  إذا كانت الحصص (على الأرجح) مفتوحة الآن،
      False إذا كانت لا تزال مغلقة،
      None  إذا تعذّر تحديد الحالة (مثلاً ظهرت صفحة تسجيل دخول غير متوقعة)
            — في هذه الحالة يتم تجاهل الدورة بدل إرسال إشعار خاطئ.
    """
    resp = requests.get(STATUS_URL, headers=HEADERS, timeout=30, verify=False)
    resp.raise_for_status()
    html = resp.text

    # حارس أمان: لو صار الموقع يطلب تسجيل دخول فعليًا مستقبلًا
    if 'id="form_connexion"' in html or 'id="user_connexion"' in html:
        print(
            "تحذير: الصفحة رجعت نموذج تسجيل دخول بدل صفحة الحالة. "
            "قد يكون الموقع بدّل سلوكه (مثلاً صار يتطلب جلسة مسجّلة). "
            "تجاهلنا هذه الدورة احتياطًا.",
            file=sys.stderr,
        )
        return None

    return CLOSED_MARKER not in html


def send_telegram(token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url, data={"chat_id": chat_id, "text": message}, timeout=15
        )
        if not r.ok:
            print(f"Telegram error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")


def send_ntfy(topic: str, message: str, title: str) -> None:
    url = f"https://ntfy.sh/{topic}"
    try:
        r = requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "urgent",
                "Tags": "rotating_light",
            },
            timeout=15,
        )
        if not r.ok:
            print(f"ntfy error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"ntfy send failed: {e}")


def main() -> int:
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    ntfy_topic = os.environ.get("NTFY_TOPIC")

    state = load_state()
    was_open = bool(state.get("is_open", False))

    is_open = check_status()

    if is_open is None:
        # تعذّر تحديد الحالة هذه الدورة (راجع رسالة التحذير أعلاه).
        # لا نغيّر state.json ولا نرسل إشعار — ننتظر الدورة القادمة.
        return 0

    print(f"الحالة السابقة: {'مفتوحة' if was_open else 'مغلقة'}")
    print(f"الحالة الحالية: {'مفتوحة' if is_open else 'مغلقة'}")

    if is_open and not was_open:
        message = (
            "🎉 يبدو أن حصة جديدة فُتحت في برنامج استبدال المكيفات (APRUE)!\n"
            "سجّل بسرعة قبل نفاد الحصة:\n"
            f"{STATUS_URL}"
        )
        if tg_token and tg_chat_id:
            send_telegram(tg_token, tg_chat_id, message)
        if ntfy_topic:
            send_ntfy(ntfy_topic, message, "🚨 حصص المكيفات مفتوحة!")
        print("تم إرسال الإشعار.")
    elif is_open and was_open:
        print("لا تغيير: الحصص ما زالت مفتوحة (تم الإشعار مسبقًا).")
    else:
        print("لا تغيير: الحصص ما زالت مغلقة.")

    now = datetime.datetime.now(datetime.timezone.utc)
    state_changed = is_open != was_open
    last_saved_str = state.get("last_saved")
    heartbeat_due = True
    if last_saved_str:
        try:
            last_saved = datetime.datetime.fromisoformat(last_saved_str)
            heartbeat_due = (now - last_saved) >= datetime.timedelta(days=25)
        except ValueError:
            heartbeat_due = True

    if state_changed or heartbeat_due:
        state["is_open"] = is_open
        state["last_checked"] = now.isoformat()
        state["last_saved"] = now.isoformat()
        save_state(state)
    else:
        print("لا تغيير بالحالة، وما حان وقت heartbeat — ما راح نعمل commit هذي الدورة.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
