# ESPHome packages for the indoor units

Shared configuration for the five ESP32 + CN105 controllers. The room files
(`mitsubishi-ac-*.yaml`) hold the per-unit substitutions and `!secret`
references, and live in Home Assistant's ESPHome folder; the copies here are a
record of them, so keep them in step when you change one. They pull
`common/` from this repository as
[remote packages](https://esphome.io/components/packages/), so the shared parts
can be maintained here rather than in the Device Builder, which does not show
package files.

The room files expect a local `common/wifi.yaml` and a `secrets.yaml` next to
them in Home Assistant; neither belongs in this repository.

| File | What it holds | Needs these substitutions |
| --- | --- | --- |
| `common/base.yaml` | board, framework, logger, OTA, Bluetooth proxy | `device_name`, `friendly_name` |
| `common/cn105.yaml` | CN105 component (pinned), UART, climate entity, room temperature feed, restart button | `friendly_name`, `remote_temp_sensor`, `hp_rx_pin`, `hp_tx_pin`, `restart_button_name` |
| `common/window_lockout.yaml` | switch off after the lockout sensor has been open for the grace period; refuse to turn on while open; publish that as **Window Lockout** | `ac_lockout_sensor`, `ac_lockout_delay`, `room_label` |
| `common/diagnostics.yaml` | uptime, Wi-Fi, version, time | — |

Nothing here may use `!secret`: ESPHome does not allow it in remote packages.

## Using them from a room file

```yaml
packages:
  wifi: !include common/wifi.yaml   # local: holds Wi-Fi secrets
  shared:
    url: https://github.com/paganl/ha-multisplit
    ref: main
    refresh: always
    files:
      - esphome/common/base.yaml
      - esphome/common/cn105.yaml
      - esphome/common/window_lockout.yaml   # rooms with a lockout sensor only
      - esphome/common/diagnostics.yaml
```

`refresh: always` makes every Validate and Install fetch the repository. Keep
it: ESPHome caches remote packages in `.esphome/packages`, which **Clean Build
Files** does not clear, so with a longer refresh a change pushed here is not
seen until that interval has passed.

Unit-specific extras (vanes, i-see, outside air, runtime hours) belong in the
room file, added to the climate entity with `climate: - id: !extend hp`.

## Windows and the integration

The device enforces the lockout itself, so a unit stays off with a window open
even if Home Assistant is down. It also publishes **Window Lockout**
(`binary_sensor.<device name>_window_lockout`), on once its grace has passed.
Set that as the room's `lockout_sensor` in the integration's questionnaire and
the planner stands the room aside too: no vote for its group's mode, no
commands the device would refuse, and *window open* as its status.
