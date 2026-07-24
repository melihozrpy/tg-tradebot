from fastapi.testclient import TestClient

from app.main import app


def test_dashboard_is_utf8_and_teaches_core_workflow():
    response = TestClient(app).get("/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Karışık sinyal değil" in response.text
    assert "/islemplani THYAO" in response.text
    assert "Hangi kod ne işe yarıyor?" in response.text
    assert "ATR nedir?" in response.text


def test_dashboard_is_responsive_and_has_risk_disclaimer():
    response = TestClient(app).get("/dashboard")
    assert "@media(max-width:850px)" in response.text
    assert "yatırım tavsiyesi üretmez" in response.text
