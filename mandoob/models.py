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
    lang: Mapped[Optional[str]] = mapped_column(String(5))  # لغة الواجهة المختارة: ar (الافتراضية) / en
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
    kind: Mapped[Optional[str]] = mapped_column(String(20))  # None = حملة عادية، social = حاوية «مواقع التواصل الاجتماعي» المستقلة
    target_place: Mapped[Optional[str]] = mapped_column(String(200))  # «المكان المستهدف» كما كتبه المدير
    social_sources: Mapped[Optional[list]] = mapped_column(JSON)  # مفاتيح «مواقع التواصل الاجتماعي» المختارة
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
    won_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    # «العملاء المحتملون»: قابلية التواصل والأولوية لسيارة آب (بدليل منشور)
    unified_number: Mapped[Optional[str]] = mapped_column(String(20), index=True)  # الرقم الوطني الموحد (7xxxxxxxxx)
    cr_number: Mapped[Optional[str]] = mapped_column(String(20))
    prospect_on: Mapped[Optional[date]] = mapped_column(Date, index=True)  # يوم دخولها قائمة العملاء المحتملين
    contactable: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    contact_level: Mapped[Optional[str]] = mapped_column(String(20))  # website (موثّق من موقعها) / directory (منشور في دليل)
    channels_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    priority_level: Mapped[Optional[str]] = mapped_column(String(10))  # high / medium / low
    priority_reason: Mapped[Optional[str]] = mapped_column(Text)
    priority_url: Mapped[Optional[str]] = mapped_column(String(500))
    officials_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    official_status: Mapped[Optional[str]] = mapped_column(String(20))  # verified / needs_check / none  # تاريخ تسجيل الصفقة (لأداء المبيعات حسب الفترة)
    offer_sent_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    offer_note: Mapped[Optional[str]] = mapped_column(String(300))
    # حالة التواصل اليدوي — على سجل المنشأة نفسه (تظهر في كل القوائم والحملات وتبقى عند نقل العميل)
    contact_status: Mapped[str] = mapped_column(String(30), default="new")
    contact_status_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    contact_status_by_id: Mapped[Optional[int]] = mapped_column(Integer)
    last_note: Mapped[Optional[str]] = mapped_column(Text)
    last_note_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # «عملاء CRM»: نسخة للقراءة من crm.syarahapp.sa — المفتاح رقم العميل في الـCRM (لا تكرار عند إعادة السحب)
    crm_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    crm_number: Mapped[Optional[str]] = mapped_column(String(80), index=True)  # الرقم الظاهر للعميل في الـCRM
    crm_source: Mapped[Optional[str]] = mapped_column(String(20))       # api / file
    crm_status: Mapped[Optional[str]] = mapped_column(String(120))
    crm_contact_name: Mapped[Optional[str]] = mapped_column(String(200))
    crm_owner_name: Mapped[Optional[str]] = mapped_column(String(200))  # الموظف الذي يعمل على العميل في الـCRM
    crm_notes: Mapped[Optional[str]] = mapped_column(Text)               # الملاحظات المسجلة أمام العميل في الـCRM
    crm_raw: Mapped[Optional[dict]] = mapped_column(JSON)
    crm_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_note_by_id: Mapped[Optional[int]] = mapped_column(Integer)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_attempt_by_id: Mapped[Optional[int]] = mapped_column(Integer)
    attempts_count: Mapped[int] = mapped_column(Integer, default=0)
    reached_dm_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # وصل الفريق إلى المسؤول/صاحب القرار
    reached_dm_by_id: Mapped[Optional[int]] = mapped_column(Integer)
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
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)  # طلب تجريبي — مستبعد من أرقام المبيعات
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
    crm_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)  # مصدره «عملاء CRM»


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
    # SMTP من صفحة «ربط بريد الشركة»: كلمة السر مشفّرة بمفتاح الخادم ولا تُعرض أبدًا
    smtp_host: Mapped[Optional[str]] = mapped_column(String(120))
    smtp_port: Mapped[Optional[int]] = mapped_column(Integer)
    smtp_user: Mapped[Optional[str]] = mapped_column(String(200))
    smtp_password_enc: Mapped[Optional[str]] = mapped_column(Text)


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


