# Changelog

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
