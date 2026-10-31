import pytest
from fastapi.testclient import TestClient

from ews import api
from ews.engine import Engine
from ews.synth import generate, write_tables


@pytest.fixture(scope="session")
def synthetic_dir(tmp_path_factory):
    """Self-contained: generate the seeded dataset so tests don't depend on `make data`."""
    return write_tables(generate(seed=42), tmp_path_factory.mktemp("synthetic"))


@pytest.fixture()
def engine(synthetic_dir):
    return Engine(synthetic_dir)  # fresh per test so POSTs don't leak


@pytest.fixture()
def client(engine):
    api.app.dependency_overrides[api.get_engine] = lambda: engine
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()
