"""The questionnaire is the source; the config is generated from it.

These tests exist so the two cannot drift apart silently, and so a change to
the generator cannot quietly rewrite the measured values.
"""

from pathlib import Path

import pytest
import yaml

import generate

ROOT = Path(__file__).resolve().parent.parent

QUESTIONNAIRE = yaml.safe_load((ROOT / "questionnaire.yaml").read_text(encoding="utf-8"))


def questions():
    return [q for section in QUESTIONNAIRE["sections"] for q in section["questions"]]


def test_the_committed_config_matches_the_questionnaire():
    """The drift check, as a test rather than only as a CI step."""
    rendered, _, missing = generate.generate(QUESTIONNAIRE)
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
    _, assumed, _ = generate.generate(QUESTIONNAIRE)
    assert set(assumed) == {
        "tick_seconds",
        "write_interval_seconds",
        "stale_after_minutes",
        "telemetry_max_mb",
        "telemetry_events",
        "telemetry_movement",
        "telemetry_heartbeat_minutes",
        "min_on_minutes",
        "lockout_hysteresis",
        "deadband",
        "comfort_band",
        "inertia_weight",
        "inertia_half_life_minutes",
        "aging_rate",
        "stale_grace_minutes",
    }


def test_shadow_mode_is_the_shipped_default():
    """Writes stay off until the divergences from the old system are looked at."""
    generated = yaml.safe_load((ROOT / "multisplit.yaml").read_text(encoding="utf-8"))
    assert generated["operation"]["apply_to_hardware"] is False


def test_telemetry_is_on_by_default():
    """The curves cannot be refitted from data nobody recorded."""
    generated = yaml.safe_load((ROOT / "multisplit.yaml").read_text(encoding="utf-8"))
    assert generated["telemetry"]["enabled"] is True


def test_the_harvested_values_are_not_assumptions():
    """These came from the building and must be real answers."""
    answered = {q["id"]: q.get("answer") for q in questions()}
    assert answered["cut_off_offset"] == [[0.0, 1.5]]
    assert answered["rest_minutes"] == [[0.0, 5], [7.0, 15], [13.0, 25]]
    assert answered["resume_deadband"] == 0.5
    assert answered["cool_below"] == 20.0
    assert answered["heat_above"] == 24.0
    assert answered["default_target"] == 20.0
    assert answered["away_target"] == 16.0
    assert answered["outdoor_sensor"] == "sensor.openweathermap_temperature"


def test_every_question_declares_its_type():
    """A question with no type falls back to "number", which is wrong for most.

    Added after an edit dropped `type: pair` from the setpoint range: the
    generator rejected it loudly, which is the right failure, but a question
    missing its type should not get as far as the generator.
    """
    known = {
        "number", "number_or_null", "boolean", "text", "entity",
        "choice", "pair", "curve", "records",
    }
    for question in questions():
        assert "type" in question, f"{question['id']} declares no type"
        assert question["type"] in known, f"{question['id']}: unknown type {question['type']!r}"
        if question["type"] == "entity":
            assert "domain" in question, f"{question['id']} needs a domain"
        if question["type"] == "choice":
            assert question.get("options"), f"{question['id']} needs options"
        if question["type"] == "records":
            assert question.get("key_field"), f"{question['id']} needs a key_field"


def test_every_target_is_unique():
    """Two questions writing the same key would silently fight each other."""
    targets = [q["target"] for q in questions()]
    assert len(targets) == len(set(targets))


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
        ("boolean", "yes"),
        ("boolean", 1),
        ("text", ""),
        ("text", 7),
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
    with pytest.raises(generate.AnswerError):
        generate.validate(question, value)


def test_a_duplicate_room_key_is_refused():
    question = {"id": "rooms", "type": "records", "key_field": "key"}
    with pytest.raises(generate.AnswerError, match="duplicate"):
        generate.validate(question, [{"key": "office"}, {"key": "office"}])


# --------------------------------------------------------------------------
# Reconfiguring must not corrupt the answer it just read
# --------------------------------------------------------------------------


def records_question(question_id):
    return next(q for q in questions() if q["id"] == question_id)


@pytest.mark.parametrize("question_id", ["groups", "rooms"])
def test_a_record_answer_round_trips_through_validation(question_id):
    """The reconfigure bug: validating an answer returned the *configuration's*
    shape, which was written back as the answer and was not a valid answer, so
    the next edit of that section failed and showed the wrong thing."""
    question = records_question(question_id)
    once = generate.validate(question, question["answer"])
    twice = generate.validate(question, once)
    assert once == twice == question["answer"]


@pytest.mark.parametrize("question_id", ["groups", "rooms"])
def test_validation_keeps_the_authored_shape_and_to_config_keys_it(question_id):
    question = records_question(question_id)
    answer = generate.validate(question, question["answer"])
    assert isinstance(answer, list), "the questionnaire lists entries"

    config = generate.to_config(question, answer)
    assert isinstance(config, dict), "the configuration keys them"
    assert set(config) == {record["key"] for record in answer}
    assert all("key" not in body for body in config.values()), (
        "the key becomes the mapping key, not a field inside it"
    )


def test_a_questionnaire_damaged_by_that_bug_repairs_itself():
    """A keyed mapping where a list belongs is accepted and normalised, so a
    file already written by the broken version reads correctly."""
    question = records_question("groups")
    damaged = {"a": {"name": "Group A"}, "b": {"name": "Group B"}}
    assert generate.validate(question, damaged) == question["answer"]


def test_a_record_entry_still_needs_its_key():
    question = records_question("groups")
    with pytest.raises(generate.AnswerError, match="needs a 'key'"):
        generate.validate(question, [{"name": "Group A"}])


def test_to_config_leaves_every_other_type_alone():
    for question in questions():
        if question["type"] == "records":
            continue
        answer = question.get("answer", question.get("assume"))
        if answer is None:
            continue
        validated = generate.validate(question, answer)
        assert generate.to_config(question, validated) is validated
