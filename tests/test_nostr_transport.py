"""Tests for Phase 5A Nostr transport foundation."""

import time
from unittest.mock import MagicMock

import pytest

from modules.database import HiveDatabase
from modules.nostr_transport import NostrTransport


@pytest.fixture
def mock_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc = MagicMock()
    plugin.rpc.signmessage.return_value = {"zbase": "nostr-derivation-sig"}
    return plugin


@pytest.fixture
def database(mock_plugin, tmp_path):
    db_path = str(tmp_path / "test_nostr.db")
    db = HiveDatabase(db_path, mock_plugin)
    db.initialize()
    return db


def test_identity_persists_across_restarts(mock_plugin, database):
    t1 = NostrTransport(mock_plugin, database)
    id1 = t1.get_identity()
    assert len(id1["pubkey"]) == 64
    assert len(id1["privkey"]) == 64

    t2 = NostrTransport(mock_plugin, database)
    id2 = t2.get_identity()
    assert id2["pubkey"] == id1["pubkey"]
    assert id2["privkey"] == id1["privkey"]


def test_start_stop_and_status(mock_plugin, database):
    transport = NostrTransport(mock_plugin, database)
    assert transport.start()
    status = transport.get_status()
    assert status["running"] is True
    assert status["relay_count"] >= 1

    transport.stop()
    status = transport.get_status()
    assert status["running"] is False


def test_publish_updates_last_event_state(mock_plugin, database):
    transport = NostrTransport(mock_plugin, database)
    transport.start()
    event = transport.publish({"kind": 1, "content": "hello"})
    assert "id" in event
    assert "sig" in event

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if database.get_nostr_state("event:last_published_id") == event["id"]:
            break
        time.sleep(0.05)

    assert database.get_nostr_state("event:last_published_id") == event["id"]
    assert database.get_nostr_state("event:last_published_at") is not None
    transport.stop()


def test_send_dm_and_process_inbound_callbacks(mock_plugin, database):
    transport = NostrTransport(mock_plugin, database)

    seen = []
    transport.receive_dm(lambda evt: seen.append(evt))

    outbound_dm = transport.send_dm("02" + "11" * 32, "ping")
    inbound_dm = dict(outbound_dm)
    transport.inject_event(inbound_dm)
    processed = transport.process_inbound()

    assert processed == 1
    assert len(seen) == 1
    assert seen[0]["kind"] == 4
    assert seen[0]["plaintext"] == "ping"


def test_subscribe_filters(mock_plugin, database):
    transport = NostrTransport(mock_plugin, database)

    events = []
    sub_id = transport.subscribe({"kinds": [38901]}, lambda evt: events.append(evt))
    assert sub_id

    transport.inject_event({"kind": 1, "id": "a" * 64, "pubkey": "b" * 64, "created_at": int(time.time())})
    transport.inject_event({"kind": 38901, "id": "c" * 64, "pubkey": "d" * 64, "created_at": int(time.time())})
    processed = transport.process_inbound()

    assert processed == 2
    assert len(events) == 1
    assert events[0]["kind"] == 38901

    assert transport.unsubscribe(sub_id)

