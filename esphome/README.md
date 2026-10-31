# ESPHome packages for the indoor units

Shared configuration for the five ESP32 + CN105 controllers. The room files
stay in Home Assistant's ESPHome folder (they hold the per-unit substitutions
and `!secret` references); they pull these files from this repository as
[remote packages](https://esphome.io/components/packages/), so the shared parts
can be maintained here rather than in the Device Builder, which does not show
package files.

| File | What it holds | Needs these substitutions |
| --- | --- | --- |
| `common/base.yaml` | board, framework, logger, OTA, Bluetooth proxy | `device_name`, `friendly_name` |
| `common/cn105.yaml` | CN105 component (pinned), UART, climate entity, room temperature feed, restart button | `friendly_name`, `remote_temp_sensor`, `hp_rx_pin`, `hp_tx_pin`, `restart_button_name` |
| `common/window_lockout.yaml` | switch off after the lockout sensor has been open for the grace period; refuse to turn on while open | `ac_lockout_sensor`, `ac_lockout_delay`, `room_label` |
| `common/diagnostics.yaml` | uptime, Wi-Fi, version, time | — |

Nothing here may use `!secret`: ESPHome does not allow it in remote packages.

## Using them from a room file

```yaml
packages:
  wifi: !include common/wifi.yaml   # local: holds Wi-Fi secrets
  shared:
    url: https://github.com/paganl/ha-multisplit
    ref: main
    refresh: 1d
    files:
      - esphome/common/base.yaml
      - esphome/common/cn105.yaml
      - esphome/common/window_lockout.yaml   # rooms with a lockout sensor only
      - esphome/common/diagnostics.yaml
```

ESPHome caches the repository for `refresh`. After pushing a change here, use
**Clean Build Files** on a device in the Device Builder (or wait a day) before
installing, or the old copy is used.

Unit-specific extras (vanes, i-see, outside air, runtime hours) belong in the
room file, added to the climate entity with `climate: - id: !extend hp`.

## Not handled by the integration yet

The window lockout lives on the device because the multisplit integration has
no notion of an open window: it still counts that room's demand and keeps
re-sending heat/cool while the device vetoes it. Moving the lockout into the
integration would remove both.
