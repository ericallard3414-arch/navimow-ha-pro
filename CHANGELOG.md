## 0.7.1

- Show exactly one mower and one mowing trail in the bundled JS dashboard card.
- Detect camera-baked overlays and suppress the card's duplicate SVG mower/trail layers.
- Correct the standalone camera mower marker's 90-degree direction offset.
- Bump the bundled frontend cache version so browsers load the corrected card immediately.

## 0.7.0-beta.19

- Restore the current mower position and direction directly in the standalone live-map camera image.
- Restore the persisted blade-on mowing trail directly in the standalone live-map camera image.
- Keep the custom dashboard card's smoother attribute-based mower and trail overlays unchanged.
- Correct the integration manifest version so HACS detects and reports the update normally.

## 0.7.0-beta.17

- Keep the displayed mower pinned to the charging-pile coordinates whenever its state is Docked or Charging.
- Prevent later stale location packets from moving a docked mower away from the base marker.
- Retain the fullscreen overlay redraw fix and iPhone header sizing improvements.

## 0.7.0-beta.16

- Restore the current mower pose and live trail immediately after entering or exiting fullscreen map mode.
- Fix a docked/charging mower appearing clipped at the top-left corner in fullscreen.
- Retain the iPhone-only smaller Schedule and Configuration buttons from beta.15.

## 0.7.0-beta.15

- Reduce only the Schedule and Configuration header buttons on iPhone-width screens (480px and below).
- Preserve the existing button sizes on tablets, laptops, desktops, and 4K dashboards.

## 0.7.0-beta.14

- Increase visible mowing-trail stroke on large/4K dashboards while preserving laptop sizing.
- 2000px+ viewport: 16px trail; 3000px+ viewport: 18px trail.
- Store mowing trails per partition instead of as one global path.
- Starting Front Lawn no longer erases partial/completed work from Back Lawn or any other unselected lawn.
- Resume preserves the selected lawn trail; Start Fresh clears only the selected lawn(s).
- A 100% completed lawn automatically starts as a fresh job when selected again.
- Migrates beta.9-beta.12 mowing-only trails to partition-tagged storage without throwing away valid work.

- Fix mowing trail disappearing after the mower returns to the charger or after an HA/integration restart.
- Persist `trail_schema_version` together with the mowing-only trail so valid beta.8+ trail data is no longer mistaken for legacy travel-path data.
- Force an immediate trail save on pause, return-home, docked, charging, and idle transitions.
- Keeps beta.11 4K trail scaling and beta.10 Chromium overlay/smooth mower movement.

## 0.7.0-beta.11

- Scale the native live mowing trail width with viewport size so 4K portrait dashboards no longer render an overly thin trail.
- Keep laptop/desktop sizing effectively unchanged: the trail remains about 6 px on normal-width browsers and grows smoothly up to 12 px on 4K displays.
- Retains beta.10 Chromium/Edge trail overlay and smooth mower interpolation.

# Changelog

## 0.7.0-beta.10

- Render the mowing-only trail as a native Lovelace SVG overlay instead of relying on camera-frame refreshes. This fixes missing trail rendering seen in Chrome/Edge.
- Expose `live_trail_path`, `live_trail_points`, and `mower_pose` on the live-map camera entity.
- Smooth mower movement between telemetry samples with requestAnimationFrame interpolation and shortest-path heading interpolation.
- Reduce full camera image refreshes to 20 seconds; terrain/map imagery remains static while trail and mower update from Home Assistant state events.
- Retains beta.9 persistent mowing-only trail storage and resume/start-fresh behavior.


## 0.7.0-beta.9

- Fix mowing-only trail disappearing after Home Assistant/integration restart.
- Persist the trail schema marker in Home Assistant storage (beta.8 omitted it).
- Save the blade-on trail immediately when mowing transitions to pause/return/dock.
- Reduce live-trail persistence debounce from 30 seconds to 10 seconds.
- Official server path groups remain diagnostics-only; visible coverage is still blade-on live trail only.

## 0.7.0-beta.8

- Make the live map strictly **mowing-only**: official server path archives are no longer rendered as coverage because they include transport/approach movement, even inside a lawn polygon.
- Keep official path data available internally/diagnostically for protocol research.
- Persist only the live blade-on trail for visual coverage and resume continuity.
- Add trail schema v2 migration: legacy cached travel trails from beta.7 and earlier are cleared once on upgrade.

## 0.7.0-beta.5

- Keep the current mowing trail visible after RETURN HOME / dock / idle; passive progress=0 telemetry no longer erases it.
- Persist last authoritative per-zone completion percentages so unfinished work remains resumable after docking and Home Assistant restarts.
- START FRESH explicitly clears the selected zone progress cache while suppressing stale pre-reset server percentages until a new server session appears.
- Show explicit inline **Resume work** and **Start fresh** controls whenever the selected zone(s) already have partial completion.
- Resume controls work for manually returned-to-dock jobs as well as interrupted/paused jobs.

## 0.7.0-beta.4

- Added logical Resume / Start fresh choice when selected zones already contain mowing progress.
- Resume sends the official continue-work partition mode without clearing existing coverage or trail.
- Start fresh keeps the existing fresh-session behavior and clears old trail immediately.
- The last commanded unfinished zone selection is persisted and restored on the dashboard after a manual return to dock or Home Assistant restart.
- Live-map camera now exposes per-zone progress and resumable zone IDs for the dashboard.

# Changelog

## 0.7.0-beta.7
- PAUSE is now state-aware: while paused the same button becomes RESUME and uses the native lawn_mower resume command.
- Zone Resume / Start Fresh choices are only shown when the mower is docked/idle/charging; they are hidden during mowing, pause, and return-home transitions.
- MOW/Resume/Fresh launch controls disable immediately after a job is launched and remain unavailable while a mowing job is active.
- RETURN HOME leaves unfinished per-zone progress available so selecting that zone at the dock exposes Resume / Start Fresh again.

## 0.7.0-beta.2

- Added experimental official Navimow swept-trail download using `/vehicle/trail/get-path-info-data-compress`.
- Added Base64 + Zstandard decoding for compressed private-cloud payloads.
- The live mowing map now renders the server-authoritative trail when available while retaining the high-frequency live trail overlay.


## 0.7.0-beta.1

First public beta candidate based on the internally tested 0.6.30 build.

### Highlights
- Interactive LiDAR map and live mower position.
- Multi-zone selection and ordered zone mowing.
- Fast live-map bootstrap when a mowing session starts.
- Mowing trail separated from travel-to-zone movement, with perimeter tolerance.
- Dock/charging position snap to avoid stale mower positions after docking.
- Responsive dashboard with map fullscreen mode for phones/tablets/laptops.
- Friendly zone names with compact zone IDs.
- Cutting-height and work-mode controls.
- Configuration panel with inline switches and sliders.
- Native Navimow schedule editor.
- Rain, wind, frost, snow and temperature delay handling/alerts.
- Manual Return Home no longer reuses a stale weather-delay reason.
- Local Home Assistant brand assets for the integration.

### Beta notes
- The integration uses unofficial/private Navimow interfaces and may break if Segway/Navimow changes them.
- The current test baseline is an i-series LiDAR mower. Other models need community testing.
