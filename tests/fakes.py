"""مصادر وزاحف وهميان للاختبار (لا شبكة). البيانات أسماء خيالية بنطاقات .example."""
from mandoob.discovery.base import Candidate, SourceError
from mandoob.discovery.crawler import Crawler


def site(title, desc, phone=None, email=None, contact_link=True, extra=""):
    contact = '<a href="/contact-us">اتصل بنا</a>' if contact_link else ""
    return (f"<html><head><title>{title}</title><meta name='description' content='{desc}'></head><body>"
            f"<h1>{title}</h1><p>{desc}</p>{contact}{extra}</body></html>"), \
           ("<html><body><h2>تواصل معنا</h2>"
            + (f"<p>الهاتف: {phone}</p>" if phone else "")
            + (f"<a href='mailto:{email}'>{email}</a>" if email else "") + "</body></html>")


SITES = {}


def add_site(domain, title, desc, phone=None, email=None):
    home, contact = site(title, desc, phone, email)
    SITES[f"https://{domain}"] = home
    SITES[f"https://{domain}/contact-us"] = contact


class FakeCrawler(Crawler):
    def __init__(self):
        super().__init__(http=None, max_pages=4)
        self.fetched = []

    def _allowed(self, url):
        return True

    def fetch(self, url):
        self.fetched.append(url)
        return SITES.get(url.rstrip("/")) or SITES.get(url)


class ListSource:
    def __init__(self, name, cands):
        self.name, self.cands = name, cands

    def search(self, query, city, state):
        if state.get("done"):
            return
        start = state.get("idx", 0)
        for i, c in enumerate(self.cands[start:], start):
            state["idx"] = i + 1
            yield c
        state["done"] = True


class BrokenSource:
    name = "web_search"

    def search(self, query, city, state):
        raise SourceError("انتهت حصة الاستعلامات (429)")
        yield  # pragma: no cover


def cand(name, domain, source="google_places", place_id=None):
    return Candidate(name=name, source_type=source, source_url=f"https://maps.example/{place_id or domain}",
                     external_id=place_id, city="الرياض", activity="تأجير السيارات",
                     website=f"https://{domain}")
