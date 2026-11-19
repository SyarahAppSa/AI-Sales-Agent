"""نموذج البيانات. كل الأوقات محفوظة بتوقيت UTC (بدون منطقة زمنية) وتُعرض بتوقيت الرياض."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text,
                        UniqueConstraint, Index)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# ---------------------------------------------------------------- المستخدمون


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    phone: Mapped[Optional[str]] = mapped_column(String(40))
    password_hash: Mapped[str] = mapped_column(String(300))
    # admin = مدير المنصة، manager = مدير المبيعات، rep = مندوب
    role: Mapped[str] = mapped_column(String(20), default="rep")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # قواعد التوزيع: قوائم مفاتيح القطاعات والمدن التي يغطيها المندوب (فارغة = الكل)
    sectors: Mapped[list] = mapped_column(JSON, default=list)
    cities: Mapped[list] = mapped_column(JSON, default=list)
    max_open_leads: Mapped[int] = mapped_column(Integer, default=60)
    calendar_email: Mapped[Optional[str]] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # بوابة الموظفين: الفريق ونطاق الاطلاع والصلاحيات (انظر mandoob/permissions.py)
    team: Mapped[Optional[str]] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(10), default="own")  # own / team / all
    perms: Mapped[dict] = mapped_column(JSON, default=dict)
    profile: Mapped[str] = mapped_column(String(40), default="sales_rep")
    session_version: Mapped[int] = mapped_column(Integer, default=1)  # زيادته تُغلق كل الجلسات المفتوحة
    created_by: Mapped[Optional[int]] = mapped_column(Integer)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class LoginCode(Base):
    """رمز دخول لمرة واحدة (٦ أرقام، ٥ دقائق). يُخزَّن مُجزّأً فقط."""
    __tablename__ = "login_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    portal: Mapped[str] = mapped_column(String(10))  # admin / staff
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AuditLog(Base):
    """السجل الإداري: من أضاف الموظف، ومن غيّر صلاحياته، ومتى."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer)
    actor_name: Mapped[str] = mapped_column(String(120), default="")
    action: Mapped[str] = mapped_column(String(40))
    target_user_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    target_name: Mapped[str] = mapped_column(String(120), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- الحملات والفئات


class Attachment(Base):
    def read_bytes(self) -> bytes:
        if self.data is not None:
            return self.data
        from pathlib import Path
        return Path(self.stored_path).read_bytes()

    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[Optional[str]] = mapped_column(String(500))
    # المحتوى محفوظ في قاعدة البيانات (لا يُعدّل أبدًا بعد الرفع) — يتشاركه الخادم والعامل
    data: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    mime_type: Mapped[str] = mapped_column(String(100), default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Offer(Base):
    """عرض معتمد (خدمة واحدة) يمكن ربطه بحملة أو فئة."""
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True)  # يستخدم لمنع تكرار العرض نفسه
    service_name: Mapped[str] = mapped_column(String(200))
    subject_template: Mapped[str] = mapped_column(String(300))
    value_points: Mapped[str] = mapped_column(Text, default="")  # الفوائد المعتمدة
    approved_body: Mapped[str] = mapped_column(Text, default="")  # نص معتمد (يستخدم إذا لم يتوفر الذكاء الاصطناعي)
    pricing_text: Mapped[str] = mapped_column(Text, default="")
    call_to_action: Mapped[str] = mapped_column(
        String(300), default="هل يناسبكم اتصال تعريفي لمدة عشر دقائق هذا الأسبوع؟")
    attachment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))
    # النطاق الجغرافي للخدمة (مدن/مناطق/مدن صناعية) — فارغ = كل المملكة
    coverage: Mapped[list] = mapped_column(JSON, default=list)
    # الشرائح التي يناسبها العرض — فارغ = كل الشرائح
    segments: Mapped[list] = mapped_column(JSON, default=list)
    audience: Mapped[str] = mapped_column(String(20), default="customer")  # customer / partner
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    attachment: Mapped[Optional[Attachment]] = relationship()


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    request_text: Mapped[str] = mapped_column(Text, default="")  # طلب المدير بالعربية كما كتبه
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft / active / paused
    daily_target: Mapped[int] = mapped_column(Integer, default=400)
    # النطاق الجغرافي بمعرّفات دليل الأماكن (أداة «البحث عن العملاء»): {"all", "regions", "places", "exclude"}
    geo_scope: Mapped[Optional[dict]] = mapped_column(JSON)
    allow_backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusions: Mapped[list] = mapped_column(JSON, default=list)  # أنشطة مستبعدة (وسطاء...)
    exclude_existing_customers: Mapped[bool] = mapped_column(Boolean, default=True)
    # الإرسال
    send_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    send_days: Mapped[list] = mapped_column(JSON, default=lambda: [6, 0, 1, 2, 3])  # Python weekday: الأحد=6
    send_start_hour: Mapped[int] = mapped_column(Integer, default=9)
    send_end_hour: Mapped[int] = mapped_column(Integer, default=16)
    daily_send_cap: Mapped[int] = mapped_column(Integer, default=150)
    test_recipient: Mapped[Optional[str]] = mapped_column(String(200))  # وضع التجربة: كل الرسائل تذهب هنا
    followup_days: Mapped[int] = mapped_column(Integer, default=4)  # 0 = بلا متابعة آلية
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    categories: Mapped[list["Category"]] = relationship(back_populates="campaign",
                                                        cascade="all, delete-orphan",
                                                        order_by="Category.priority")


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    sector_key: Mapped[str] = mapped_column(String(60))
    label: Mapped[str] = mapped_column(String(200))
    cities: Mapped[list] = mapped_column(JSON, default=list)
    keywords_ar: Mapped[list] = mapped_column(JSON, default=list)
    keywords_en: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[int] = mapped_column(Integer, default=1)  # 1 = الأعلى
    priority_signals: Mapped[list] = mapped_column(JSON, default=list)  # مثل: يدير أسطول مركبات
    quota: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_experiment: Mapped[bool] = mapped_column(Boolean, default=False)  # ضمن حصة اختبار الشرائح الجديدة
    cursor: Mapped[int] = mapped_column(Integer, default=0)  # موضع (الموقع × عبارة البحث) التالي — يستأنف منه البحث
    offer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("offers.id"))
    campaign: Mapped[Campaign] = relationship(back_populates="categories")
    offer: Mapped[Optional[Offer]] = relationship()


