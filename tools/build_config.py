#!/usr/bin/env python3
"""Generate multisplit.yaml from questionnaire.yaml.

    python3 tools/build_config.py            regenerate the config
    python3 tools/build_config.py --ask      prompt for what is unanswered
    python3 tools/build_config.py --check    fail if the config has drifted

The questionnaire holds the questions as they were asked and the answers as
they were given. This turns those answers into the shape the integration
wants -- curves, seconds, nested keys -- and carries the question text into
the generated file as comments, so the config explains itself without anyone
having to keep two documents in step.

Nothing here invents a value. An unanswered question either has an `assume`,
in which case the output is marked UNMEASURED, or it is an error.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUESTIONNAIRE = ROOT / "questionnaire.yaml"
OUTPUT = ROOT / "multisplit.yaml"

sys.path.insert(0, str(ROOT / "custom_components" / "multisplit"))

HEADER = """\
# Multi-split climate control — generated configuration.
#
# DO NOT EDIT. Generated from questionnaire.yaml by tools/build_config.py.
# Change an answer there and regenerate:
#
#     python3 tools/build_config.py
#
# Copy to the Home Assistant configuration directory as multisplit.yaml.
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


def build(questionnaire: dict, *, ask: bool) -> tuple[Node, list[str], list[str]]:
    root = Node()
    assumed: list[str] = []
    missing: list[str] = []

    for section in questionnaire["sections"]:
        first = True
        for question in section["questions"]:
            answer = question.get("answer")

            if answer is None and "assume" not in question and ask:
                answer = prompt(question)
                question["answer"] = answer

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


def prompt(question: dict) -> Any:
    print(f"\n{question['ask']}")
    for line in wrap(question.get("why", "")):
        print(f"  {line}")
    if question.get("unit"):
        print(f"  Units: {question['unit']}")
    if question.get("options"):
        print(f"  One of: {', '.join(question['options'])}")
    while True:
        raw = input("> ").strip()
        if not raw:
            return None
        try:
            return validate(question, yaml.safe_load(raw))
        except AnswerError as error:
            print(f"  {error}")


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


def generate(questionnaire: dict, *, ask: bool) -> tuple[str, list[str], list[str]]:
    root, assumed, missing = build(questionnaire, ask=ask)
    body = "\n".join(render(root))
    return f"{HEADER}\n{body}\n", assumed, missing


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ask", action="store_true", help="prompt for unanswered questions")
    parser.add_argument("--check", action="store_true", help="fail if the config is out of date")
    args = parser.parse_args()

    questionnaire = yaml.safe_load(QUESTIONNAIRE.read_text(encoding="utf-8"))

    try:
        rendered, assumed, missing = generate(questionnaire, ask=args.ask)
    except AnswerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if missing:
        print("error: unanswered questions with no assumption to fall back on:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        print("\nAnswer them in questionnaire.yaml, or run with --ask.", file=sys.stderr)
        return 1

    # The real check: does it load?
    from core.config import ConfigError, load

    try:
        config = load(yaml.safe_load(rendered))
    except ConfigError as error:
        print(f"error: the generated configuration is not valid:\n{error}", file=sys.stderr)
        return 1

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(
                f"error: {OUTPUT.name} does not match {QUESTIONNAIRE.name}.\n"
                "Run: python3 tools/build_config.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT.name} is up to date.")
    else:
        if args.ask:
            QUESTIONNAIRE.write_text(
                yaml.safe_dump(questionnaire, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            print(f"\nwrote {QUESTIONNAIRE.name}")
        OUTPUT.write_text(rendered, encoding="utf-8")
        print(f"wrote {OUTPUT.name}")

    rooms = ", ".join(f"{g.name}: {len(config.rooms_in(g.key))} rooms" for g in config.groups)
    print(f"{len(config.rooms)} rooms across {len(config.groups)} groups ({rooms})")
    if assumed:
        print(f"{len(assumed)} value(s) still assumed: {', '.join(assumed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