class DirectoryRun(Base):
    """تشغيل «اكتشاف عملاء جدد»: مرحلة ١ (أسماء ومدن وأنشطة من الأدلة) ثم مرحلة ٢ (هاتف وبريد من موقع المنشأة)."""
    __tablename__ = "directory_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/running/done/stopped/failed
    sources: Mapped[list] = mapped_column(JSON, default=list)
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # activities, scope, stage1_limit, budget_usd, similar_to
    budget_usd: Mapped[float] = mapped_column(Float, default=5.0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[Optional[int]] = mapped_column(Integer)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    log: Mapped[list] = mapped_column(JSON, default=list)
    heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime)


class LeadStub(Base):
    """منشأة اكتُشفت في المرحلة الأولى (اسم/مدينة/نشاط + رابط الدليل) قبل صرف تكلفة البحث التفصيلي."""
    __tablename__ = "lead_stubs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("directory_runs.id"), index=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(300))
    name_norm: Mapped[str] = mapped_column(String(300), index=True)
    city: Mapped[str] = mapped_column(String(120), default="")
    activity: Mapped[str] = mapped_column(String(200), default="")
    sector_key: Mapped[Optional[str]] = mapped_column(String(60))
    website: Mapped[Optional[str]] = mapped_column(String(300))
    phone_hint: Mapped[Optional[str]] = mapped_column(String(40))
    source_url: Mapped[str] = mapped_column(String(500), default="")  # رابط الدليل/القائمة
    vehicle_hint: Mapped[Optional[str]] = mapped_column(Text)
    # مسار الوصول: عبارة البحث ← أول نتيجة قادت إلى المنشأة ← مرجع الخرائط/الحساب التجاري إن ظهر
    query: Mapped[Optional[str]] = mapped_column(String(300))
    found_url: Mapped[Optional[str]] = mapped_column(String(500))
    maps_url: Mapped[Optional[str]] = mapped_column(String(500))
    social_url: Mapped[Optional[str]] = mapped_column(String(500))
    # مسارات فرص البيع: الدليل الذي رشّح المنشأة (اقتباس حرفي من صفحة قرأها الوكيل، بتاريخه ورابطه)
    signal_kind: Mapped[Optional[str]] = mapped_column(String(30))
    evidence_url: Mapped[Optional[str]] = mapped_column(String(500))
    evidence_date: Mapped[Optional[date]] = mapped_column(Date)
    evidence_quote: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[Optional[str]] = mapped_column(Text)               # سبب الترشيح بجملة للمندوب
    vehicles_count: Mapped[Optional[int]] = mapped_column(Integer)    # فقط إن ذكره المصدر
    contact_dept: Mapped[Optional[str]] = mapped_column(String(200))  # الجهة المقترحة للتواصل
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    # new / duplicate / existing_customer / complete / incomplete / failed / skipped
    duplicate_of: Mapped[Optional[int]] = mapped_column(Integer)
    company_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    enriched_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ProspectRun(Base):
    """دفعة «العملاء المحتملون»: جمع من دليل عام، ثم التحقق من قنوات التواصل في موقع كل منشأة، ثم الترتيب."""
    __tablename__ = "prospect_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/running/done/stopped/failed
    source: Mapped[str] = mapped_column(String(40), default="bluepages")  # قديم (مصدر واحد)
    sources: Mapped[Optional[str]] = mapped_column(Text)  # المصادر بالترتيب، مفصولة بفواصل
    target: Mapped[int] = mapped_column(Integer, default=100)  # عدد المنشآت الجديدة القابلة للتواصل المطلوب
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    log: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime)


