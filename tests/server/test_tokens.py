"""AttachTokenRegistry: single-use, TTL-bounded tokens."""

from __future__ import annotations

import time

from metis.server.tokens import AttachTokenRegistry


def test_mint_and_consume_happy_path():
    reg = AttachTokenRegistry(ttl_seconds=60)
    token, expires_at = reg.mint("sess_1")
    assert token.startswith("atk_")
    assert expires_at > time.time()
    assert reg.consume(token, session_id="sess_1") is True


def test_token_is_single_use():
    reg = AttachTokenRegistry()
    token, _ = reg.mint("sess_1")
    assert reg.consume(token, session_id="sess_1") is True
    assert reg.consume(token, session_id="sess_1") is False


def test_token_scoped_to_session():
    reg = AttachTokenRegistry()
    token, _ = reg.mint("sess_1")
    # Wrong session should fail (and consume the token).
    assert reg.consume(token, session_id="sess_other") is False
    assert reg.consume(token, session_id="sess_1") is False


def test_unknown_token_returns_false():
    reg = AttachTokenRegistry()
    assert reg.consume("atk_nope", session_id="sess_1") is False


def test_expired_token_returns_false():
    reg = AttachTokenRegistry(ttl_seconds=-1.0)  # already expired on mint
    token, _ = reg.mint("sess_1")
    assert reg.consume(token, session_id="sess_1") is False


def test_prune_expired():
    reg = AttachTokenRegistry(ttl_seconds=-1.0)
    reg.mint("a")
    reg.mint("b")
    pruned = reg.prune_expired()
    assert pruned == 2
