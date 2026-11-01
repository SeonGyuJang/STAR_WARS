import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolated data dir for every test session (set before paperagent.config loads).
_tmp = tempfile.mkdtemp(prefix="paperagent-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["ADMIN_PASSWORD"] = ""
# The Claude client is mocked in tests; a non-empty key just means "configured".
os.environ["ANTHROPIC_API_KEY"] = "test-key"
# Run the suite against Postgres (as on Heroku) with e.g.
#   TEST_DATABASE_URL=postgresql://app@127.0.0.1:5432/starwars pytest tests
# That also switches figures/cards to the database blob storage.
os.environ.pop("DATABASE_URL", None)
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Collectors never reach the internet in tests (unmocked calls fail at once
    instead of retrying with sleeps). Tests mock what they need."""
    import requests

    from paperagent import collectors
    from paperagent.collectors import arxiv, tech

    def offline(*a, **k):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(collectors._session, "request", offline)
    monkeypatch.setattr(collectors.time, "sleep", lambda s: None)
    monkeypatch.setattr(arxiv, "REQUEST_GAP_SECONDS", 0)
    monkeypatch.setattr(arxiv, "_busy_until", 0.0)
    monkeypatch.setattr(tech, "render_page", lambda url: None)  # no headless browser for article pages


@pytest.fixture(scope="session")
def sample_pdf():
    from paperagent import demo

    return demo.make_pdf(Path(_tmp) / "sample.pdf")


@pytest.fixture()
def fresh_db():
    from paperagent import config, db

    if os.environ.get("DATABASE_URL"):
        db.metadata.drop_all(db.engine())
    elif config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.init_db()
    return db
