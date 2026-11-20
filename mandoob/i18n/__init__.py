"""العربية | English: العربية هي الأصل والافتراضية، والإنجليزية ترجمة لنصوص الواجهة فقط.

- القاموس `en.json`: النص العربي كما هو في الواجهة ← الإنجليزي. `{x}` قيمة متغيرة (رقم/اسم/تاريخ)، و`{1}` `{2}` لتغيير
  ترتيبها في الإنجليزية.
- تُترجم الصفحة بعد تكوينها: نصوص العناصر وسمات placeholder/title/aria-label فقط. لا تُترجم محتويات `<textarea>`
  ولا قيم الحقول ولا أي عنصر عليه `translate="no"` (أسماء الشركات وملاحظات الموظفين تبقى بالنص الذي أُدخلت به).
- ملفات Excel: عناوين الأعمدة والأوراق والحالات؛ أعمدة الأسماء والملاحظات والبريد والمواقع لا تُلمس.
"""
from __future__ import annotations

import contextvars
import html as _html
import json
import re
from functools import lru_cache
from pathlib import Path

LANGS = ("ar", "en")
DEFAULT = "ar"
_LANG: contextvars.ContextVar[str] = contextvars.ContextVar("lang", default=DEFAULT)
_AR = re.compile(r"[؀-ۿ]")
_HERE = Path(__file__).parent


def current_lang() -> str:
    return _LANG.get()


def set_lang(lang: str):
    return _LANG.set(lang if lang in LANGS else DEFAULT)


def reset_lang(token) -> None:
    _LANG.reset(token)


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


@lru_cache(maxsize=1)
def _tables():
    exact: dict[str, str] = {}
    for f in sorted(_HERE.glob("en*.json")):  # en.json ثم en_extra.json (تصحيحات يدوية تتقدّم)
        exact.update({_norm(k): v for k, v in json.load(open(f, encoding="utf-8")).items() if v})
    try:  # أسماء المناطق والمدن من بيانات الجغرافيا نفسها
        from .. import geo
        for d in list(geo.REGIONS.values()) + list(geo.PLACES.values()):
            if d.get("ar") and d.get("en"):
                exact.setdefault(_norm(d["ar"]), d["en"])
    except Exception:  # noqa: BLE001
        pass
    pats = []
    for k, v in exact.items():
        if "{x}" not in k:
            continue
        lits = [p for p in k.split("{x}")]
        if len(lits) > 2 and len(_AR.findall("".join(lits))) < 5:  # أنماط عامة جدًا مثل «{x} من {x}» تُفسد الجمل — تُترك
            continue
        rx = re.compile("^" + "(.+?)".join(re.escape(p) for p in lits) + "$", re.S)
        longest = max(lits, key=len)
        pats.append((len(longest), longest, rx, v))
    pats.sort(key=lambda p: -p[0])
    return exact, pats


def _fill(template: str, groups: tuple[str, ...]) -> str:
    vals = [_tr_en(g) for g in groups]
    if re.search(r"\{\d+\}", template):
        return re.sub(r"\{(\d+)\}", lambda m: vals[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(vals) else m.group(0),
                      template)
    it = iter(vals)
    return re.sub(r"\{x\}", lambda m: next(it, ""), template)


def _lookup(t: str) -> str | None:
    exact, pats = _tables()
    if t in exact:
        return exact[t]
    for _, longest, rx, v in pats:
        if longest and longest not in t:
            continue
        m = rx.match(t)
        if m:
            return _fill(v, m.groups())
    return None


_SPLIT = re.compile(r"( — | – | · | \| |: |، |\. |\(|\)|«|»|“|”|\n)")


@lru_cache(maxsize=20000)
def _tr_en(text: str) -> str:
    core = _norm(text)
    if not core or not _AR.search(core):
        return text
    got = _lookup(core)
    if got is None:
        parts = _SPLIT.split(core)
        if len(parts) > 1:
            out, changed = [], False
            for p in parts:
                if _AR.search(p):
                    q = _lookup(p.strip())
                    if q is not None:
                        lead = p[: len(p) - len(p.lstrip())]
                        trail = p[len(p.rstrip()):]
                        p, changed = lead + q + trail, True
                out.append({"«": "“", "»": "”", "، ": ", "}.get(p, p))
            if changed:
                got = "".join(out)
    if got is None:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return lead + got + trail


def tr(text: str, lang: str | None = None) -> str:
    """ترجمة نص واجهة إلى لغة المستخدم (يعيد النص كما هو بالعربية أو إن لم يوجد له مقابل)."""
    if (lang or current_lang()) != "en" or not isinstance(text, str):
        return text
    return _tr_en(text)


# ---------------------------------------------------------------- صفحات HTML

_TOKENS = re.compile(r"(<!--.*?-->|<script\b.*?</script\s*>|<style\b.*?</style\s*>|<textarea\b.*?</textarea\s*>"
                     r"|<pre\b.*?</pre\s*>|<[^>]+>)", re.S | re.I)
_ATTRS = re.compile(r'\b(placeholder|title|aria-label|data-tip)="([^"]*)"')
_TAGNAME = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9]+)")
_VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "base", "col", "embed", "param", "track"}


