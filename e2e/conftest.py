import os
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = tempfile.TemporaryDirectory(prefix="lofter-dev-e2e-")
os.environ["ASTRBOT_ROOT"] = RUNTIME.name
sys.path.insert(0, RUNTIME.name)


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="Also test the real LOFTER DWR service")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        if not os.getenv("LOFTER_COOKIE") or not os.getenv("LOFTER_TAG"):
            raise pytest.UsageError("--live requires LOFTER_COOKIE and LOFTER_TAG in .env.test")
        return
    for item in items:
        if "live" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="Use --live with LOFTER_COOKIE and LOFTER_TAG"))


@pytest.fixture(scope="session")
async def runtime():
    from harness import Runtime

    app = Runtime(ROOT, Path(RUNTIME.name))
    try:
        await app.start()
        yield app
    finally:
        await app.close()


def pytest_unconfigure(config):
    RUNTIME.cleanup()
