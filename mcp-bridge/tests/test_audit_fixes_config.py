"""Audit fixes — config startup hardening (warn-don't-crash on bad config).

CONFIG-01: malformed SHELDON_SERVER_IP_MAP (bad JSON) must NOT raise at load —
it warns and falls back to the empty map.
CONFIG-02: malformed numeric SHELDON_* env vars (bare int()/float() before) must
NOT raise — each warns and falls back to its documented default.

load_config() reads config.json only if it exists; these tests run with no
config file and a chdir to a tmp dir so the env vars are the sole source.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheldon_bridge.config import load_config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Run where there is no config.json so SHELDON_* env vars are the only input.
    monkeypatch.chdir(tmp_path)
    # Clear every SHELDON_* var so a host/CI value can't leak in.
    for k in list(__import__("os").environ):
        if k.startswith("SHELDON_"):
            monkeypatch.delenv(k, raising=False)


def test_malformed_server_ip_map_does_not_raise(monkeypatch):
    monkeypatch.setenv("SHELDON_SERVER_IP_MAP", "{not valid json")
    cfg = load_config()  # must not raise
    assert cfg.server_ip_map == {}


def test_valid_server_ip_map_still_parses(monkeypatch):
    monkeypatch.setenv("SHELDON_SERVER_IP_MAP", '{"1.2.3.4": {"server_id": "s1"}}')
    cfg = load_config()
    assert cfg.server_ip_map == {"1.2.3.4": {"server_id": "s1"}}


def test_malformed_numeric_env_vars_do_not_raise(monkeypatch):
    # Bad values for every guarded numeric env var.
    monkeypatch.setenv("SHELDON_WS_PORT", "not-a-port")
    monkeypatch.setenv("SHELDON_MAX_TOKENS", "lots")
    monkeypatch.setenv("SHELDON_TEMPERATURE", "warm")
    monkeypatch.setenv("SHELDON_LLM_TIMEOUT", "soon")
    monkeypatch.setenv("SHELDON_LLM_RETRIES", "many")
    monkeypatch.setenv("SHELDON_MAX_TOOL_ITERATIONS", "")  # empty -> default too
    monkeypatch.setenv("SHELDON_TOKEN_BUDGET", "14k")

    cfg = load_config()  # must not raise

    # Each falls back to its documented default.
    assert cfg.websocket_port == 8443
    assert cfg.llm.max_tokens == 4096
    assert cfg.llm.temperature == 0.7
    assert cfg.llm.timeout == 60
    assert cfg.llm.num_retries == 2
    assert cfg.max_tool_iterations == 25
    assert cfg.context_token_budget == 14000


def test_valid_numeric_env_vars_still_parse(monkeypatch):
    monkeypatch.setenv("SHELDON_WS_PORT", "9001")
    monkeypatch.setenv("SHELDON_MAX_TOKENS", "2048")
    monkeypatch.setenv("SHELDON_TEMPERATURE", "0.2")
    cfg = load_config()
    assert cfg.websocket_port == 9001
    assert cfg.llm.max_tokens == 2048
    assert cfg.llm.temperature == 0.2
