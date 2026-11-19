import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="mandoob-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["ANTHROPIC_API_KEY"] = ""          # الاختبارات حتمية بدون ذكاء اصطناعي
os.environ["ADMIN_EMAIL"] = "admin@sayaraup.test"
os.environ["ADMIN_PASSWORD"] = "admin-pass-123"
