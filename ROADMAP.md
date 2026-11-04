# Roadmap

What is planned, in order. Each item says why it is here. Most come from live
telemetry since the switch to live control on 31 Oct 2026. Done items move to
[CHANGELOG.md](CHANGELOG.md).

## 1.0.0

1.0 means the behaviour and the configuration format are settled, and the
integration copes with its own failure modes. Criteria:

### Robustness

- **Missing or malformed entities raise a Repairs issue.** A configured entity
  that does not exist is skipped silently today. This has happened three times:
  the window-lockout ids (1 Nov), the bedroom template (31 Oct–3 Nov) and
  `sensor.sensor.bme680_2_temperature` (4 Nov). Reject ids with more than one
  `.` when the config loads. Don't flag anything until Home Assistant has
  finished starting: for the first seconds after a restart every climate entity
  reads as missing.
- **Out-of-range readings are rejected.** A 0.0° reading is accepted today, and
  frost protection then heats. Seen seven times on 1–3 Nov, from failed Evohome
  polls and twice from a unit's own reading. Check both the room-sensor path
  and the return-air path in `measurement.collect`.
- **A run in progress survives a restart.** After a restart, a room heating
  towards its cut-off but inside the resume deadband is left off. Erin, 3 Nov:
  stopped at 19.8° against a 21.5° cut-off, and not reheated for 2½ hours.
- **The window lockout holds through a Home Assistant restart.** The device's
  lockout cleared when its contact sensor went unavailable during the restart,
  and Erin heated for 6 minutes with the window open (1 Nov).
- **A unit that keeps refusing commands is flagged,** instead of being
  rewritten every interval indefinitely.

### Indoor units

- **Room temperature reaches the units reliably.**
  `remote_temperature_timeout: 30min` in `esphome/common/cn105.yaml` drops the
  room temperature once a reading has held steady for 30 minutes, because Home
  Assistant sends ESPHome only changed values. The unit then runs on its own
  return-air sensor. Raise the timeout, and confirm in telemetry.

### Proven in use

- **Two weeks of telemetry with no new finding.** No room heating or stopping
  without a reason.
- **Cooling run live at least once.** The outdoor temperature has stayed
  below the cooling lockout since going live, so the cooling path has not run.
  Either wait for warm weather or do a controlled test with the lockout
  lowered.

## After 1.0

### Multi-language support

Dutch first, with English kept. Four layers, in this order:

1. **Home Assistant's own text.** Translation keys for entity names (today
   hard-coded `_attr_name`: Status, Mode, Overshoot, Divergence,
   Multi-split away, *mode* allowed), service descriptions (out of
   `services.yaml` into `strings.json`), and error messages. Then
   `translations/nl.json`. Each user sees their own profile language.
2. **The schedule card.** About 75 strings hard-coded in
   `multisplit-schedule-card.js`; a dictionary per language, chosen from
   `hass.locale.language`. Times and dates already follow Home Assistant's
   locale.
3. **Room status reasons.** The planner returns a reason code with its values
   instead of an English sentence, and the integration renders it. The text
   appears in the house's language (`hass.config.language`), since it is made
   on the server. **Telemetry keeps the codes, untranslated,** so reviews and
   `tools/replay.py` do not depend on the language. This touches every reason
   string and the planner tests, so do it after the 1.0 changes to those
   reasons.
4. **The Configure form.** Generated from `questionnaire.yaml`, about 600 lines
   of questions and explanations; the generator takes them per language.
   Mostly translation work. Lowest priority, since only an admin sees it.

### Smaller items

- **Rooms on away do not bank solar.** Banking raises an away room from 16° to
  17° and heats an empty room (Ischa and the office, 2 Nov).
- **Hysteresis on what a room wants.** The living room's average sensor moves
  by 0.1° around the deadband edge, producing about 80 telemetry records a
  night that change nothing.
- **Telemetry records rooms switched off,** as off by request, instead of
  leaving them out.
- **One planner pass per cycle.** At a schedule mark, the change and the tick
  can both plan, giving "holding off, only 0s since the last write".
