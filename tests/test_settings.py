from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from llmautotel.config import RuntimeConfig
from llmautotel.main import create_app
from llmautotel.models import AppSettings
from llmautotel.store import Store


def test_settings_persist_without_exposing_credentials(tmp_path: Path) -> None:
    runtime = RuntimeConfig(data_dir=tmp_path, frontend_dir=tmp_path / "no-frontend")
    payload: dict[str, Any] = AppSettings().model_dump(mode="json")
    payload["sales"]["goal"] = "介绍订阅服务"
    for stage in ("asr", "llm", "tts"):
        payload[stage].update(
            base_url=f"http://{stage}.test/v1", model=f"{stage}-model", api_key=f"secret-{stage}"
        )
    with TestClient(create_app(runtime)) as client:
        result = client.put("/api/settings", json=payload)
        assert result.status_code == 200
        assert "secret-" not in result.text
        assert all(result.json()[stage]["api_key_set"] for stage in ("asr", "llm", "tts"))
        public = result.json()
        for stage in ("asr", "llm", "tts"):
            public[stage].pop("api_key_set")
        assert client.put("/api/settings", json=public).status_code == 200
    with TestClient(create_app(runtime)) as client:
        saved = client.get("/api/settings").json()
        assert saved["sales"]["goal"] == "介绍订阅服务"
        assert saved["asr"]["api_key_set"]
        public["asr"]["api_key"] = None
        result = client.put("/api/settings", json=public).json()
        assert not result["asr"]["api_key_set"]
        assert result["llm"]["api_key_set"]
    assert (tmp_path / "app.sqlite3").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_validation_never_echoes_secret_input(tmp_path: Path) -> None:
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        response = client.put("/api/settings", json={"asr": {"api_key": {"secret": "LEAK"}}})
        assert response.status_code == 422
        assert "LEAK" not in response.text
        assert "input" not in response.json()["detail"][0]
        response = client.put("/api/settings", json={"asr": {"base_url": "https://key@test/v1"}})
        assert response.status_code == 422
        assert "https://key@" not in response.text
        response = client.put("/api/settings", json={"asr": {"language": "invalid-language"}})
        assert response.status_code == 422
        assert "invalid-language" not in response.text


async def test_settings_are_loaded_as_secrets(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await store.save_settings(AppSettings.model_validate({"tts": {"api_key": "tts-secret"}}))
    settings = await Store(tmp_path).get_settings()
    assert settings.tts.api_key is not None
    assert settings.tts.api_key.get_secret_value() == "tts-secret"
    assert "tts-secret" not in repr(settings)
