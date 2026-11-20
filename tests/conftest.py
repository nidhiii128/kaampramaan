from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.ai import AI, BoardInfo
from app.config import Settings
from app.db import Database
from app.main import create_app

TODAY = date(2026, 11, 13)


def make_settings(tmp_path, **over):
    base = dict(
        db_path=str(tmp_path / "kp.db"), media_dir=str(tmp_path / "media"),
        gemini_api_key="", gemini_model="test", gemini_fallback_models=(), wa_token="", wa_phone_number_id="",
        wa_verify_token="verify-me", wa_app_secret="", wa_api_version="v21.0",
        public_base_url="http://testserver", admin_user="admin", admin_password="secret",
    )
    base.update(over)
    return Settings(**base)


class BoardReadingAI(AI):
    """Offline AI that pretends Gemini read a PWD site board."""
    @property
    def enabled(self):
        return True

    def read_board(self, image, mime="image/jpeg"):
        return BoardInfo(is_site_board=True, project="Road widening, Sector 62",
                         principal_employer="PWD Delhi", employer_type="government",
                         contractor="XYZ Builders", location="Delhi")


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "kp.db"))


@pytest.fixture
def client(settings):
    app = create_app(settings, ai=BoardReadingAI(), today_fn=lambda: TODAY)
    c = TestClient(app)
    c.auth = ("admin", "secret")
    return c
