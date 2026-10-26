"""Questionnaire → configuration YAML.

Shared by the command line tool and the integration's options flow, so the
file a person edits by hand and the form they fill in through the Home
Assistant UI go through exactly the same validation and produce exactly the
same output.

Imports yaml and the standard library only -- no Home Assistant, and no
relative imports, so it can be imported as ``generate`` from the tools or as
``custom_components.multisplit.generate`` inside Home Assistant.

Nothing here invents a value. An unanswered question either has an `assume`,
in which case the output is marked UNMEASURED, or it is reported as missing.
"""

from __future__ import annotations

import textwrap
from typing import Any

import yaml

HEADER = """\
# Multi-split climate control — generated configuration.
#
# DO NOT EDIT. Generated from questionnaire.yaml.
#
# Change an answer in questionnaire.yaml and regenerate:
#
#     python3 tools/build_config.py
#
# or reconfigure through Home Assistant: Settings → Devices & services →
# Multi-split climate → Configure, which writes the answers back and
# regenerates this file.
#
# No threshold, curve, entity id or room name lives in the Python. Lines
# marked UNMEASURED are assumptions standing in for a question nobody has
# answered yet — they are the ones worth revisiting first.
"""


class AnswerError(Exception):
    pass


class Flow(list):
    """A list to render inline, so curves read as [outdoor, value] pairs."""


yaml.SafeDumper.add_representer(
    Flow,
    lambda dumper, data: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True
    ),
)


# --------------------------------------------------------------------------
# Answer validation
# --------------------------------------------------------------------------


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnswerError(f"{where}: expected a number, got {value!r}")
    return float(value)


def validate(question: dict, value: Any) -> Any:
    kind = question.get("type", "number")
    where = question["id"]

    if kind == "entity":
        domain = question["domain"]
        if not isinstance(value, str) or not value.startswith(f"{domain}."):
            raise AnswerError(f"{where}: expected a {domain}.* entity id, got {value!r}")
        return value

    if kind == "number":
        return _number(value, where)

    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            raise AnswerError(f"{where}: expected a non-empty string, got {value!r}")
        return value.strip()

    if kind == "boolean":
        if not isinstance(value, bool):
            raise AnswerError(f"{where}: expected true or false, got {value!r}")
        return value

    if kind == "number_or_null":
        return None if value is None else _number(value, where)

    if kind == "choice":
        if value not in question["options"]:
            raise AnswerError(f"{where}: expected one of {question['options']}, got {value!r}")
        return value

    if kind == "pair":
        if not isinstance(value, list) or len(value) != 2:
            raise AnswerError(f"{where}: expected two values, got {value!r}")
        return Flow(_number(v, where) for v in value)

    if kind == "curve":
        if not isinstance(value, list) or not value:
            raise AnswerError(f"{where}: expected a non-empty list of [outdoor, value] pairs")
        points = []
        for point in value:
            if not isinstance(point, list) or len(point) != 2:
                raise AnswerError(f"{where}: expected [outdoor, value], got {point!r}")
            points.append(Flow([_number(point[0], where), _number(point[1], where)]))
        if [p[0] for p in points] != sorted(p[0] for p in points):
            raise AnswerError(f"{where}: curve points must be in ascending outdoor order")
        return points

    if kind == "records":
        if not isinstance(value, list) or not value:
            raise AnswerError(f"{where}: expected a non-empty list of entries")
        field = question["key_field"]
        out: dict[str, dict] = {}
        for record in value:
            if not isinstance(record, dict) or field not in record:
                raise AnswerError(f"{where}: every entry needs a '{field}', got {record!r}")
            key = record[field]
            if key in out:
                raise AnswerError(f"{where}: duplicate {field} {key!r}")
            out[key] = {
                k: (Flow(v) if isinstance(v, list) else v)
                for k, v in record.items()
                if k != field
            }
        return out

    raise AnswerError(f"{where}: unknown question type {kind!r}")


# --------------------------------------------------------------------------
# Building the tree
# --------------------------------------------------------------------------


class Node:
    """An ordered tree that remembers which question produced each key."""

    def __init__(self) -> None:
        self.children: dict[str, Node] = {}
        self.value: Any = None
        self.leaf = False
        self.comment: list[str] = []

    def at(self, path: list[str]) -> Node:
        node = self
        for part in path:
            node = node.children.setdefault(part, Node())
        return node


