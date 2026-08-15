"""Live SVG trail map for Navimow Complete."""
from __future__ import annotations

import html
import math
import base64

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NavimowCoordinator

VIEW = 800


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][config_entry.entry_id]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]
    async_add_entities(NavimowTrailCamera(c) for c in coordinators.values())


class NavimowTrailCamera(CoordinatorEntity[NavimowCoordinator], Camera):
    """A correctly scaled, persistent mower-coordinate map."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        device = coordinator.device
        self._attr_name = "Live mowing map"
        self._attr_unique_id = f"{DOMAIN}_{device.id}_live_map"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            sw_version=device.firmware_version or None,
            serial_number=device.serial_number or device.id,
        )
        self.content_type = "image/svg+xml"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose exact map geometry for the interactive Lovelace zone card."""
        geometry = self.coordinator.get_map_geometry() or {}
        zones = []
        for zone in geometry.get("zones") or []:
            points = zone.get("points") or []
            if len(points) < 3:
                continue
            zones.append({
                "id": zone.get("id"),
                "name": self.coordinator.get_zone_label(zone.get("id"))
                or zone.get("name")
                or f"Zone {zone.get('id')}",
                "points": points,
            })

        terrain = self.coordinator.get_terrain_map()
        view = None
        if terrain is not None:
            _image, meta = terrain
            view = {
                "min_x": meta.get("min_x"),
                "max_x": meta.get("max_x"),
                "min_y": meta.get("min_y"),
                "max_y": meta.get("max_y"),
                "scale": meta.get("pixels_per_meter"),
                "width": meta.get("width"),
                "height": meta.get("height"),
            }
        elif zones:
            all_points = [point for zone in zones for point in zone["points"]]
            xs = [float(point[0]) for point in all_points]
            ys = [float(point[1]) for point in all_points]
            min_x, max_x = math.floor(min(xs) - 2), math.ceil(max(xs) + 2)
            min_y, max_y = math.floor(min(ys) - 2), math.ceil(max(ys) + 2)
            span_x = max(max_x - min_x, 1)
            span_y = max(max_y - min_y, 1)
            scale = VIEW / max(span_x, span_y)
            view = {
                "min_x": min_x, "max_x": max_x,
                "min_y": min_y, "max_y": max_y,
                "scale": scale,
                "width": max(240, round(span_x * scale)),
                "height": max(240, round(span_y * scale)),
            }

        location = self.coordinator.get_location() or {}
        delay_context = self.coordinator.get_delay_context()
        return {
            "selectable_zones": zones,
            "map_view": view,
            # Raw Navimow type-4 location message.  The official app uses
            # this to indicate that an active/one-time mowing task is being
            # delayed (for example by rain).  Exposing it lets the Lovelace
            # card react immediately without waiting for another entity.
            "task_delay": location.get("taskDelay"),
            # Latched copy survives the very short type-4 message and the
            # mower's transition back to charging, so the UI can explain why.
            "last_task_delay": delay_context.get("last_task_delay"),
            "last_task_delay_age_s": delay_context.get("last_task_delay_age_s"),
            "last_task_delay_epoch_ms": delay_context.get("last_task_delay_epoch_ms"),
            "interruption_notice": delay_context.get("interruption_notice"),
        }

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes:
        return self._render().encode("utf-8")

    def _render(self) -> str:
        trail = self.coordinator.get_trail()
        location = self.coordinator.get_location() or {}
        geometry = self.coordinator.get_map_geometry() or {}
        terrain = self.coordinator.get_terrain_map()
        zones = geometry.get("zones") or []
        paths = geometry.get("paths") or []
        dock = geometry.get("dock")
        all_points = [p for zone in zones for p in zone["points"]]
        all_points.extend(p for route in paths for p in route["points"])
        all_points.extend(trail)
        if isinstance(dock, list):
            all_points.append(dock)
        if not all_points and terrain is None:
            return self._placeholder("Waiting for the mower to send live location data")

        terrain_svg = ""
        if terrain is not None:
            image, meta = terrain
            min_x, max_x = meta["min_x"], meta["max_x"]
            min_y, max_y = meta["min_y"], meta["max_y"]
            scale = meta["pixels_per_meter"]
            width, height = meta["width"], meta["height"]
            encoded = base64.b64encode(image).decode("ascii")
            terrain_svg = (
                f'<image href="data:image/webp;base64,{encoded}" x="0" y="0" '
                f'width="{width}" height="{height}" preserveAspectRatio="none"/>'
            )
        else:
            xs = [p[0] for p in all_points]
            ys = [p[1] for p in all_points]
            min_x, max_x = math.floor(min(xs) - 2), math.ceil(max(xs) + 2)
            min_y, max_y = math.floor(min(ys) - 2), math.ceil(max(ys) + 2)
            span_x = max(max_x - min_x, 1)
            span_y = max(max_y - min_y, 1)
            scale = VIEW / max(span_x, span_y)
            width = max(240, round(span_x * scale))
            height = max(240, round(span_y * scale))

        def px(x: float) -> float:
            return (x - min_x) * scale

        def py(y: float) -> float:
            return (max_y - y) * scale

        def _point_in_polygon(x: float, y: float, points: list) -> bool:
            clean = []
            for point in points or []:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    continue
                try:
                    clean.append((float(point[0]), float(point[1])))
                except (TypeError, ValueError):
                    continue
            if len(clean) < 3:
                return False
            inside = False
            j = len(clean) - 1
            for i, (xi, yi) in enumerate(clean):
                xj, yj = clean[j]
                if (yi > y) != (yj > y):
                    edge_x = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
                    if x < edge_x:
                        inside = not inside
                j = i
            return inside

        def _zone_for_point(point):
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, ValueError, IndexError):
                return None
            for mapped_zone in zones:
                if _point_in_polygon(x, y, mapped_zone.get("points") or []):
                    return mapped_zone.get("id")
            return None

        # Build separate path segments per lawn zone. This prevents the final
        # point of one zone from being joined by a straight line to the first
        # point of the next zone while the mower is merely travelling between
        # them with the blades not covering grass.
        trail_commands = []
        previous_zone = object()
        previous_point = None
        for point in trail:
            zone_id = _zone_for_point(point)
            gap = False
            if previous_point is not None:
                try:
                    gap = math.hypot(
                        float(point[0]) - float(previous_point[0]),
                        float(point[1]) - float(previous_point[1]),
                    ) > 2.5
                except (TypeError, ValueError, IndexError):
                    gap = True
            command = "M" if (not trail_commands or zone_id != previous_zone or gap) else "L"
            trail_commands.append(f"{command} {px(point[0]):.1f} {py(point[1]):.1f}")
            previous_zone = zone_id
            previous_point = point
        trail_path = " ".join(trail_commands)

        # The mower icon must ALWAYS follow the latest live pose, even when we
        # intentionally suppress trail drawing while it travels to/from a zone.
        mower = None
        x = location.get("postureX")
        y = location.get("postureY")
        try:
            mower = [float(x), float(y)]
        except (TypeError, ValueError):
            mower = trail[-1] if trail else None
        progress = location.get("mowingPercentage")
        progress_text = f"{progress}%" if progress is not None else "Live"
        theta = location.get("postureTheta")
        try:
            heading = math.degrees(float(theta)) % 360
        except (TypeError, ValueError):
            heading = 0.0
        # Navimow reports postureTheta in mathematical map coordinates
        # (zero=east, positive=counter-clockwise). SVG uses a downward Y axis,
        # so the equivalent display rotation is the negative angle. While the
        # mower is moving, its latest position vector is even more dependable
        # than a potentially delayed heading packet.
        marker_rotation = -heading
        if len(trail) >= 2:
            current = trail[-1]
            for previous in reversed(trail[:-1]):
                dx = current[0] - previous[0]
                dy = current[1] - previous[1]
                if math.hypot(dx, dy) >= 0.03:
                    marker_rotation = math.degrees(math.atan2(-dy, dx))
                    break
        zone = location.get("currentMowBoundary")
        if zone is None:
            zone = location.get("targetZone")
        zone_text = f" · Zone {zone}" if zone is not None else ""
        def polygon_centroid(points):
            """Return the area-weighted polygon centroid, with a safe average fallback."""
            pts = [(float(p[0]), float(p[1])) for p in points if len(p) >= 2]
            if not pts:
                return (0.0, 0.0)
            if len(pts) < 3:
                return (
                    sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts),
                )
            twice_area = 0.0
            cx_sum = 0.0
            cy_sum = 0.0
            for idx, current in enumerate(pts):
                nxt = pts[(idx + 1) % len(pts)]
                cross = current[0] * nxt[1] - nxt[0] * current[1]
                twice_area += cross
                cx_sum += (current[0] + nxt[0]) * cross
                cy_sum += (current[1] + nxt[1]) * cross
            if abs(twice_area) < 1e-9:
                return (
                    sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts),
                )
            return (cx_sum / (3.0 * twice_area), cy_sum / (3.0 * twice_area))

        # Zone labels are intentionally NOT rendered into the camera image.
        # The dashboard SVG overlay owns all zone-name/ID labels. Rendering a
        # second label here caused the old plain name to remain visible under
        # the new pill label. Keeping the camera layer label-free also means
        # renames are reflected in exactly one place.
        zone_svg: list[str] = []
        route_svg = "".join(
            f'<polyline points="{" ".join(f"{px(p[0]):.1f},{py(p[1]):.1f}" for p in route["points"])}" fill="none" stroke="#91a9b7" stroke-width="5" stroke-linecap="round" stroke-dasharray="8 5"/>'
            for route in paths
        )
        dock_svg = ""
        if isinstance(dock, list):
            dock_svg = (
                f'<g transform="translate({px(dock[0]):.1f} {py(dock[1]):.1f})">'
                '<circle r="19" fill="#252c37" stroke="#ffffff" stroke-width="2" '
                'stroke-opacity=".2"/>'
                '<path d="M 2 -13 L -10 2 L -2 2 L -6 13 L 11 -5 L 2 -5 Z" '
                'fill="#ffffff" stroke="#ffffff" stroke-width="1.5" '
                'stroke-linejoin="round"/>'
                '</g>'
            )
        trail_svg = (
            f'<path d="{trail_path}" fill="none" stroke="#e4e7eb" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" opacity=".38"/>'
            if len(trail) >= 2 else ""
        )
        marker_svg = ""
        if mower is not None:
            marker_svg = (
                f'<g transform="translate({px(mower[0]):.1f} {py(mower[1]):.1f}) rotate({marker_rotation:.1f})">'
                # Soft shadow and a rounded mower body whose front faces +X.
                '<ellipse cx="0" cy="3" rx="20" ry="16" fill="#000" opacity=".28"/>'
                '<path d="M -15 -14 H 9 Q 18 -14 20 -5 V 5 Q 18 14 9 14 H -15 '
                'Q -20 10 -20 5 V -5 Q -20 -10 -15 -14 Z" fill="#aeb8c9" '
                'stroke="#f7f9fc" stroke-width="2"/>'
                '<path d="M -12 -10 H 8 Q 14 -10 15 -4 V 5 Q 14 10 8 10 H -12 Z" '
                'fill="#202735"/>'
                # LiDAR turret and orange status indicator.
                '<circle cx="7" cy="-2" r="8" fill="#111723" stroke="#eef2f7" stroke-width="3"/>'
                '<circle cx="-10" cy="6" r="3.5" fill="#ff672c"/>'
                # Small nose mark makes the travel direction unambiguous.
                '<path d="M 20 -5 L 25 0 L 20 5 Z" fill="#ff672c" stroke="#fff" stroke-width="1.5"/>'
                '</g>'
            )
        return "".join((
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">',
            # Deliberately omit a canvas rectangle: areas outside the LiDAR
            # terrain remain transparent when the camera is embedded in a card.
            terrain_svg,
            "".join(zone_svg),
            route_svg,
            dock_svg,
            trail_svg,
            marker_svg,
            f'<rect x="{max(width - 248, 18):.1f}" y="18" width="230" height="46" rx="23" fill="#111d27" opacity=".92"/>',
            f'<text x="{max(width - 133, 133):.1f}" y="49" text-anchor="middle" fill="#fff" font-size="22" '
            f'font-family="sans-serif">{html.escape(str(progress_text) + zone_text)}</text>',
            '</svg>',
        ))

    @staticmethod
    def _placeholder(message: str) -> str:
        safe = html.escape(message)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{VIEW}" height="500" '
            f'viewBox="0 0 {VIEW} 500">'
            '<rect width="100%" height="100%" rx="22" fill="#071018"/>'
            '<circle cx="400" cy="205" r="34" fill="#ff7a1a"/>'
            f'<text x="400" y="285" text-anchor="middle" fill="#dce8ef" '
            f'font-size="20" font-family="sans-serif">{safe}</text></svg>'
        )
