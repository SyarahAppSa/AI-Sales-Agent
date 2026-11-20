"""يستخرج نصوص الواجهة العربية من القوالب وملفات بايثون المختارة (مفاتيح قاموس الترجمة)."""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AR = re.compile(r"[؀-ۿ]")
keys: dict[str, str] = {}


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def add(t, where):
    t = norm(t)
    if AR.search(t) and len(t) < 600:
        keys.setdefault(t, where)


for p in sorted((ROOT / "mandoob/web/templates").glob("*.html")):
    src = p.read_text()
    src = re.sub(r"<script.*?</script>|<style.*?</style>", " ", src, flags=re.S)
    # نصوص حرفية داخل تعابير Jinja
    for expr in re.findall(r"\{[{%](.*?)[}%]\}", src, flags=re.S):
        for lit in re.findall(r"'([^']*)'|\"([^\"]*)\"", expr):
            add(lit[0] or lit[1], p.name)
    s = re.sub(r"\{\{.*?\}\}", "{x}", src, flags=re.S)
    s = re.sub(r"\{%.*?%\}", "<>", s, flags=re.S)
    s = re.sub(r"\{#.*?#\}", "<>", s, flags=re.S)
    for attr in re.findall(r'(?:placeholder|title|aria-label|value|data-tip)="([^"]*)"', s):
        add(attr, p.name)
    for seg in re.split(r"<[^>]*>", s):
        add(seg.replace("{x}", "{x}"), p.name)

PY = ["mandoob/web/app.py", "mandoob/contact.py", "mandoob/reports.py", "mandoob/health.py", "mandoob/xlsx.py",
      "mandoob/prospecting.py", "mandoob/leads.py", "mandoob/companies.py",
      "mandoob/directory.py", "mandoob/discovery/runner.py", "mandoob/auth.py",
      "mandoob/assignment.py", "mandoob/permissions.py"]
for f in PY:
    fp = ROOT / f
    if not fp.exists():
        continue
    tree = ast.parse(fp.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                parts.append(v.value if isinstance(v, ast.Constant) else "{x}")
            add("".join(parts), f)
            for v in node.values:
                v._skip = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and not getattr(node, "_skip", False):
            add(node.value, f)
# أسماء الأنشطة والقطاعات والمناطق والمدن (من البيانات نفسها، دون كلمات البحث)
sys.path.insert(0, str(ROOT))
from mandoob import activities, geo, targeting  # noqa: E402
for d in (activities.SECTORS, activities.ACTIVITIES, targeting.SECTORS):
    for v in d.values():
        add(v.get("label", ""), "labels")
for r in geo.REGIONS.values() if isinstance(geo.REGIONS, dict) else geo.REGIONS:
    for k in ("label", "name", "capital"):
        if isinstance(r, dict) and r.get(k):
            add(r[k], "geo")
for pl in (geo.PLACES.values() if isinstance(geo.PLACES, dict) else geo.PLACES):
    for k in ("label", "name", "city"):
        if isinstance(pl, dict) and pl.get(k):
            add(pl[k], "geo")
keys = {k: v for k, v in keys.items() if len(k) <= 300}
out = ROOT / "mandoob/i18n/keys.json"
json.dump(keys, open(out, "w"), ensure_ascii=False, indent=0)
print(len(keys), "keys", sum(len(k) for k in keys), "chars", file=sys.stderr)