# ---------------------------------------------------------------- الشركات


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    name_en: Mapped[Optional[str]] = mapped_column(String(300))
    name_norm: Mapped[str] = mapped_column(String(300), index=True)
    activity: Mapped[str] = mapped_column(String(200), default="")
    sector_key: Mapped[Optional[str]] = mapped_column(String(60))
    city: Mapped[str] = mapped_column(String(80), default="")
    website: Mapped[Optional[str]] = mapped_column(String(300))
    domain: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    # complete = مكتملة (هاتف + بريد موثقان)، incomplete = قيد استكمال البيانات، rejected = غير مطابقة
    data_status: Mapped[str] = mapped_column(String(20), default="incomplete")
    fit_reason: Mapped[str] = mapped_column(Text, default="")
    fit_evidence_url: Mapped[Optional[str]] = mapped_column(String(500))
    fit_score: Mapped[float] = mapped_column(Float, default=0.0)
    campaign_id: Mapped[Optional[int]] = mapped_column(ForeignKey("campaigns.id"))
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"))
    first_found_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # تاريخ احتسابها ضمن الهدف
    counted_on: Mapped[Optional[date]] = mapped_column(Date, index=True)  # يوم الاحتساب بتوقيت الرياض
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    times_seen: Mapped[int] = mapped_column(Integer, default=1)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    # new / contacted / replied / interested / meeting / won / lost / do_not_contact
    stage: Mapped[str] = mapped_column(String(30), default="new")
    place_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)  # الموقع الموثّق لأول احتساب
    region_id: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    branches: Mapped[Optional[list]] = mapped_column(JSON)  # فروع موثّقة أخرى [{place_id, city, url}]
    entity_type: Mapped[Optional[str]] = mapped_column(String(30))  # شركة / مؤسسة فردية / جمعية ...
    vehicle_indicator: Mapped[Optional[str]] = mapped_column(String(20))  # confirmed / unconfirmed
    # التواصل اليدوي من الفريق: ما سجّله الموظف بنفسه (من ومتى) — مصدر تقرير العروض في المرحلة الحالية
    contacted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    contacted_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    offer_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    offer_sent_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    offer_note: Mapped[Optional[str]] = mapped_column(String(300))
    # حالة التواصل اليدوي — على سجل المنشأة نفسه (تظهر في كل القوائم والحملات وتبقى عند نقل العميل)
    contact_status: Mapped[str] = mapped_column(String(30), default="new")
    contact_status_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    contact_status_by_id: Mapped[Optional[int]] = mapped_column(Integer)
    last_note: Mapped[Optional[str]] = mapped_column(Text)
    last_note_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_note_by_id: Mapped[Optional[int]] = mapped_column(Integer)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_attempt_by_id: Mapped[Optional[int]] = mapped_column(Integer)
    attempts_count: Mapped[int] = mapped_column(Integer, default=0)
    contact_lock_by_id: Mapped[Optional[int]] = mapped_column(Integer)  # «جارٍ التواصل بواسطة …»
    contact_lock_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    next_followup_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    next_followup_owner_id: Mapped[Optional[int]] = mapped_column(Integer)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    is_existing_customer: Mapped[bool] = mapped_column(Boolean, default=False)
    next_action: Mapped[Optional[str]] = mapped_column(String(300))
    next_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_action: Mapped[Optional[str]] = mapped_column(String(300))
    last_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    deal_value: Mapped[Optional[float]] = mapped_column(Float)
    retry_enrich_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    enrich_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # تشغيل المركبات
    fit_phrase: Mapped[Optional[str]] = mapped_column(String(300))  # عبارة التشغيل التي رُشحت بسببها
    operator_status: Mapped[str] = mapped_column(String(20), default="unknown")  # self / contracted / unknown
    operator_evidence: Mapped[Optional[str]] = mapped_column(Text)
    operator_evidence_url: Mapped[Optional[str]] = mapped_column(String(500))
    decision_unit: Mapped[Optional[str]] = mapped_column(String(120))  # الجهة المقترحة لاتخاذ القرار
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"))  # المقر الرئيسي إن كانت فرعًا
    lead_source: Mapped[str] = mapped_column(String(80), default="discovery")  # discovery / website_form / campaign:x / partner:x / carrier_of:x
    vehicles_estimate: Mapped[Optional[int]] = mapped_column(Integer)
    signal_boost: Mapped[float] = mapped_column(Float, default=0.0)
    # «بيانات تجريبية»: مستبعدة من كل إرسال واتصال ومتابعة آلية ومن هدف الـ٤٠٠ اليومي
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    contacts: Mapped[list["Contact"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    sources: Mapped[list["CompanySource"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    owner: Mapped[Optional[User]] = relationship(foreign_keys=[owner_id])
    offer_sent_by: Mapped[Optional[User]] = relationship(foreign_keys=[offer_sent_by_id])
    contacted_by: Mapped[Optional[User]] = relationship(foreign_keys=[contacted_by_id])
    campaign: Mapped[Optional[Campaign]] = relationship()
    category: Mapped[Optional[Category]] = relationship()

    def primary(self, kind: str) -> Optional["Contact"]:
        """الهاتف/البريد الموثّق الذي يخص المنشأة نفسها (يُستبعد بريد مطوّر الموقع أو هاتف منشأة أخرى)."""
        from .companies import contact_belongs
        cs = [c for c in self.contacts if c.kind == kind and c.verified and contact_belongs(self, c)]
        cs.sort(key=lambda c: (not c.preferred, c.id or 0))
        return cs[0] if cs else None


class Contact(Base):
    """هاتف أو بريد منشور، مع رابط الصفحة التي نُشر فيها. لا يُقبل أي بيان بلا مصدر."""
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_contact_kind_value"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    kind: Mapped[str] = mapped_column(String(10))  # phone / email
    value: Mapped[str] = mapped_column(String(200))  # الهاتف بصيغة E.164، البريد بأحرف صغيرة
    raw_value: Mapped[str] = mapped_column(String(200), default="")
    source_url: Mapped[str] = mapped_column(String(500))
    source_type: Mapped[str] = mapped_column(String(40), default="website")
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    preferred: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[Optional[str]] = mapped_column(String(80))  # مثل: المبيعات، المشتريات
    found_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    company: Mapped[Company] = relationship(back_populates="contacts")


class CompanySource(Base):
    __tablename__ = "company_sources"
    __table_args__ = (UniqueConstraint("company_id", "source_type", "url", name="uq_company_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    source_type: Mapped[str] = mapped_column(String(40))  # google_places / web_search / ai_search / website / directory
    url: Mapped[str] = mapped_column(String(500), default="")
    external_id: Mapped[Optional[str]] = mapped_column(String(200))  # مثل place_id
    snippet: Mapped[str] = mapped_column(Text, default="")
    found_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("discovery_runs.id"))
    company: Mapped[Company] = relationship(back_populates="sources")


class CompanyRelation(Base):
    """علاقات بين الشركات: الناقل الذي يشغّل توزيع شركة، أو الفرع والمقر الرئيسي."""
    __tablename__ = "company_relations"
    __table_args__ = (UniqueConstraint("company_id", "related_id", "kind", name="uq_company_relation"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    related_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # carrier (related يشغّل نقل company) / branch (company فرع لـ related)
    evidence: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    company: Mapped["Company"] = relationship(foreign_keys=[company_id])
    related: Mapped["Company"] = relationship(foreign_keys=[related_id])


class CompanySignal(Base):
    """إشارة تغيّر الاحتياج: افتتاح مستودع، توسع لمدينة، عقد تشغيل، توظيف سائقين/مشرفي أسطول."""
    __tablename__ = "company_signals"
    __table_args__ = (UniqueConstraint("company_id", "kind", "url", name="uq_company_signal"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(Text, default="")
    announced_on: Mapped[Optional[date]] = mapped_column(Date)
    url: Mapped[str] = mapped_column(String(500), default="")
    found_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InboundLead(Base):
    """طلب «اطلب عرضًا لأسطولك» من الصفحة العامة."""
    __tablename__ = "inbound_leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"))
    company_name: Mapped[str] = mapped_column(String(300))
    city: Mapped[str] = mapped_column(String(120))
    vehicles: Mapped[Optional[int]] = mapped_column(Integer)
    activity: Mapped[Optional[str]] = mapped_column(String(60))
    offer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("offers.id"))
    service_text: Mapped[str] = mapped_column(String(200), default="")
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[Optional[str]] = mapped_column(String(40))
    email: Mapped[Optional[str]] = mapped_column(String(200))
    preferred_channel: Mapped[str] = mapped_column(String(20), default="phone")  # phone / email / whatsapp
    source: Mapped[str] = mapped_column(String(80), default="website")
    notes: Mapped[str] = mapped_column(Text, default="")
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExistingCustomer(Base):
    """عملاء سيارة آب الحاليون (للاستبعاد)."""
    __tablename__ = "existing_customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    name_norm: Mapped[str] = mapped_column(String(300), index=True)
    domain: Mapped[Optional[str]] = mapped_column(String(200))
    phone: Mapped[Optional[str]] = mapped_column(String(40))
    email: Mapped[Optional[str]] = mapped_column(String(200))


class Suppression(Base):
    """قائمة إيقاف التواصل. تُراجع قبل أي بريد أو مكالمة."""
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("kind", "value", "channel", name="uq_suppression"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # email / domain / phone / company
    value: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(10), default="all")  # all / email / call
    reason: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(60), default="manual")
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Consent(Base):
    """سجل الأساس النظامي/الموافقة للتواصل لكل قناة."""
    __tablename__ = "consents"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    channel: Mapped[str] = mapped_column(String(10))  # email / call
    # published_business_contact = بيانات أعمال منشورة للتواصل التجاري (للبريد)
    # explicit = موافقة صريحة (مطلوبة للمكالمات الآلية)
    basis: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[str] = mapped_column(Text, default="")
    granted: Mapped[bool] = mapped_column(Boolean, default=True)
    recorded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- التشغيل اليومي


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date] = mapped_column(Date, index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running / done / partial / failed
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    target: Mapped[int] = mapped_column(Integer, default=0)
    complete_count: Mapped[int] = mapped_column(Integer, default=0)
    incomplete_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    # نقطة التوقف لكل فئة ومصدر: {"cat:3": {"google_places": {"query_idx": 4, "page_token": "..."}}}
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    per_category: Mapped[dict] = mapped_column(JSON, default=dict)
    per_source: Mapped[dict] = mapped_column(JSON, default=dict)
    per_region: Mapped[dict] = mapped_column(JSON, default=dict)  # {رمز المنطقة: {searched, dup, out, last}}
    source_errors: Mapped[dict] = mapped_column(JSON, default=dict)
    shortfall: Mapped[int] = mapped_column(Integer, default=0)
    shortfall_reason: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSON, default=list)


class MailAccount(Base):
    __tablename__ = "mail_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))  # microsoft365 / google / sandbox
    sender_address: Mapped[str] = mapped_column(String(200))
    sender_name: Mapped[str] = mapped_column(String(120), default="سيارة آب")
    signature_html: Mapped[str] = mapped_column(Text, default="")
    reply_mailbox: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # أول إرسال ناجح فعلًا
    sync_cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbound_dedupe"),
        Index("ix_outbound_company", "company_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    campaign_id: Mapped[Optional[int]] = mapped_column(ForeignKey("campaigns.id"))
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"))
    offer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("offers.id"))
    kind: Mapped[str] = mapped_column(String(20), default="offer")  # offer / followup / confirmation
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("outbound_messages.id"))
    # company:<id>|offer:<key>  يمنع إرسال العرض نفسه مرتين لنفس الشركة مهما تعددت الحملات أو أعيد التشغيل
    dedupe_key: Mapped[str] = mapped_column(String(200))
    mail_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("mail_accounts.id"))
    from_address: Mapped[str] = mapped_column(String(200), default="")
    to_address: Mapped[str] = mapped_column(String(200))  # المستلم الفعلي
    intended_to: Mapped[str] = mapped_column(String(200), default="")  # بريد الشركة (يختلف في وضع التجربة)
    subject: Mapped[str] = mapped_column(String(300))
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text)
    attachment_name: Mapped[Optional[str]] = mapped_column(String(255))
    attachment_path: Mapped[Optional[str]] = mapped_column(String(500))  # (قديم) مسار محلي
    attachment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))  # الملف كما أُرسل (غير قابل للتعديل)
    attachment_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    # queued / sending / sent / delivered / bounced / failed / skipped
    status: Mapped[str] = mapped_column(String(20), default="queued")
    status_detail: Mapped[str] = mapped_column(Text, default="")
    bounce_reason: Mapped[Optional[str]] = mapped_column(Text)
    internet_message_id: Mapped[Optional[str]] = mapped_column(String(300), index=True)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(300))
    thread_id: Mapped[Optional[str]] = mapped_column(String(300), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    bounced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    followups_stopped: Mapped[bool] = mapped_column(Boolean, default=False)
    company: Mapped[Company] = relationship()
    campaign: Mapped[Optional[Campaign]] = relationship()
    offer: Mapped[Optional[Offer]] = relationship()


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    __table_args__ = (UniqueConstraint("provider_message_id", name="uq_inbound_provider_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_message_id: Mapped[str] = mapped_column(String(300))
    internet_message_id: Mapped[Optional[str]] = mapped_column(String(300))
    in_reply_to: Mapped[Optional[str]] = mapped_column(String(300))
    thread_id: Mapped[Optional[str]] = mapped_column(String(300))
    from_address: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300), default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # reply / bounce / delivery_receipt / auto_reply / unrelated
    message_type: Mapped[str] = mapped_column(String(20), default="reply")
    outbound_id: Mapped[Optional[int]] = mapped_column(ForeignKey("outbound_messages.id"))
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"))
    # interested / callback_request / details_request / quote_request / meeting_request /
    # not_interested / unsubscribe / out_of_office / other
    intent: Mapped[Optional[str]] = mapped_column(String(30))
    intent_label: Mapped[Optional[str]] = mapped_column(String(100))
    requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # موعد المتابعة المطلوب (UTC)
    requested_window: Mapped[Optional[str]] = mapped_column(String(120))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="call")  # call / email / meeting / quote / review
    title: Mapped[str] = mapped_column(String(300))
    details: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime] = mapped_column(DateTime)
    window_label: Mapped[Optional[str]] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open / done / cancelled
    source_inbound_id: Mapped[Optional[int]] = mapped_column(ForeignKey("inbound_messages.id"))
    source_call_id: Mapped[Optional[int]] = mapped_column(ForeignKey("calls.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    company: Mapped[Company] = relationship()
    owner: Mapped[Optional[User]] = relationship()


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    emailed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Call(Base):
    __tablename__ = "calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    direction: Mapped[str] = mapped_column(String(10), default="outbound")
    to_number: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(30), default="elevenlabs")
    provider_call_id: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    # requested / ringing / answered / no_answer / busy / failed / completed
    status: Mapped[str] = mapped_column(String(20), default="requested")
    answered: Mapped[bool] = mapped_column(Boolean, default=False)
    # interested / meeting_booked / transfer_to_rep / callback / not_interested / do_not_call / voicemail
    outcome: Mapped[Optional[str]] = mapped_column(String(30))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    transcript: Mapped[Optional[str]] = mapped_column(Text)
    next_step: Mapped[Optional[str]] = mapped_column(String(300))
    next_step_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    company: Mapped[Company] = relationship()


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    attendee_email: Mapped[Optional[str]] = mapped_column(String(200))
    calendar_event_id: Mapped[Optional[str]] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="booked")  # booked / done / cancelled
    confirmation_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("outbound_messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    company: Mapped[Company] = relationship()
    owner: Mapped[User] = relationship()


class Activity(Base):
    """السجل الزمني الموحد للشركة: اكتشاف، عروض، ردود، مكالمات، اجتماعات، ملاحظات."""
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    details: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    ref_type: Mapped[Optional[str]] = mapped_column(String(30))
    ref_id: Mapped[Optional[int]] = mapped_column(Integer)


class CostEntry(Base):
    __tablename__ = "cost_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    service: Mapped[str] = mapped_column(String(40))  # google_places / brave / claude / voice / email
    units: Mapped[float] = mapped_column(Float, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    campaign_id: Mapped[Optional[int]] = mapped_column(ForeignKey("campaigns.id"))
    note: Mapped[str] = mapped_column(String(200), default="")
    at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=utcnow, index=True)  # لقياس تكلفة كل دفعة


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class JobLock(Base):
    """قفل بسيط لمنع تشغيل المهمة نفسها مرتين بالتوازي."""
    __tablename__ = "job_locks"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    locked_until: Mapped[datetime] = mapped_column(DateTime)
    holder: Mapped[str] = mapped_column(String(120), default="")


class ContactLog(Base):
    """سجل التواصل اليدوي لكل منشأة: لا يُحذف ولا يُستبدل — كل تحديث سطر جديد باسم الموظف ووقته."""
    __tablename__ = "contact_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    user_name: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(20), default="update")  # start / update / release
    status: Mapped[str] = mapped_column(String(30))
    note: Mapped[Optional[str]] = mapped_column(Text)
    next_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    next_owner_name: Mapped[Optional[str]] = mapped_column(String(120))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