def build(questionnaire: dict) -> tuple[Node, list[str], list[str]]:
    root = Node()
    assumed: list[str] = []
    missing: list[str] = []

    for section in questionnaire["sections"]:
        first = True
        for question in section["questions"]:
            answer = question.get("answer")

            if answer is None:
                if "assume" not in question:
                    missing.append(f"{question['id']}: {question['ask']}")
                    continue
                answer = question["assume"]
                assumed.append(question["id"])
                marked = True
            else:
                marked = False

            value = validate(question, answer)

            node = root.at(question["target"].split("."))
            node.leaf = True
            node.value = value

            comment: list[str] = []
            if first:
                comment.append("")
                comment.append(f"=== {section['title']} ===")
                if section.get("note"):
                    comment.append("")
                    comment.extend(wrap(section["note"]))
                comment.append("")
                first = False
            comment.append(question["ask"])
            if question.get("why"):
                comment.extend(wrap(question["why"]))
            if question.get("unit"):
                comment.append(f"Units: {question['unit']}.")
            if marked:
                comment.append("UNMEASURED — assumed; the question was left blank.")
            node.comment = comment

    return root, assumed, missing


def wrap(text: str, width: int = 72) -> list[str]:
    return textwrap.wrap(" ".join(text.split()), width=width)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(node: Node, indent: int = 0) -> list[str]:
    lines: list[str] = []
    pad = " " * indent

    for key, child in node.children.items():
        for line in child.comment:
            lines.append(f"{pad}# {line}".rstrip())

        if child.leaf:
            dumped = yaml.safe_dump(
                {key: child.value},
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            ).rstrip()
            lines.extend(f"{pad}{line}" for line in dumped.splitlines())
        else:
            lines.append(f"{pad}{key}:")
            lines.extend(render(child, indent + 2))

    return lines


def generate(questionnaire: dict) -> tuple[str, list[str], list[str]]:
    """Render the configuration, and say what was assumed and what is missing."""
    root, assumed, missing = build(questionnaire)
    body = "\n".join(render(root))
    return f"{HEADER}\n{body}\n", assumed, missing


# --------------------------------------------------------------------------
# Translations
# --------------------------------------------------------------------------


def render_translations(questionnaire: dict) -> dict:
    """Build the integration's translations from the questionnaire.

    The reconfigure form shows one field per question, and the label and
    helper text are the question and its explanation. Generating them from the
    same file means the form cannot drift from the questionnaire -- and that
    the person changing a threshold in the UI reads the same reasoning as the
    person editing the YAML.
    """
    labels: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    sections: dict[str, str] = {}

    for section in questionnaire["sections"]:
        sections[section["title"]] = section["title"]
        for question in section["questions"]:
            label = question["ask"]
            if question.get("unit"):
                label = f"{label} ({question['unit']})"
            labels[question["id"]] = label
            why = " ".join(question.get("why", "").split())
            if "assume" in question and question.get("answer") is None:
                why = f"{why} Currently an assumption, not a measurement."
            descriptions[question["id"]] = why.strip()

    return {
        "config": {
            "step": {
                "user": {
                    "title": "Multi-split climate control",
                    "description": (
                        "Reads multisplit.yaml from the configuration directory. "
                        "Generate it from questionnaire.yaml first."
                    ),
                }
            },
            "abort": {
                "single_instance_allowed": "Multi-split climate control is already set up.",
                "file_missing": (
                    "multisplit.yaml was not found in the configuration directory."
                ),
            },
            "error": {"invalid_config": "{problems}"},
        },
        "options": {
            "step": {
                "init": {
                    "title": "Reconfigure",
                    "description": (
                        "Pick a section to change. Answers are written back to "
                        "questionnaire.yaml, multisplit.yaml is regenerated, and the "
                        "controller restarts."
                    ),
                    "data": {"section": "Section"},
                },
                "edit": {
                    "title": "{section}",
                    "description": (
                        "Leave a field as it is to keep the current answer. "
                        "Curves and lists are edited as YAML."
                    ),
                    "data": labels,
                    "data_description": descriptions,
                },
            },
            "error": {
                "invalid_answer": "{problems}",
                "invalid_config": "{problems}",
            },
            "abort": {
                "questionnaire_missing": (
                    "questionnaire.yaml was not found in the configuration directory, "
                    "so there is nothing to reconfigure. Copy it there, or edit "
                    "multisplit.yaml by hand and call multisplit.reload."
                )
            },
        },
    }
