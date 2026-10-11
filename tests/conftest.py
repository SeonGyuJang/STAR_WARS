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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def sample_pdf():
    from make_sample_pdf import make

    path = Path(_tmp) / "sample.pdf"
    make(str(path))
    return path


@pytest.fixture()
def fresh_db():
    from paperagent import config, db

    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.init_db()
    return db
