"""Constructed pilot: the aggregate checker intentionally has a documented bug.

Its initial happy-path tests pass. The controller's independent contract check
must fail until a Codex task repairs the treatment of non-success statuses.
"""


def check_succeeded(status):
    return status == "success"


def required_checks_succeeded(checks, required):
    """Initial bug: non-empty failure strings are truthy. Repair via pilot task."""
    return bool(required) and all(checks.get(name) for name in required)
