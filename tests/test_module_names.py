"""Module names Home Assistant reserves for its own platform hooks.

Found the hard way: a module called `intent.py` is picked up by the voice
assistant's intent component, which calls `async_setup_intents` on it and
raises AttributeError on every start. The file is unrelated to voice, but the
name is not ours to use.

Entity platforms (sensor, climate, binary_sensor) are the opposite case: those
names are how Home Assistant finds the platforms we do want.
"""

from pathlib import Path

INTEGRATION = Path(__file__).resolve().parent.parent / "custom_components" / "multisplit"

#: Names Home Assistant scans every integration for and calls into. Using one
#: for something else makes it a platform of that component by accident.
RESERVED = {
    "intent",
    "logbook",
    "trigger",
    "condition",
    "device_trigger",
    "device_condition",
    "device_action",
    "recorder",
    "repairs",
    "system_health",
    "hardware",
    "backup",
    "conversation",
    "media_source",
    "notify",
    "group",
    "scene",
}

#: Platform names we publish on purpose; these must keep their names.
INTENDED_PLATFORMS = {"sensor", "binary_sensor", "climate"}

#: Integration-level hooks we implement on purpose.
INTENDED_HOOKS = {"config_flow", "diagnostics"}


def module_names():
    return {path.stem for path in INTEGRATION.glob("*.py")} - {"__init__"}


def test_no_module_accidentally_becomes_a_platform():
    clashes = module_names() & RESERVED
    assert not clashes, (
        f"{sorted(clashes)} are reserved by Home Assistant components; "
        "rename them or they will be loaded as platforms of those components"
    )


def test_the_platforms_we_do_publish_are_still_named_correctly():
    names = module_names()
    assert INTENDED_PLATFORMS <= names
    assert INTENDED_HOOKS <= names


def test_comfort_is_not_called_intent():
    """The specific regression: it was intent.py and broke every start."""
    names = module_names()
    assert "comfort" in names
    assert "intent" not in names
