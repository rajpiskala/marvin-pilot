from __future__ import annotations

import threading
from pathlib import Path

from marvin_pilot.visualizer_runtime import (
    find_reusable_server,
    register_server,
    session_key,
    unregister_server,
)
from marvin_pilot.visualizer_server import VisualizerServer


def test_reuses_only_live_matching_loopback_session(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    key = session_key(str(plan), None, [], None, False)
    assert key is not None
    assert session_key("-", None, [], None, False) is None
    assert session_key(str(plan), None, [], None, True) != key
    server = VisualizerServer(session_key=key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    registration = register_server(server.url, key, directory=tmp_path)
    try:
        assert find_reusable_server(key, directory=tmp_path) == server.url
        assert find_reusable_server("different", directory=tmp_path) is None
    finally:
        server.shutdown()
        thread.join(timeout=5)
        unregister_server(registration)
    assert find_reusable_server(key, directory=tmp_path) is None
