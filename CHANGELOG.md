## 0.7.22

- Make cutting-height controls use the exact discrete height list advertised by each mower.
- Detect quarter-inch cutting grids directly from mower telemetry instead of the model name.
- Preserve every other mower's exact advertised discrete grid without forcing an i-series or X-series assumption.
- Support the X430 quarter-inch grid from 0.75 to 4.00 in, with rounded metric labels of 19, 26, 32, 39, 45, 51, 57, 64, 70, 76, 82, 89, 95 and 101 mm.
- Expose supported millimetre positions and the height family on the Home Assistant number entity.
- Use discrete telemetry-provided positions in both the main dashboard slider and Configuration slider.
- Preserve two-decimal labels such as 0.75 in and exact backend snapping.
- Send telemetry-detected quarter-inch heights to the mower as numeric millimetres, preventing the X430 from interpreting hexadecimal text as a much lower decimal height.
- Keep the confirmed X430 range bounded to its advertised 19–101 mm / 0.75–4.00 in positions.
- Bump the bundled frontend cache version so dashboards load the model-aware controls.

## 0.7.21

- Distinguish stale pre-write settings from genuine changes made in the official Navimow app.
- Keep protecting a Home Assistant height selection while the cloud repeats its old value.
- Accept a third, externally selected height immediately instead of masking it for the full optimistic-write timeout.
- Poll only the lightweight mutable-settings endpoint every 5 seconds so app changes appear in Home Assistant much sooner than the normal 30-second coordinator cycle.
- Cancel the dedicated settings task cleanly when the integration unloads or reloads.
- Preserve the normal coordinator interval, heavier telemetry schedule and all model-specific command encodings.

## 0.7.20

- Refresh mutable mower settings every coordinator cycle so changes made in the Navimow app propagate back to Home Assistant promptly.
- Prevent a successfully selected cutting height from reverting to an older cached value after about one minute.
- Extend the optimistic settings guard from 45 to 180 seconds so Navimow's cloud has time to acknowledge a write.
- Keep heavier maintenance, device-information and planning requests on the existing slow refresh cycle.
- Preserve the confirmed model-specific i-series and X-series command encodings.

## 0.7.19

- Match the official Navimow imperial cutting-height labels: each 5 mm position is displayed as a 0.2 in step, including 90 mm as 3.6 in.
- Convert imperial card selections back to the exact mower height grid while preserving Home Assistant's native-unit conversion.
- Preserve the confirmed model-specific i-series and X-series write protocols; both mower families can briefly show stale values until their next cloud refresh.
- Bump the bundled frontend cache version so dashboards load the corrected unit mapping.

## 0.7.18

- Polish the dashboard's dark-mode resume banner and action area.
- Replace light-theme white and grey disabled controls with dark surfaces, subtle borders and readable muted text.
- Keep Resume visually prominent while improving Start Fresh, Pause and Return Home states.
- Bump the bundled frontend cache version so dashboards load the updated dark palette.

## 0.7.17

- Fix cutting-height sliders when Home Assistant converts state/min/max to inches but leaves the backend step in millimetres.
- Build both card sliders on Navimow's consistent 5 mm height grid instead of the mixed-unit step attribute.
- Prevent the main slider thumb from jumping to the maximum while the value remains at 65 mm.
- Bump the bundled frontend cache version so dashboards load the corrected JavaScript.

## 0.7.16

- Replace the main cutting-height range with discrete positions mapped to exact mower-supported height increments.
- Fix the slider thumb remaining at the minimum in Chromium and Android WebView after switching display units.
- Preserve correct Automatic, Metric and Imperial readouts and convert commands back to the Home Assistant entity's native unit.
- Bump the bundled frontend cache version so dashboards load the corrected card instead of the cached 0.7.15 JavaScript.

## 0.7.14

- Add Automatic, Metric and Imperial measurement-unit choices to the dashboard Configuration drawer.
- Make Automatic follow Home Assistant's configured unit system.
- Convert both cutting-height sliders and readouts while keeping all mower commands in supported native millimetres.
- Save the card's unit override in the current browser and support an optional `height_units: auto|metric|imperial` YAML setting.
- Bump the bundled frontend cache version so existing dashboards load the updated card.

## 0.7.13

- Send cutting-height values to i-series mowers using their decimal robot encoding while retaining hexadecimal encoding for X-series models.
- Snap Home Assistant's imperial-to-metric conversion to the nearest height advertised by the mower before writing.
- Prevent i215 height selections such as 2.6 in from being normalized back to approximately 1.6–1.8 in.

## 0.7.12

- Correctly decode hexadecimal cutting-height values reported by X-series mowers.
- Resolve ambiguous height strings against each mower's advertised supported-height list, preserving compatibility with models that report decimal values.
- Prevent a selected height such as 2.6 in (65 mm, reported as `41`) from appearing to reset to about 1.6 in after a refresh or Home Assistant restart.

## 0.7.11

- Fix Resume in the dashboard card by calling Home Assistant's supported `lawn_mower.start_mowing` action.
- Add the missing `asyncio` import used by the fast location bootstrap task.
- Prevent unhandled `NameError` exceptions during location bootstrap and cancellation.