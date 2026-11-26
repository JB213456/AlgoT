from live_ingestion.health import ConnectionHealth, ConnectionState


def test_initial_state_is_starting():
    h = ConnectionHealth(name="ws")
    assert h.state == ConnectionState.STARTING


def test_mark_message_transitions_to_connected():
    h = ConnectionHealth(name="ws")
    h.mark_message()
    assert h.state == ConnectionState.CONNECTED
    assert h.messages_received == 1


def test_reconnect_counted_after_disconnect():
    h = ConnectionHealth(name="ws")
    h.mark_message()
    h.mark_disconnected()
    h.mark_connected()
    assert h.reconnects == 1


def test_mark_error_sets_last_error():
    h = ConnectionHealth(name="ws")
    h.mark_error(RuntimeError("boom"))
    assert h.state == ConnectionState.ERROR
    assert "boom" in h.last_error


def test_is_stale_without_messages_when_connected():
    h = ConnectionHealth(name="ws")
    h.mark_connected()
    assert h.is_stale(max_age_seconds=10) is True


def test_is_stale_false_right_after_message():
    h = ConnectionHealth(name="ws")
    h.mark_message()
    assert h.is_stale(max_age_seconds=10) is False
