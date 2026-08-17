"""Live SVG trail map for Navimow Complete."""
from __future__ import annotations

import html
import math
import base64

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NavimowCoordinator

VIEW = 800

# Camera state attributes are stored as one JSON object and Home Assistant
# refuses to record objects larger than 16 KiB. Large LiDAR maps (notably the
# X-series) can contain thousands of polygon/trail points, so expose a compact
# overlay representation while keeping the full-resolution SVG camera image.
MAX_ZONE_ATTRIBUTE_POINTS = 240
MAX_TRAIL_ATTRIBUTE_POINTS = 300


def _evenly_sample(points: list, limit: int) -> list:
    """Return at most limit evenly distributed points, preserving both ends."""
    if len(points) <= limit:
        return list(points)
    if limit <= 1:
        return [points[-1]]
    last = len(points) - 1
    indexes = {round(index * last / (limit - 1)) for index in range(limit)}
    return [points[index] for index in sorted(indexes)]


def _compact_map_points(points: list, limit: int) -> list[list[float]]:
    """Compact map geometry and round coordinates for small state attributes."""
    compact = _evenly_sample(points, max(3, limit))
    result = []
    for point in compact:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            result.append([round(float(point[0]), 3), round(float(point[1]), 3)])
        except (TypeError, ValueError):
            continue
    return result


def _compact_trail(points: list, limit: int) -> list:
    """Compact a trail while retaining endpoints and zone/gap boundaries."""
    if len(points) <= limit:
        return list(points)

    mandatory = {0, len(points) - 1}
    previous = None
    previous_zone = None
    for index, point in enumerate(points):
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        zone = point[2] if len(point) >= 3 else None
        if previous is not None:
            try:
                gap = math.hypot(
                    float(point[0]) - float(previous[0]),
                    float(point[1]) - float(previous[1]),
                ) > 2.5
            except (TypeError, ValueError, IndexError):
                gap = True
            if zone != previous_zone or gap:
                mandatory.update((max(0, index - 1), index))
        previous = point
        previous_zone = zone

    if len(mandatory) >= limit:
        chosen = _evenly_sample(sorted(mandatory), limit)
    else:
        chosen = set(mandatory)
        remaining = limit - len(chosen)
        chosen.update(
            round(index * (len(points) - 1) / max(remaining - 1, 1))
            for index in range(remaining)
        )
    return [points[index] for index in sorted(chosen)]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][config_entry.entry_id]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]
    entities = []
    for coordinator in coordinators.values():
        background = NavimowTrailCamera(
            coordinator,
            include_dynamic_overlays=False,
        )
        live = NavimowTrailCamera(
            coordinator,
            include_dynamic_overlays=True,
            background_camera=background,
        )
        entities.extend((live, background))
    async_add_entities(entities)


