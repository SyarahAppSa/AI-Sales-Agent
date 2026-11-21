"""«بحث مباشر في خرائط Google» — للعرض فقط.

- Places API (New) Text Search من الخادم بالمفتاح GOOGLE_PLACES_API_KEY (لا يصل المفتاح للمتصفح).
- النتائج تُعرض في الصفحة وعلى خريطة Google (صورة Maps Static تُجلب عبر الخادم) ولا تُحفظ في قاعدة البيانات:
  شروط Google لا تسمح بتخزين محتوى الأماكن. السجلات المحفوظة تأتي من المصادر المستقلة (Overture وغيرها).
- يُحفظ فقط حالة الاتصال (وقت آخر اختبار، نجاح/خطأ، عدد النتائج) لعرضها — بلا أي بيانات منشآت.
"""
from __future__ import annotations

import httpx

from .config import settings
from .db import session_scope
from .models import Setting
from .timeutil import now_utc

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ("places.id,places.displayName,places.formattedAddress,places.location,places.nationalPhoneNumber,"
          "places.websiteUri,places.googleMapsUri,places.rating,places.userRatingCount,places.businessStatus,"
          "places.primaryTypeDisplayName")
STATUS_KEY = "google_live_status"
COST_PER_SEARCH = 0.035  # Text Search بحقول الهاتف والموقع (Enterprise) — تقديري لكل طلب
STATIC_URL = "https://maps.googleapis.com/maps/api/staticmap"


class LiveError(Exception):
    pass


def _explain(code: int, body: dict) -> str:
    err = (body or {}).get("error") or {}
    status, msg = err.get("status") or "", (err.get("message") or "")[:160]
    if code == 403 and ("not been used" in msg or "disabled" in msg or "SERVICE_DISABLED" in str(err)):
        return "Places API (New) غير مفعّلة لهذا المفتاح في Google Cloud — فعّلها ثم أعد الاختبار"
    if code == 403:
        return f"رفضت Google المفتاح ({status}) — تحقق من قيود المفتاح (عنوان IP/الواجهات المسموحة)"
    if code == 400 and "API key" in msg:
        return "المفتاح غير صالح — تحقق من قيمته في Render"
    if code == 429:
        return "تجاوزت حصة الطلبات في Google Cloud"
    return f"خطأ من Google (HTTP {code} {status}) {msg}".strip()


def set_status(**kw) -> None:
    with session_scope() as s:
        row = s.get(Setting, STATUS_KEY)
        v = {**((row.value if row else None) or {}), **kw, "at": now_utc().isoformat()}
        if row is None:
            s.add(Setting(key=STATUS_KEY, value=v))
        else:
            row.value = v


def get_status(s) -> dict:
    row = s.get(Setting, STATUS_KEY)
    return dict((row.value if row else None) or {})


def search(query: str, center: tuple[float, float] | None = None, http: httpx.Client | None = None) -> list[dict]:
    key = settings.google_places_api_key
    if not key:
        set_status(ok=False, error="المفتاح GOOGLE_PLACES_API_KEY غير مضبوط على الخادم", query=query, count=0)
        raise LiveError("المفتاح GOOGLE_PLACES_API_KEY غير مضبوط على الخادم — أضفه في Render ثم أعد الاختبار")
    body = {"textQuery": query, "languageCode": "ar", "regionCode": "SA", "pageSize": 20}
    if center:
        body["locationBias"] = {"circle": {"center": {"latitude": center[0], "longitude": center[1]}, "radius": 40000.0}}
    cl = http or httpx.Client(timeout=20)
    try:
        r = cl.post(URL, json=body, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS})
    except httpx.HTTPError as e:
        set_status(ok=False, error=f"تعذّر الوصول إلى Google ({type(e).__name__})", query=query, count=0)
        raise LiveError(f"تعذّر الوصول إلى Google ({type(e).__name__})") from None
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = {}
    from .costs import record_cost
    record_cost("google_places", 1, COST_PER_SEARCH, note="google_live")
    if r.status_code != 200:
        why = _explain(r.status_code, data)
        set_status(ok=False, error=why, http=r.status_code, query=query, count=0)
        raise LiveError(why)
    out = []
    for p in data.get("places") or []:
        loc = p.get("location") or {}
        out.append({"name": (p.get("displayName") or {}).get("text", ""), "address": p.get("formattedAddress", ""),
                    "phone": p.get("nationalPhoneNumber", ""), "website": p.get("websiteUri", ""),
                    "maps": p.get("googleMapsUri", ""), "rating": p.get("rating"), "reviews": p.get("userRatingCount"),
                    "type": (p.get("primaryTypeDisplayName") or {}).get("text", ""), "status": p.get("businessStatus", ""),
                    "lat": loc.get("latitude"), "lng": loc.get("longitude")})
    set_status(ok=True, error=None, http=200, query=query, count=len(out))
    return out


def static_map(points: list[tuple[float, float]], http: httpx.Client | None = None) -> tuple[bytes, str]:
    """صورة خريطة Google بعلامات مرقّمة — تُجلب عبر الخادم حتى لا يظهر المفتاح في المتصفح."""
    key = settings.google_places_api_key
    if not key:
        raise LiveError("لا مفتاح")
    labels = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    params = [("size", "640x420"), ("scale", "2"), ("language", "ar"), ("region", "SA"), ("key", key)]
    for i, (la, ln) in enumerate(points[:20]):
        params.append(("markers", f"color:0x6941A5|label:{labels[i]}|{la:.6f},{ln:.6f}"))
    cl = http or httpx.Client(timeout=20)
    r = cl.get(STATIC_URL, params=params)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise LiveError("Maps Static API غير مفعّلة لهذا المفتاح — فعّلها لعرض الخريطة")
    from .costs import record_cost
    record_cost("google_static_map", 1, 0.002, note="google_live")
    return r.content, r.headers.get("content-type", "image/png")
