"""Tests for the master-side node registry."""
import time

import pytest

from transcodarr_core import cluster


@pytest.fixture(autouse=True)
def _clear():
    cluster._nodes.clear()
    yield
    cluster._nodes.clear()


def test_register_and_list():
    cluster.register_node("a", "10.0.0.1", {"backends": []}, 4, True)
    nodes = cluster.list_nodes()
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "a"
    assert nodes[0]["online"] is True
    assert nodes[0]["worker_count"] == 4


def test_aggregate_counts_only_online_and_storage_ok():
    cluster.register_node("ok", "10.0.0.1", {}, 4, True)
    cluster.register_node("no_storage", "10.0.0.2", {}, 2, False, "not visible")
    assert cluster.aggregate_worker_count() == 4  # storage-bad node excluded


def test_heartbeat_updates_worker_count():
    cluster.register_node("a", "10.0.0.1", {}, 4, True)
    assert cluster.heartbeat("a", worker_count=6) is True
    assert cluster.aggregate_worker_count() == 6


def test_heartbeat_unknown_node_returns_false():
    """An unknown node (master restarted) must be told to re-register."""
    assert cluster.heartbeat("ghost") is False


def test_offline_after_timeout_excluded():
    cluster.register_node("a", "10.0.0.1", {}, 4, True)
    cluster._nodes["a"]["last_seen"] = time.time() - (cluster.HEARTBEAT_TIMEOUT_S + 1)
    assert cluster.list_nodes()[0]["online"] is False
    assert cluster.aggregate_worker_count() == 0


def test_reregister_preserves_registered_at():
    cluster.register_node("a", "10.0.0.1", {}, 4, True)
    first = cluster.list_nodes()[0]["registered_at"]
    cluster.register_node("a", "10.0.0.1", {}, 8, True)  # reconnect
    assert cluster.list_nodes()[0]["registered_at"] == first
    assert cluster.list_nodes()[0]["worker_count"] == 8


def test_remove_node():
    cluster.register_node("a", "10.0.0.1", {}, 4, True)
    cluster.remove_node("a")
    assert cluster.list_nodes() == []


def test_online_nodes_filters_storage_and_liveness():
    cluster.register_node("live_ok", "10.0.0.1", {}, 4, True)
    cluster.register_node("live_nostorage", "10.0.0.2", {}, 2, False)
    cluster.register_node("dead", "10.0.0.3", {}, 3, True)
    cluster._nodes["dead"]["last_seen"] = time.time() - 999
    ids = {n["node_id"] for n in cluster.online_nodes()}
    assert ids == {"live_ok"}
