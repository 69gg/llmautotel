"""外呼配置的默认禁用、独立凭据、旧数据兼容和请求回读。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from llmautotel.config import RuntimeConfig
from llmautotel.main import create_app
from llmautotel.models import AppSettings
from llmautotel.store import Store
from llmautotel.telephony.settings import PROVIDER_NAMES, AsteriskSettings, TelephonySettings


def test_default_and_legacy_settings_disable_every_phone_provider() -> None:
    for settings in (AppSettings(), AppSettings.model_validate({"sales": {"goal": "目标"}})):
        assert all(not getattr(settings.telephony, name).enabled for name in PROVIDER_NAMES)
        assert all(not settings.public()["telephony"][name]["enabled"] for name in PROVIDER_NAMES)
        assert settings.telephony.public_base_url == ""


async def test_credentials_persist_independently_without_public_leak(tmp_path: Path) -> None:
    settings = AppSettings.model_validate(
        {
            "telephony": {
                "asterisk": {"password": "AST_TEST_SECRET"},
                "freeswitch": {"password": "FS_TEST_SECRET"},
                "aliyun": {
                    "access_key_secret": "ALI_TEST_SECRET",
                    "gateway_token": "ALI_GATEWAY_SECRET",
                },
                "tencent": {"secret_key": "TC_TEST_SECRET", "gateway_token": "TC_GATEWAY_SECRET"},
            }
        }
    )
    store = Store(tmp_path)
    await store.save_settings(settings)
    loaded = await Store(tmp_path).get_settings()
    assert loaded.telephony.asterisk.password.get_secret_value() == "AST_TEST_SECRET"
    assert loaded.telephony.freeswitch.password.get_secret_value() == "FS_TEST_SECRET"
    assert loaded.telephony.aliyun.access_key_secret.get_secret_value() == "ALI_TEST_SECRET"
    assert loaded.telephony.tencent.secret_key.get_secret_value() == "TC_TEST_SECRET"
    public = json.dumps(loaded.public())
    assert "_SECRET" not in public and "********" not in public
    assert loaded.public()["telephony"]["asterisk"]["password_set"] is True
    assert "_SECRET" not in repr(loaded)
    # 旧前端只保存模型设置，不重置电话配置。
    await store.save_settings(AppSettings.model_validate({"llm": {"model": "new"}}))
    assert (await store.get_settings()).telephony.asterisk.password.get_secret_value() == (
        "AST_TEST_SECRET"
    )
    # 配置表单省略凭据时保留；显式 null 仅清除该凭据。
    await store.save_settings(
        AppSettings.model_validate(
            {
                "telephony": {
                    "asterisk": {"caller_id": "01012345678"},
                    "freeswitch": {"password": None},
                }
            }
        )
    )
    loaded = await store.get_settings()
    assert loaded.telephony.asterisk.password.get_secret_value() == "AST_TEST_SECRET"
    assert loaded.telephony.freeswitch.password is None
    assert loaded.telephony.tencent.secret_key.get_secret_value() == "TC_TEST_SECRET"


def test_catalog_and_validation_do_not_expose_secret_values(tmp_path: Path) -> None:
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        result = client.get("/api/telephony/providers")
        assert result.status_code == 200
        assert {item["id"] for item in result.json()} == set(PROVIDER_NAMES)
        assert all(item["enabled"] is False for item in result.json())
        result = client.put(
            "/api/settings",
            json={
                "telephony": {
                    "asterisk": {
                        "ari_url": "http://TEST_SECRET@server/ari",
                        "password": "TEST_SECRET",
                    },
                }
            },
        )
        assert result.status_code == 422 and "TEST_SECRET" not in result.text


@pytest.mark.parametrize(
    "template",
    [
        "PJSIP/{number}/{other}",
        "PJSIP/{number:08}",
        "PJSIP/{number!r}",
        "PJSIP/{number}\ncommand",
        "PJSIP/no-number",
        "Local/{number}",
    ],
)
def test_endpoint_template_accepts_only_documented_single_number_placeholder(template: str) -> None:
    with pytest.raises(ValidationError):
        AsteriskSettings(endpoint_template=template)


def test_cloud_and_media_settings_have_separate_parameters() -> None:
    settings = TelephonySettings.model_validate(
        {
            "asterisk": {
                "ari_url": "http://ast.test/ari",
                "endpoint_template": "PJSIP/{number}@trunk",
            },
            "freeswitch": {"gateway": "supplier-1", "fs_media_host": "127.0.0.1"},
            "aliyun": {"app_id": "app-1"},
            "tencent": {"sdk_app_id": 123},
        }
    )
    assert settings.asterisk.ari_url == "http://ast.test/ari"
    assert settings.freeswitch.gateway == "supplier-1"
    assert settings.aliyun.app_id == "app-1" and settings.tencent.sdk_app_id == 123
