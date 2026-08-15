# Navimow HA Pro

**Public beta — unofficial Home Assistant integration for Segway Navimow robotic mowers.**

Navimow HA Pro brings mower telemetry, controls, LiDAR map data, live position, zone
mowing, schedules and a responsive Navimow-style dashboard into Home Assistant.

> [!WARNING]
> This project is unofficial and is not affiliated with, endorsed by, or supported by
> Segway or Navimow. It uses unofficial/private cloud interfaces that can change without
> notice. Use the beta at your own risk and keep the official Navimow app available.

## Current beta focus

The current development/test baseline is the **Navimow i-series LiDAR family**. The
integration may work with additional Navimow models, but public beta feedback is needed
before broader compatibility is claimed.

## Features

- Home Assistant config flow for Navimow account setup
- Mower status, battery, progress and weekly mowing area
- Live LiDAR map camera with mower position
- Fast position bootstrap when mowing starts
- Friendly editable zone names
- Select one or multiple zones and mow them in order
- Fresh-session/reset behavior for selected-zone jobs
- Live mowing trail that excludes travel-to-zone and return-home movement
- Perimeter trail tolerance for edge mowing near/outside the zone polygon
- Dock-position correction when the mower reports Docked/Charging
- Cutting-height control
- Precision / Standard / Efficient work modes
- Responsive Navimow-style dashboard for kiosk, desktop, tablet and phone
- Fullscreen map mode for easier zone selection on smaller displays
- Configuration panel with inline switches and sliders
- Native Navimow schedule editor with multiple periods and zone selection
- Rain / wind / frost / snow / temperature-delay handling and visual alerts
- Manual Pause and Return Home controls
- Local integration branding for Home Assistant 2026.3+

## Installation with HACS

Until the repository is accepted into the default HACS catalog:

1. In HACS, open **Custom repositories**.
2. Add this GitHub repository URL.
3. Select **Integration** as the category.
4. Install **Navimow HA Pro**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & services → Add integration** and search for
   **Navimow HA Pro**.

For public beta releases, install the newest beta release when HACS offers it.

## Manual installation

Copy:

```text
custom_components/navimow_ha_pro/
```

to:

```text
/config/custom_components/navimow_ha_pro/
```

Restart Home Assistant and add the integration from **Settings → Devices & services**.

## Dashboard

The integration ships its dependency-free scheduler and zone-dashboard JavaScript and
copies/registers the required frontend resources when Home Assistant starts.

Create a panel view containing:

```yaml
views:
  - type: panel
    path: navimow
    title: Navimow
    icon: mdi:robot-mower
    cards:
      - type: custom:navimow-zone-dashboard-card
```

The card is designed to scale between a portrait kiosk and smaller phone/tablet/laptop
screens. Use the map fullscreen control when you want maximum map area for zone
selection.

## Services

The integration includes Home Assistant services for advanced operations, including:

- `navimow_ha_pro.mow` — start one or more selected zone IDs in order
- `navimow_ha_pro.set_schedule` — save a weekday's mowing periods/zones
- `navimow_ha_pro.set_zone_name` — assign friendly names to Navimow partition IDs

The dashboard normally calls these for you.

## Privacy and diagnostics

Do **not** publish Navimow passwords, OAuth/private tokens, MQTT credentials, mower
serial numbers, precise GPS coordinates, or unredacted diagnostics/logs.

The account password is used by the private-map setup path to obtain renewable tokens;
the integration UI states that the password itself is not stored.

## Known beta limitations

- Private Navimow endpoints/protocols can change at any time.
- Model compatibility outside the current i-series LiDAR test baseline is not yet
  established.
- Cloud state acknowledgement can take several seconds even though the dashboard uses
  optimistic controls to keep the UI responsive.
- Location/map freshness ultimately depends on data Navimow makes available to the
  account and mower.
- Local brand images require Home Assistant 2026.3 or newer; older HA versions can still
  run the integration but may show a generic integration icon.

## Reporting bugs

Use the GitHub issue templates and include Home Assistant version, mower model,
integration version, reproduction steps and **redacted** logs. Model-compatibility reports
are especially useful during the beta.

## License

MIT. See [LICENSE](LICENSE).

## Trademark notice

Segway and Navimow names/logos are trademarks of their respective owners. They are used
here only to identify compatibility with the relevant products. This project is not an
official Segway/Navimow product.

## Credits

Navimow HA Pro is based on the MIT-licensed `ilguala/navimow_pro` Home Assistant
integration by Roberto Gualandris and includes substantial dashboard, LiDAR-map,
configuration, scheduling, live-position and trail work developed on top of that base.
The original MIT copyright notice is preserved in this repository's license.