class NavimowTrailCamera(CoordinatorEntity[NavimowCoordinator], Camera):
    """A correctly scaled, persistent mower-coordinate map."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        *,
        include_dynamic_overlays: bool = True,
        background_camera: "NavimowTrailCamera | None" = None,
    ) -> None:
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        device = coordinator.device
        self._include_dynamic_overlays = include_dynamic_overlays
        self._background_camera = background_camera
        if include_dynamic_overlays:
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
        else:
            self._attr_name = "Map background"
            self._attr_unique_id = f"{DOMAIN}_{device.id}_map_background"
            # Internal rendering dependency for the bundled dashboard card.
            # Keep it active and addressable, but out of normal device views.
            self._attr_entity_registry_visible_default = False
        self.content_type = "image/svg+xml"

    async def async_added_to_hass(self) -> None:
        """Hide an already-registered internal background camera."""
        await super().async_added_to_hass()
        if self._include_dynamic_overlays or not self.entity_id:
            return
        registry = er.async_get(self.hass)
        entry = registry.async_get(self.entity_id)
        if entry is None:
            return
        updates = {}
        if entry.hidden_by is None:
            updates["hidden_by"] = er.RegistryEntryHider.INTEGRATION
        if entry.device_id is not None:
            # Remove legacy 0.7.2-0.7.4 attachment from the mower device.
            # The entity remains enabled and its state is still available to
            # the bundled JS card through background_camera_entity_id.
            updates["device_id"] = None
        if updates:
            registry.async_update_entity(self.entity_id, **updates)

    def _display_location(self, geometry: dict) -> dict:
        """Return the live pose, pinned to the charging pile while docked.

        Navimow may keep publishing its last approach pose after the state has
        changed to docked/charging.  Treat the map's charging-pile geometry as
        authoritative on every camera/card refresh so a later stale location
        packet cannot pull the displayed mower away from its base again.
        """
        location = dict(self.coordinator.get_location() or {})
        state = self.coordinator.get_device_state()
        raw_state = getattr(state, "state", None) if state is not None else None
        raw_state = getattr(raw_state, "value", raw_state)
        state_name = str(raw_state or "").strip().lower()
        dock = geometry.get("dock")
        if state_name in {"docked", "charging", "ischarging"} and isinstance(
            dock, (list, tuple)
        ) and len(dock) >= 2:
            try:
                location["postureX"] = float(dock[0])
                location["postureY"] = float(dock[1])
                location["position_source"] = "dock_display_snap"
            except (TypeError, ValueError):
                pass
        return location

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose compact map geometry for the interactive Lovelace zone card."""
        # The background entity only supplies an SVG image. Duplicating the live
        # geometry/trail attributes here caused two >16 KiB Recorder warnings.
        if not self._include_dynamic_overlays:
            return {"camera_overlays_baked": False}

        geometry = self.coordinator.get_map_geometry() or {}
        raw_zones = geometry.get("zones") or []
        zone_point_limit = max(
            8, MAX_ZONE_ATTRIBUTE_POINTS // max(len(raw_zones), 1)
        )
        zones = []
        for zone in raw_zones:
            points = zone.get("points") or []
            if len(points) < 3:
                continue
            zones.append({
                "id": zone.get("id"),
                "name": self.coordinator.get_zone_label(zone.get("id"))
                or zone.get("name")
                or f"Zone {zone.get('id')}",
                "points": _compact_map_points(points, zone_point_limit),
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
        elif raw_zones:
            # Calculate bounds from the original geometry so compaction cannot
            # change the camera/card projection.
            all_points = [
                point
                for zone in raw_zones
                for point in (zone.get("points") or [])
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
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

        location = self._display_location(geometry)
        delay_context = self.coordinator.get_delay_context()
        official_groups = self.coordinator.get_official_trail_groups()
        zone_progress = self.coordinator.get_zone_progress_map()
        resumable_zone_ids = self.coordinator.get_resumable_zone_ids()
        # Dynamic map primitives for the Lovelace card.  Keeping the trail
        # and mower pose in entity attributes avoids relying on repeated camera
        # image reloads, which Chrome/Edge can aggressively cache and which also
        # makes the mower appear to jump every few seconds.
        live_trail = self.coordinator.get_trail()
        live_trail_path = ""
        mower_pose = None
        if view:
            try:
                min_x_v = float(view["min_x"])
                max_y_v = float(view["max_y"])
                scale_v = float(view["scale"])

                def _px_v(x):
                    return (float(x) - min_x_v) * scale_v

                def _py_v(y):
                    return (max_y_v - float(y)) * scale_v

                commands = []
                previous = None
                previous_zone = None
                for point in _compact_trail(
                    live_trail[-8000:], MAX_TRAIL_ATTRIBUTE_POINTS
                ):
                    if not isinstance(point, (list, tuple)) or len(point) < 2:
                        continue
                    try:
                        x0, y0 = float(point[0]), float(point[1])
                    except (TypeError, ValueError):
                        continue
                    zone_id = self.coordinator._integer(point[2]) if len(point) >= 3 else None
                    gap = False
                    if previous is not None:
                        gap = math.hypot(x0 - previous[0], y0 - previous[1]) > 2.5
                    new_segment = not commands or gap or (zone_id is not None and previous_zone is not None and zone_id != previous_zone)
                    commands.append(
                        f'{"M" if new_segment else "L"} {_px_v(x0):.1f} {_py_v(y0):.1f}'
                    )
                    previous = (x0, y0)
                    previous_zone = zone_id
                live_trail_path = " ".join(commands)

                lx = location.get("postureX")
                ly = location.get("postureY")
                if lx is not None and ly is not None:
                    theta = location.get("postureTheta")
                    try:
                        heading = (-math.degrees(float(theta))) % 360.0
                    except (TypeError, ValueError):
                        heading = 0.0
                    mower_pose = {
                        "x": round(float(lx), 4),
                        "y": round(float(ly), 4),
                        "heading": round(heading, 2),
                    }
            except (TypeError, ValueError, KeyError):
                live_trail_path = ""
                mower_pose = None

        device = self.coordinator.device
        return {
            "mower_name": device.name,
            "mower_model": device.model or "Unknown",
            "selectable_zones": zones,
            "map_view": view,
            "live_trail_path": live_trail_path,
            "live_trail_points": len(live_trail),
            "mower_pose": mower_pose,
            # Signals the bundled dashboard card that these layers are already
            # present in the camera frame, preventing duplicate mower/trail SVGs.
            "camera_overlays_baked": self._include_dynamic_overlays,
            "background_camera_entity_id": (
                self._background_camera.entity_id
                if self._background_camera is not None
                else None
            ),
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
            "official_trail_groups": len(official_groups),
            "official_trail_points": sum(len(group.get("points") or []) for group in official_groups),
            "official_trail_partitions": sorted({group.get("partition_id") for group in official_groups if group.get("partition_id") is not None}),
            "zone_progress": {str(zone_id): round(progress, 1) for zone_id, progress in zone_progress.items()},
            "resumable_zone_ids": resumable_zone_ids,
        }

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes:
        return self._render().encode("utf-8")

    def _render(self) -> str:
        trail = self.coordinator.get_trail()
        official_trail_groups = self.coordinator.get_official_trail_groups()
        geometry = self.coordinator.get_map_geometry() or {}
        location = self._display_location(geometry)
        terrain = self.coordinator.get_terrain_map()
        zones = geometry.get("zones") or []
        paths = geometry.get("paths") or []
        dock = geometry.get("dock")
        all_points = [p for zone in zones for p in zone["points"]]
        all_points.extend(p for route in paths for p in route["points"])
        all_points.extend(trail)
        # Official trail API data is retained for diagnostics/protocol
        # research only. Navimow includes transport/approach movement in that
        # dataset, including movement inside the selected lawn. The official
        # mobile app does not paint those points as mowing coverage, so they
        # must not affect live-map bounds or rendering.
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
            if isinstance(point, (list, tuple)) and len(point) >= 3:
                embedded = self.coordinator._integer(point[2])
                if embedded is not None:
                    return embedded
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
        # Official trail data can contain both blade-on work strokes and the
        # mower's approach/transfer route.  Only the former belongs on the live
        # coverage map.  First constrain every server group to its own partition
        # polygon, then suppress highly-straight connector groups.  This keeps
        # genuine historical/resumable mowing inside the lawn while avoiding
        # the dock-to-zone / corridor line that Navimow also stores.
        zone_by_id = {
            int(zone.get("id")): zone
            for zone in zones
            if zone.get("id") is not None
        }

        def _looks_like_transfer(points: list) -> bool:
            clean = []
            for point in points or []:
                try:
                    clean.append((float(point[0]), float(point[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            if len(clean) < 2:
                return True
            length = sum(
                math.hypot(b[0] - a[0], b[1] - a[1])
                for a, b in zip(clean, clean[1:])
            )
            if length <= 0.25:
                return True
            chord = math.hypot(clean[-1][0] - clean[0][0], clean[-1][1] - clean[0][1])
            straightness = chord / length if length else 1.0
            # Navimow transfer/approach groups are typically a relatively long,
            # almost straight polyline.  Real coverage groups contain turns,
            # perimeter curvature, or repeated passes.  Keep the threshold
            # deliberately conservative so a short genuine mowing pass survives.
            return (length >= 4.0 and straightness >= 0.94) or (length >= 2.0 and len(clean) <= 5 and straightness >= 0.90)

        official_svg_parts: list[str] = []
        for group in official_trail_groups:
            points = group.get("points") or []
            if len(points) < 2:
                continue
            try:
                partition_id = int(group.get("partition_id")) if group.get("partition_id") is not None else None
            except (TypeError, ValueError):
                partition_id = None
            zone_geometry = zone_by_id.get(partition_id) if partition_id is not None else None
            polygon = zone_geometry.get("points") if zone_geometry else None

            # Split the official path whenever it leaves its own mowing zone.
            # This alone removes historical paths crossing unrelated lawns.
            segments: list[list] = []
            current_segment: list = []
            for point in points:
                try:
                    x0, y0 = float(point[0]), float(point[1])
                except (TypeError, ValueError, IndexError):
                    if len(current_segment) >= 2:
                        segments.append(current_segment)
                    current_segment = []
                    continue
                in_partition = True
                if polygon:
                    in_partition = _point_in_polygon(x0, y0, polygon)
                if in_partition:
                    current_segment.append([x0, y0])
                else:
                    if len(current_segment) >= 2:
                        segments.append(current_segment)
                    current_segment = []
            if len(current_segment) >= 2:
                segments.append(current_segment)

            for segment in segments:
                if _looks_like_transfer(segment):
                    continue
                commands = []
                previous = None
                for point in segment:
                    gap = False
                    if previous is not None:
                        gap = math.hypot(float(point[0]) - float(previous[0]), float(point[1]) - float(previous[1])) > 2.5
                    command = "M" if (not commands or gap) else "L"
                    commands.append(f"{command} {px(point[0]):.1f} {py(point[1]):.1f}")
                    previous = point
                if commands:
                    official_svg_parts.append(
                        f'<path d="{" ".join(commands)}" fill="none" stroke="#ffffff" stroke-width="8" '
                        'stroke-linecap="round" stroke-linejoin="round" opacity=".38"/>'
                    )
        # Do not render server path groups as cut coverage. They are a
        # movement/path archive, not a reliable blade-on trail. The live trail
        # below is built only from mower-state==mowing samples and is persisted
        # across pause/return/dock for resume.
        official_trail_svg = ""
        # Keep the standalone camera entity complete as well as exposing the
        # smoother attribute-based overlays used by the dashboard card. Camera
        # consumers that do not use the custom card still need the live
        # blade-on trail and current mower pose baked into each SVG frame.
        trail_svg = (
            f'<path d="{trail_path}" fill="none" stroke="#ffffff" stroke-width="8" '
            'stroke-linecap="round" stroke-linejoin="round" opacity=".72"/>'
            if self._include_dynamic_overlays and trail_path
            else ""
        )
        marker_svg = ""
        if self._include_dynamic_overlays and mower is not None:
            marker_x = px(float(mower[0]))
            marker_y = py(float(mower[1]))
            marker_svg = (
                f'<g transform="translate({marker_x:.1f} {marker_y:.1f}) '
                # The marker artwork points upward, while calculated map
                # headings use zero degrees to the right (east).
                f'rotate({marker_rotation + 90.0:.1f})">'
                '<circle r="18" fill="#1f2933" stroke="#ffffff" stroke-width="2" '
                'stroke-opacity=".9"/>'
                '<rect x="-8" y="-11" width="16" height="22" rx="5" '
                'fill="#f4f7f9" stroke="#263746" stroke-width="2"/>'
                '<rect x="-5" y="-7" width="10" height="7" rx="2" fill="#ff7a1a"/>'
                '<circle cx="-10" cy="-7" r="2.5" fill="#151d24"/>'
                '<circle cx="-10" cy="7" r="2.5" fill="#151d24"/>'
                '<circle cx="10" cy="-7" r="2.5" fill="#151d24"/>'
                '<circle cx="10" cy="7" r="2.5" fill="#151d24"/>'
                '<path d="M 0 -16 L -4 -10 L 4 -10 Z" fill="#ffffff"/>'
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
            official_trail_svg,
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
