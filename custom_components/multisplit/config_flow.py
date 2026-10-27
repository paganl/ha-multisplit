"""Setup and reconfiguration.

Setup is deliberately thin: the configuration lives in multisplit.yaml, so
there is nothing for a setup wizard to collect. All it does is check that the
file is there and valid, and say what it found.

Reconfiguration renders the questionnaire as a form. Answers are written back
to questionnaire.yaml, multisplit.yaml is regenerated from it, and the
controller restarts -- the same path as editing the file by hand and calling
multisplit.reload, through the same validation. There is one source of truth
and two ways to reach it, rather than two sources that drift.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
import yaml

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONFIG_FILE, DOMAIN, QUESTIONNAIRE_FILE
from .core.config import ConfigError, load
from .generate import AnswerError, generate, render_translations, validate

_LOGGER = logging.getLogger(__name__)

#: Answers too structured for a form field, edited as YAML in a text box.
_YAML_TYPES = ("curve", "records", "pair")


def _read(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class MultisplitConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per Home Assistant instance; the YAML holds everything else."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        path = Path(self.hass.config.path(CONFIG_FILE))
        if not await self.hass.async_add_executor_job(path.exists):
            return self.async_abort(reason="file_missing")

        try:
            raw = await self.hass.async_add_executor_job(_read, path)
            config = await self.hass.async_add_executor_job(load, raw)
        except ConfigError as error:
            return self.async_show_form(
                step_id="user",
                errors={"base": "invalid_config"},
                description_placeholders={"problems": str(error)},
            )
        except (OSError, yaml.YAMLError) as error:
            return self.async_show_form(
                step_id="user",
                errors={"base": "invalid_config"},
                description_placeholders={"problems": str(error)},
            )

        if user_input is None:
            summary = ", ".join(
                f"{group.name}: {len(config.rooms_in(group.key))} rooms"
                for group in config.groups
            )
            mode = "live" if config.operation.apply_to_hardware else "shadow mode"
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({}),
                description_placeholders={"summary": f"{summary} — {mode}"},
            )

        return self.async_create_entry(title="Multi-split climate control", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> MultisplitOptionsFlow:
        return MultisplitOptionsFlow()


class MultisplitOptionsFlow(OptionsFlow):
    """The questionnaire, as a form."""

    def __init__(self) -> None:
        self._questionnaire: dict | None = None
        self._section: dict | None = None

    # -- picking a section ---------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        path = Path(self.hass.config.path(QUESTIONNAIRE_FILE))
        if not await self.hass.async_add_executor_job(path.exists):
            return self.async_abort(reason="questionnaire_missing")

        self._questionnaire = await self.hass.async_add_executor_job(_read, path)
        titles = [section["title"] for section in self._questionnaire["sections"]]

        if user_input is not None:
            self._section = next(
                section
                for section in self._questionnaire["sections"]
                if section["title"] == user_input["section"]
            )
            return await self.async_step_edit()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("section"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=titles, mode=selector.SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    # -- editing one section -------------------------------------------

    def _schema(self) -> vol.Schema:
        assert self._section is not None
        fields: dict = {}

        for question in self._section["questions"]:
            current = question.get("answer")
            if current is None:
                current = question.get("assume")
            kind = question.get("type", "number")
            key = vol.Optional(question["id"], default=_as_default(current, kind))

            if kind == "boolean":
                fields[key] = selector.BooleanSelector()
            elif kind == "choice":
                fields[key] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[str(option) for option in question["options"]],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            elif kind in ("number", "number_or_null"):
                fields[key] = selector.TextSelector()
            elif kind == "entity":
                fields[key] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=question["domain"])
                )
            elif kind in _YAML_TYPES:
                fields[key] = selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                )
            else:
                fields[key] = selector.TextSelector()

        return vol.Schema(fields)

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._questionnaire is not None and self._section is not None

        if user_input is None:
            return self.async_show_form(
                step_id="edit",
                data_schema=self._schema(),
                description_placeholders={"section": self._section["title"]},
            )

        problems: list[str] = []
        for question in self._section["questions"]:
            if question["id"] not in user_input:
                continue
            given = user_input[question["id"]]

            # Every field arrives as text apart from booleans and selects, so
            # parse it as YAML: that handles numbers, null, and the curves and
            # lists edited in a text box, with one rule instead of five.
            if isinstance(given, str):
                if not given.strip():
                    continue
                try:
                    given = yaml.safe_load(given)
                except yaml.YAMLError as error:
                    problems.append(f"{question['id']}: {error}")
                    continue

            try:
                question["answer"] = validate(question, given)
            except AnswerError as error:
                problems.append(str(error))

        if not problems:
            try:
                rendered, _, missing = generate(self._questionnaire)
            except AnswerError as error:
                problems.append(str(error))
                rendered, missing = "", []
            else:
                problems.extend(f"unanswered: {item}" for item in missing)

            if not problems:
                try:
                    await self.hass.async_add_executor_job(
                        load, yaml.safe_load(rendered)
                    )
                except ConfigError as error:
                    problems.append(str(error))

        if problems:
            return self.async_show_form(
                step_id="edit",
                data_schema=self._schema(),
                errors={"base": "invalid_answer"},
                description_placeholders={
                    "section": self._section["title"],
                    "problems": "\n".join(problems),
                },
            )

        await self.hass.async_add_executor_job(
            self._write, self._questionnaire, rendered
        )
        _LOGGER.info(
            "multisplit: %s updated from the UI; reloading", self._section["title"]
        )
        return self.async_create_entry(title="", data={})

    def _write(self, questionnaire: dict, rendered: str) -> None:
        """Write the questionnaire, the config and the generated labels."""
        root = Path(self.hass.config.path(""))
        (root / QUESTIONNAIRE_FILE).write_text(
            yaml.safe_dump(questionnaire, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        (root / CONFIG_FILE).write_text(rendered, encoding="utf-8")

        # Labels follow the questions, so a reworded question relabels its own
        # field without anyone editing a translation file.
        translations = Path(__file__).parent / "translations" / "en.json"
        try:
            translations.write_text(
                json.dumps(render_translations(questionnaire), indent=2, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
        except OSError as error:  # a read-only install is not a failure
            _LOGGER.debug("multisplit: could not refresh translations: %s", error)


def _as_default(value: Any, kind: str) -> Any:
    """Render a current answer as something a form field can hold.

    The YAML styles are chosen per type because a form field is read by a
    person: a list of rooms wants a line each, while a curve point wants to
    stay on one line as the [outdoor, value] pair it is. Flow style for
    everything put the whole group and room list on a single unreadable line.
    """
    if kind == "boolean":
        return bool(value)
    if kind == "choice":
        return str(value)
    if kind == "records":
        # Block style: one entry per line group, as it appears in the file.
        return yaml.safe_dump(
            value, default_flow_style=False, sort_keys=False, allow_unicode=True
        ).strip()
    if kind == "curve":
        # Block list, but each point inline: "- [7.0, 15]".
        return yaml.safe_dump(
            value, default_flow_style=None, sort_keys=False, allow_unicode=True
        ).strip()
    if kind == "pair":
        return yaml.safe_dump(value, default_flow_style=True).strip()
    if value is None:
        return "null"
    return str(value)
