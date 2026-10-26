"""The questionnaire is the source; the config is generated from it.

These tests exist so the two cannot drift apart silently, and so a change to
the generator cannot quietly rewrite the measured values.
"""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_config  # noqa: E402

QUESTIONNAIRE = yaml.safe_load((ROOT / "questionnaire.yaml").read_text(encoding="utf-8"))


def questions():
    return [q for section in QUESTIONNAIRE["sections"] for q in section["questions"]]


def test_the_committed_config_matches_the_questionnaire():
    """The drift check, as a test rather than only as a CI step."""
    rendered, _, missing = build_config.generate(QUESTIONNAIRE, ask=False)
    assert missing == []
    assert rendered == (ROOT / "multisplit.yaml").read_text(encoding="utf-8"), (
        "multisplit.yaml is stale — run: python3 tools/build_config.py"
    )


def test_every_question_is_answered_or_has_a_stated_assumption():
    for question in questions():
        assert question.get("answer") is not None or "assume" in question, (
            f"{question['id']} has neither an answer nor an assumption"
        )


def test_the_assumed_values_are_the_ones_we_think_they_are():
    """A guard against an assumption quietly becoming invisible."""
    _, assumed, _ = build_config.generate(QUESTIONNAIRE, ask=False)
    assert set(assumed) == {
        "min_on_minutes",
        "heat_above",
        "lockout_hysteresis",
        "deadband",
        "inertia_weight",
        "inertia_half_life_minutes",
        "aging_rate",
    }


def test_the_harvested_values_are_not_assumptions():
    """These came from the building and must be real answers."""
    answered = {q["id"]: q.get("answer") for q in questions()}
    assert answered["cut_off_offset"] == [[0.0, 1.5]]
    assert answered["rest_minutes"] == [[0.0, 5], [7.0, 15], [13.0, 25]]
    assert answered["resume_deadband"] == 0.5
    assert answered["cool_below"] == 22.0
    assert answered["outdoor_sensor"] == "sensor.openweathermap_temperature"


def test_every_question_explains_what_the_number_does():
    for question in questions():
        assert question.get("why"), f"{question['id']} has no explanation"
        assert question.get("ask", "").endswith("?"), f"{question['id']} is not a question"


def test_every_target_is_reachable_in_the_generated_config():
    generated = yaml.safe_load((ROOT / "multisplit.yaml").read_text(encoding="utf-8"))
    for question in questions():
        node = generated
        for part in question["target"].split("."):
            assert part in node, f"{question['id']} targets {question['target']}, missing {part}"
            node = node[part]


@pytest.mark.parametrize(
    "kind,value",
    [
        ("number", "not a number"),
        ("number", True),
        ("entity", "no_domain"),
        ("choice", "summer"),
        ("pair", [1.0]),
        ("curve", []),
        ("curve", [[7.0, 1.0], [0.0, 2.0]]),
        ("records", [{"name": "no key field"}]),
    ],
)
def test_bad_answers_are_refused(kind, value):
    question = {
        "id": "test",
        "type": kind,
        "domain": "sensor",
        "options": ["heat", "cool", "auto"],
        "key_field": "key",
    }
    with pytest.raises(build_config.AnswerError):
        build_config.validate(question, value)


def test_a_duplicate_room_key_is_refused():
    question = {"id": "rooms", "type": "records", "key_field": "key"}
    with pytest.raises(build_config.AnswerError, match="duplicate"):
        build_config.validate(question, [{"key": "office"}, {"key": "office"}])
