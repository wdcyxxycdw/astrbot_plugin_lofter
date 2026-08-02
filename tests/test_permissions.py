from core.permissions import is_admin_event


class CallableEvent:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def is_admin(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_is_admin_event_accepts_only_callable_is_admin():
    event = CallableEvent(True)
    assert is_admin_event(event) is True
    assert event.calls == 1


def test_is_admin_event_fails_closed_for_false_missing_and_exception():
    false_event = CallableEvent(False)
    error_event = CallableEvent(RuntimeError("boom"))

    assert is_admin_event(false_event) is False
    assert is_admin_event(object()) is False
    assert is_admin_event(error_event) is False
    assert false_event.calls == 1
    assert error_event.calls == 1


def test_is_admin_event_rejects_boolean_attribute_and_group_roles():
    class RoleOnlyEvent:
        is_admin = True
        role = "admin"
        sender = type("Sender", (), {"is_group_owner": True, "role": "owner"})()

    assert is_admin_event(RoleOnlyEvent()) is False
