from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


def _normalize_url(url: str) -> str:
    # Render/Heroku يعطيان postgres:// بينما SQLAlchemy يتوقع postgresql://
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


DATABASE_URL = _normalize_url(settings.database_url)
_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
    future=True,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - بيئة محلية
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    from . import models  # noqa: F401  تسجيل الجداول
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """ترحيل بسيط: يضيف الأعمدة الجديدة إلى الجداول الموجودة (إضافة فقط، لا حذف ولا تعديل)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    v = col.default.arg
                    if isinstance(v, bool):
                        default = f" DEFAULT {'TRUE' if v else 'FALSE'}" if not _is_sqlite else f" DEFAULT {int(v)}"
                    elif isinstance(v, (int, float)):
                        default = f" DEFAULT {v}"
                    elif isinstance(v, str):
                        default = " DEFAULT '" + v.replace("'", "''") + "'"
                # IF NOT EXISTS: الموقع والعامل قد يبدآن معًا بعد النشر فيحاول كلاهما إضافة العمود نفسه
                ine = "" if _is_sqlite else "IF NOT EXISTS "
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {ine}{col.name} {ddl_type}{default}'))


# ---------------------------------------------------------------- حماية أطوال النصوص
# نص أطول من عموده (مثل «النشاط» يكتبه البحث الآلي جملةً طويلة) كان يُفشل الإدراج كله على Postgres
# (StringDataRightTruncation) فتسقط دفعة الاكتشاف بالكامل. يُقصّ النص إلى طول العمود قبل الكتابة.
from sqlalchemy import String as _String, event as _event  # noqa: E402


def _clip_strings(mapper, connection, target):
    for col in mapper.columns:
        n = getattr(col.type, "length", None)
        if not n or not isinstance(col.type, _String):
            continue
        key = mapper.get_property_by_column(col).key
        v = target.__dict__.get(key)
        if isinstance(v, str) and len(v) > n:
            setattr(target, key, v[: n - 1] + "…")


_event.listen(Base, "before_insert", _clip_strings, propagate=True)
_event.listen(Base, "before_update", _clip_strings, propagate=True)
