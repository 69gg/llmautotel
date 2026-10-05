from __future__ import annotations

import json
import traceback

import pytest
from aiortc import RTCIceServer

from llmautotel.config import RuntimeConfig, parse_ice_servers


def test_default_ice_configuration_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLMAUTOTEL_ICE_SERVERS", raising=False)
    assert RuntimeConfig.from_environment().ice_servers == []


def test_environment_parses_independent_stun_and_authenticated_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servers = [
        {"urls": "stun:stun.example.test:3478"},
        {
            "urls": ["turn:relay.example.test:3478?transport=tcp"],
            "username": "test-user",
            "credential": "TURN_TEST_SECRET",
        },
    ]
    monkeypatch.setenv("LLMAUTOTEL_ICE_SERVERS", json.dumps(servers))
    config = RuntimeConfig.from_environment()
    assert config.ice_servers == [RTCIceServer(**server) for server in servers]
    assert "TURN_TEST_SECRET" not in repr(config)


@pytest.mark.parametrize(
    "value",
    [
        "not-json TURN_TEST_SECRET",
        '{"credential":"TURN_TEST_SECRET"}',
        '[{"urls":42,"credential":"TURN_TEST_SECRET"}]',
        '[{"urls":[],"credential":"TURN_TEST_SECRET"}]',
        '[{"urls":"https://TURN_TEST_SECRET.example.test"}]',
        '[{"urls":"turn:relay.test?transport=sctp","username":"user",'
        '"credential":"TURN_TEST_SECRET"}]',
        '[{"urls":"turns:relay.test?transport=udp","username":"user",'
        '"credential":"TURN_TEST_SECRET"}]',
        '[{"urls":"turn:relay.test","username":"user"}]',
        '[{"urls":"turn:relay.test","username":"user",'
        '"credential":"TURN_TEST_SECRET","credentialType":"oauth"}]',
        '[{"urls":"stuns:relay.test","credential":"TURN_TEST_SECRET"}]',
    ],
)
def test_invalid_ice_errors_do_not_echo_credentials(value: str) -> None:
    with pytest.raises(ValueError, match="LLMAUTOTEL_ICE_SERVERS") as error:
        parse_ice_servers(value)
    rendered = "".join(traceback.format_exception(error.value))
    assert "TURN_TEST_SECRET" not in rendered


def test_secure_turn_tcp_is_supported() -> None:
    servers = parse_ice_servers(
        '[{"urls":"turns:relay.test:5349?transport=tcp",'
        '"username":"user","credential":"TURN_TEST_SECRET"}]'
    )
    assert servers[0].urls == "turns:relay.test:5349?transport=tcp"