def translate_html(page: str, lang: str | None = None) -> str:
    if (lang or current_lang()) != "en":
        return page
    out = []
    skip_tag, depth = None, 0  # عنصر translate="no" مفتوح
    for tok in _TOKENS.split(page):
        if not tok:
            continue
        if tok.startswith("<"):
            low = tok[:12].lower()
            if low.startswith(("<!--", "<script", "<style", "<textarea", "<pre")):
                out.append(tok)
                continue
            m = _TAGNAME.match(tok)
            if m:
                closing, name = m.group(1) == "/", m.group(2).lower()
                if skip_tag:
                    if name == skip_tag and name not in _VOID and not tok.endswith("/>"):
                        depth += -1 if closing else 1
                        if depth == 0:
                            skip_tag = None
                    out.append(tok)
                    continue
                if not closing and 'translate="no"' in tok:
                    if name not in _VOID and not tok.endswith("/>"):
                        skip_tag, depth = name, 1
                    out.append(tok)
                    continue
                if not closing and _AR.search(tok):
                    tok = _ATTRS.sub(lambda a: f'{a.group(1)}="{_html.escape(_tr_en(_html.unescape(a.group(2))), quote=True)}"',
                                     tok)
            out.append(tok)
            continue
        if skip_tag or not _AR.search(tok):
            out.append(tok)
            continue
        raw = _html.unescape(tok)
        new = _tr_en(raw)
        out.append(tok if new == raw else _html.escape(new, quote=False))
    return "".join(out)


# ---------------------------------------------------------------- ملفات Excel

_PROTECT = ("ملاحظة", "المنشأة", "الشركة", "اسم", "البريد", "الموقع", "الرابط", "مصدر", "نص", "العنوان", "الهاتف",
            "رقم", "عبارة", "سبب", "المسؤول", "الموظف", "كاتب", "Note", "Name")


def localize_workbook(wb, lang: str | None = None) -> None:
    """يترجم عناوين الأعمدة وأسماء الأوراق والقيم المعرّفة (الحالات) — ولا يلمس الأسماء والملاحظات والبيانات."""
    if (lang or current_lang()) != "en":
        return
    for ws in wb.worksheets:
        protected: set[int] = set()
        seen_header = False
        for row in ws.iter_rows():
            header_like = not seen_header and sum(1 for c in row if isinstance(c.value, str) and _norm(c.value) in _tables()[0]) >= 3
            if header_like:
                seen_header = True
                protected = {c.column for c in row if isinstance(c.value, str) and any(w in c.value for w in _PROTECT)}
            for c in row:
                v = c.value
                if not isinstance(v, str) or not _AR.search(v):
                    continue
                if header_like or not seen_header:  # العناوين وأسطر التعريف أعلى الورقة
                    c.value = _tr_en(v)
                elif c.column not in protected and _norm(v) in _tables()[0]:
                    c.value = _tables()[0][_norm(v)]
        title = _tr_en(ws.title)
        if title != ws.title:
            ws.title = re.sub(r"[\[\]\*\?/\\:]", " ", title)[:31]
        try:
            ws.sheet_view.rightToLeft = False
        except Exception:  # noqa: BLE001
            pass
