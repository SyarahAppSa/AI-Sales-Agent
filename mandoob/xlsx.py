"""تنزيل Excel لكل القوائم والتقارير: ورقة RTL بعناوين واضحة، وكل القيم نصوص لا صيغ (بيانات المواقع الخارجية)."""
from __future__ import annotations

import io

from .timeutil import fmt

CONTACT_HEADERS = ["المندوب المسؤول", "حالة التواصل", "آخر ملاحظة", "كاتب الملاحظة", "وقت الملاحظة",
                   "تاريخ آخر محاولة", "صاحب آخر محاولة", "عدد المحاولات", "موعد المتابعة القادمة", "مسؤول المتابعة"]


def contact_cells(s, c) -> list:
    """أعمدة التواصل الموحدة في كل تقارير العملاء."""
    from .contact import summary
    x = summary(s, c)
    return [c.owner.name if c.owner else "غير مسند", x["label"], x["note"], x["note_by"],
            fmt(x["note_at"]) if x["note_at"] else "", fmt(x["attempt_at"]) if x["attempt_at"] else "",
            x["attempt_by"], x["attempts"], fmt(x["next_at"]) if x["next_at"] else "", x["next_owner"]]


def _safe(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    v = str(v)
    return v


def workbook(sheets: list[tuple[str, list[str], list[list]]], title_rows: dict | None = None) -> bytes:
    """sheets: [(اسم الورقة، العناوين، الصفوف)]. title_rows: {اسم الورقة: [سطر تعريفي]} يُكتب أعلى الورقة."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    wb.properties.title, wb.properties.creator = "المندوب الذكي لسيارة اب", "المندوب الذكي لسيارة اب"
    wb.remove(wb.active)
    for name, headers, rows in sheets:
        ws = wb.create_sheet(name[:31])
        ws.sheet_view.rightToLeft = True
        start = 1
        for line in (title_rows or {}).get(name, []):
            ws.append([_safe(x) for x in line])
            start += 1
        if (title_rows or {}).get(name):
            ws.append([])
            start += 1
        ws.append(headers)
        for cell in ws[start]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="6941A5")
        for r in rows:
            ws.append([_safe(x) for x in r])
        for col in ws.columns:
            width = max((len(str(c.value or "")) for c in col[:200]), default=8)
            ws.column_dimensions[col[0].column_letter].width = max(10, min(60, width + 2))
        for row in ws.iter_rows(min_row=start + 1):
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"  # نص لا صيغة، ولو بدأ بـ =
                cell.alignment = Alignment(wrap_text=True, vertical="top", horizontal="right")
    buf = io.BytesIO()
    from .i18n import localize_workbook
    localize_workbook(wb)
    wb.save(buf)
    return buf.getvalue()


def response(data: bytes, filename: str):
    from fastapi import Response
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={filename}", "Cache-Control": "no-store"})