class DataPurchase(Base):
    """عيّنة/دفعة بيانات مشتراة من مزوّد (استيراد Excel بلا ربط تقني): للحكم على جدواها قبل شراء دفعة أكبر."""
    __tablename__ = "data_purchases"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(200))
    data_updated_on: Mapped[Optional[date]] = mapped_column(Date)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    file_name: Mapped[str] = mapped_column(String(200), default="")
    license_note: Mapped[str] = mapped_column(String(500), default="")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    queued: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_in_file: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProspectPool(Base):
    """صفوف ملف مزوّد بيانات مرخّص — تُعالج يوميًا بمسار «العملاء المحتملون» (تكرار ← تحقق من الموقع ← أولوية)."""
    __tablename__ = "prospect_pool"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(String(200))
    license_note: Mapped[str] = mapped_column(String(500), default="")  # مصدر الملف وحق الاستخدام
    row: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # random / queued / done
    kind: Mapped[Optional[str]] = mapped_column(String(20), default="provider")  # provider (ملف مرخّص) / random (عملاء عشوائيون)
    result: Mapped[Optional[str]] = mapped_column(String(30))
    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    purchase_id: Mapped[Optional[int]] = mapped_column(ForeignKey("data_purchases.id"), index=True)



class CompanyOfficial(Base):
    """صاحب قرار في منشأة (من صفحات عامة) — سجل واحد لكل شخص في المنشأة، يُحدَّث عند ظهور مصدر جديد."""
    __tablename__ = "company_officials"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_norm: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(String(300), default="")
    relevance: Mapped[str] = mapped_column(String(20), default="other")
    source_type: Mapped[str] = mapped_column(String(20), default="other")
    evidence_url: Mapped[str] = mapped_column(String(500), default="")
    evidence_date: Mapped[str] = mapped_column(String(40), default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # كل المصادر التي ظهر فيها
    status: Mapped[str] = mapped_column(String(20), default="needs_check")  # verified / needs_check
    status_reason: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[Optional[str]] = mapped_column(String(200))   # بريد مهني منشور باسمه
    email_url: Mapped[Optional[str]] = mapped_column(String(500))
    phone: Mapped[Optional[str]] = mapped_column(String(40))    # هاتف مهني منشور باسمه
    phone_url: Mapped[Optional[str]] = mapped_column(String(500))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AdminList(Base):
    """«قائمة عملاء إدارية»: ملف Excel يرفعه المدير من جهازه ويُعرض كما هو (كل الأعمدة)، دون تحقق أو تعديل."""
    __tablename__ = "admin_lists"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    sheet: Mapped[str] = mapped_column(String(100), default="")
    headers: Mapped[list] = mapped_column(JSON, default=list)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(String(500), default="")
    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_to_verify_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AdminListContact(Base):
    """حالة التواصل لكل صف في «قائمة عملاء إدارية» — مشتركة بين الموظفين حتى لا يُتصل بالعميل مرتين."""
    __tablename__ = "admin_list_contacts"
    __table_args__ = (UniqueConstraint("list_id", "row_idx", name="uq_admin_list_row"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("admin_lists.id"), index=True)
    row_idx: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))  # replied / no_answer / wrong_number / do_not_contact
    note: Mapped[str] = mapped_column(String(500), default="")
    by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    by_name: Mapped[str] = mapped_column(String(120), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    history: Mapped[list] = mapped_column(JSON, default=list)  # كل التحديثات السابقة (لا يُمسح شيء)


class DownloadAttempt(Base):
    """محاولة تنزيل مُنعت (سياسة حماية البيانات): رقم التتبّع = المعرّف."""
    __tablename__ = "download_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    user_name: Mapped[str] = mapped_column(String(120), default="")
    user_email: Mapped[str] = mapped_column(String(200), default="")
    portal: Mapped[str] = mapped_column(String(20), default="")
    page: Mapped[str] = mapped_column(String(300), default="")      # الصفحة التي ضُغط منها
    target: Mapped[str] = mapped_column(String(300), default="")    # المسار المطلوب (بلا معاملات)
    label: Mapped[str] = mapped_column(String(200), default="")     # اسم الملف/التقرير
    report_id: Mapped[Optional[str]] = mapped_column(String(40))
    result: Mapped[str] = mapped_column(String(40), default="blocked")
    read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Incident(Base):
    """عطل مسجّل في إحدى الخدمات (بلا أسرار ولا بيانات عملاء)."""
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    service: Mapped[str] = mapped_column(String(20))   # web / db / scheduler
    kind: Mapped[str] = mapped_column(String(40), default="error")
    message: Mapped[str] = mapped_column(String(500), default="")


class AdminListAssignment(Base):
    """إسناد صف من «قائمة عملاء إدارية» لموظف — الموظف يرى ما أُسند إليه فقط."""
    __tablename__ = "admin_list_assignments"
    __table_args__ = (UniqueConstraint("list_id", "row_idx", name="uq_admin_list_assign"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("admin_lists.id"), index=True)
    row_idx: Mapped[int] = mapped_column(Integer)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CampaignSourceFile(Base):
    """ملف Excel مرفوع أمام مصدر من «مواقع التواصل الاجتماعي» في حملة: يُعرض كما هو للمعاينة، ثم يُستورد عند الاعتماد."""
    __tablename__ = "campaign_source_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(30))
    place: Mapped[str] = mapped_column(String(200), default="")
    file_name: Mapped[str] = mapped_column(String(200), default="")
    headers: Mapped[list] = mapped_column(JSON, default=list)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    cols: Mapped[dict] = mapped_column(JSON, default=dict)  # {"name": i, "phone": [i..], "email": i, ...}
    status: Mapped[str] = mapped_column(String(20), default="staged")  # staged / imported
    stats: Mapped[Optional[dict]] = mapped_column(JSON)  # {"new", "duplicate", "failed", "failures": [{row, reason}]}
    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    imported_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class CampaignLead(Base):
    """عميل في حملة ومصدره: سجل عميل واحد (Company) وقد يرتبط بأكثر من مصدر في الحملة نفسها."""
    __tablename__ = "campaign_leads"
    __table_args__ = (UniqueConstraint("campaign_id", "company_id", "source_key", name="uq_campaign_lead_src"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(30), index=True)
    place: Mapped[str] = mapped_column(String(200), default="")
    file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("campaign_source_files.id"))
    row_idx: Mapped[Optional[int]] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CampaignSourceMark(Base):
    """أمام كل عميل في ملف «مواقع التواصل الاجتماعي»: هل تم التواصل معه؟ وهل هو مهتم؟"""
    __tablename__ = "campaign_source_marks"
    __table_args__ = (UniqueConstraint("file_id", "row_idx", name="uq_campaign_source_mark"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("campaign_source_files.id"), index=True)
    row_idx: Mapped[int] = mapped_column(Integer)
    contacted: Mapped[Optional[str]] = mapped_column(String(10))   # yes / no
    interest: Mapped[Optional[str]] = mapped_column(String(20))    # interested / not_interested / later
    by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    by_name: Mapped[str] = mapped_column(String(120), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class KitItem(Base):
    """عنصر في «ملفات طلبات العملاء»: ملف معتمد و/أو قيمة للنسخ (رقم، عنوان، بريد) و/أو رابط، أو نص (دليل/أسئلة)."""
    __tablename__ = "kit_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="file")  # file / number / address / template / app / link / email / phone / text
    title: Mapped[str] = mapped_column(String(200))
    note: Mapped[str] = mapped_column(String(300), default="")      # وصف قصير يظهر للمندوب
    value: Mapped[Optional[str]] = mapped_column(String(500))       # ما يُنسخ: رقم/عنوان/بريد
    url: Mapped[Optional[str]] = mapped_column(String(500))         # رابط (أو رابط الآيفون)
    url2: Mapped[Optional[str]] = mapped_column(String(500))        # رابط الأندرويد
    body: Mapped[Optional[str]] = mapped_column(Text)               # نص الدليل/الخطوات/الأسئلة الشائعة
    file_name: Mapped[Optional[str]] = mapped_column(String(255))
    file_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    expires_on: Mapped[Optional[date]] = mapped_column(Date)
    published: Mapped[bool] = mapped_column(Boolean, default=False)       # معتمد ومنشور للمندوبين
    share_enabled: Mapped[bool] = mapped_column(Boolean, default=True)    # رابط المشاركة العام يعمل
    share_token: Mapped[str] = mapped_column(String(40), unique=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(120), default="")


class KitLog(Base):
    """سجل نشاط «ملفات طلبات العملاء»: تحميل، فتح رابط مشاركة، تسجيل إرسال لعميل."""
    __tablename__ = "kit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(20))  # download / share_open / sent / publish / update
    item_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    item_title: Mapped[str] = mapped_column(String(200), default="")
    version: Mapped[int] = mapped_column(Integer, default=0)
    company_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer)
    user_name: Mapped[str] = mapped_column(String(120), default="")
