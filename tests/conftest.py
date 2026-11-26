import pytest

from ielts_study import usage


@pytest.fixture(autouse=True)
def _usage_file(tmp_path, monkeypatch):
    """API usage from fake requests goes to a temporary file, never to data/api_usage.json."""
    monkeypatch.setattr(usage, "PATH", tmp_path / "api_usage.json")


@pytest.fixture(autouse=True)
def _standard_offline_engine(monkeypatch):
    """Tests run the Argos path even on a Mac that has the enhanced model installed."""
    from ielts_study import local_mt

    monkeypatch.setattr(local_mt, "ready", lambda: False)
