#!/usr/bin/env python3
"""Generate multisplit.yaml from questionnaire.yaml.

    python3 tools/build_config.py            regenerate the config
    python3 tools/build_config.py --ask      prompt for what is unanswered
    python3 tools/build_config.py --check    fail if the config has drifted

The work happens in custom_components/multisplit/generate.py, which the
integration's options flow uses too, so editing the file by hand and filling
in the form in Home Assistant cannot diverge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUESTIONNAIRE = ROOT / "questionnaire.yaml"
OUTPUT = ROOT / "multisplit.yaml"
TRANSLATIONS = ROOT / "custom_components" / "multisplit" / "translations" / "en.json"
STRINGS = ROOT / "custom_components" / "multisplit" / "strings.json"

sys.path.insert(0, str(ROOT / "custom_components" / "multisplit"))

from generate import AnswerError, generate, render_translations, validate, wrap  # noqa: E402


def prompt(question: dict):
    """Ask one question at the terminal, with its explanation."""
    print(f"\n{question['ask']}")
    for line in wrap(question.get("why", "")):
        print(f"  {line}")
    if question.get("unit"):
        print(f"  Units: {question['unit']}")
    if question.get("options"):
        print(f"  One of: {', '.join(str(o) for o in question['options'])}")
    if "assume" in question:
        print(f"  Press enter to keep the assumption: {question['assume']!r}")
    while True:
        raw = input("> ").strip()
        if not raw:
            return None
        try:
            return validate(question, yaml.safe_load(raw))
        except AnswerError as error:
            print(f"  {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ask", action="store_true", help="prompt for unanswered questions")
    parser.add_argument("--check", action="store_true", help="fail if the config is out of date")
    args = parser.parse_args()

    questionnaire = yaml.safe_load(QUESTIONNAIRE.read_text(encoding="utf-8"))

    if args.ask:
        for section in questionnaire["sections"]:
            shown = False
            for question in section["questions"]:
                if question.get("answer") is not None:
                    continue
                if not shown:
                    print(f"\n=== {section['title']} ===")
                    shown = True
                answer = prompt(question)
                if answer is not None:
                    question["answer"] = answer

    try:
        rendered, assumed, missing = generate(questionnaire)
    except AnswerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if missing:
        print("error: unanswered questions with no assumption to fall back on:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        print("\nAnswer them in questionnaire.yaml, or run with --ask.", file=sys.stderr)
        return 1

    from core.config import ConfigError, load

    try:
        config = load(yaml.safe_load(rendered))
    except ConfigError as error:
        print(f"error: the generated configuration is not valid:\n{error}", file=sys.stderr)
        return 1

    translations = json.dumps(render_translations(questionnaire), indent=2, ensure_ascii=False) + "\n"

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        stale = [
            path.name
            for path, wanted in ((TRANSLATIONS, translations), (STRINGS, translations))
            if not path.exists() or path.read_text(encoding="utf-8") != wanted
        ]
        if stale:
            print(
                f"error: {', '.join(stale)} does not match {QUESTIONNAIRE.name}.\n"
                "Run: python3 tools/build_config.py",
                file=sys.stderr,
            )
            return 1
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
        TRANSLATIONS.parent.mkdir(parents=True, exist_ok=True)
        TRANSLATIONS.write_text(translations, encoding="utf-8")
        STRINGS.write_text(translations, encoding="utf-8")
        print(f"wrote {OUTPUT.name}, {TRANSLATIONS.name}, {STRINGS.name}")

    rooms = ", ".join(f"{g.name}: {len(config.rooms_in(g.key))} rooms" for g in config.groups)
    print(f"{len(config.rooms)} rooms across {len(config.groups)} groups ({rooms})")
    print(f"hardware writes: {'enabled' if config.operation.apply_to_hardware else 'SHADOW MODE'}")
    if assumed:
        print(f"{len(assumed)} value(s) still assumed: {', '.join(assumed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
