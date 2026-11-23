"""Codex에 주는 지시문(PROMPT)과 출력 스키마(ANALYSIS_SCHEMA). 측정값의 이름과 뜻을 여기서 정한다."""

from __future__ import annotations


def _num(desc: str) -> dict:
    return {"type": "number", "description": desc}


_PT = {"type": "object", "additionalProperties": False, "required": ["x", "y"],
       "properties": {"x": {"type": "number"}, "y": {"type": "number"}}}

ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["camera", "room", "walls", "objects", "sun"],
    "properties": {
        "camera": {
            "type": "object", "additionalProperties": False,
            "required": ["vfov_deg", "pitch_deg", "roll_deg", "horizon_y_at_left", "horizon_y_at_right", "height_m"],
            "properties": {
                "vfov_deg": _num("vertical field of view in degrees"),
                "pitch_deg": _num("camera pitch in degrees, negative = looking down"),
                "roll_deg": _num("camera roll in degrees, positive = horizon lower on the right side"),
                "horizon_y_at_left": _num("pixel y of the horizon line at x = 0"),
                "horizon_y_at_right": _num("pixel y of the horizon line at x = image width"),
                "height_m": _num("camera height above the floor in meters"),
            },
        },
        "room": {
            "type": "object", "additionalProperties": False,
            "required": ["floor_color", "wall_color", "background_color", "width_m", "depth_m", "height_m"],
            "properties": {
                "floor_color": {"type": "string"}, "wall_color": {"type": "string"},
                "background_color": {"type": "string"},
                "width_m": {"type": "number"}, "depth_m": {"type": "number"}, "height_m": {"type": "number"},
            },
        },
        "walls": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["side", "bottom_edge", "top_edge"],
                "properties": {
                    "side": {"type": "string", "enum": ["back", "left", "right"]},
                    "bottom_edge": {"type": "array", "items": _PT},
                    "top_edge": {"type": "array", "items": _PT},
                },
            },
        },
        "objects": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["shape", "color", "bbox", "bottom_corners", "width_m", "depth_m", "height_m"],
                "properties": {
                    "shape": {"type": "string", "enum": ["box", "cylinder"]},
                    "color": {"type": "string"},
                    "bbox": {"type": "object", "additionalProperties": False,
                             "required": ["left", "top", "right", "bottom"],
                             "properties": {k: {"type": "number"} for k in ("left", "top", "right", "bottom")}},
                    "bottom_corners": {"type": "array", "items": _PT},
                    "width_m": {"type": "number"}, "depth_m": {"type": "number"}, "height_m": {"type": "number"},
                },
            },
        },
        "sun": {
            "type": "object", "additionalProperties": False, "required": ["x", "y", "z"],
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
        },
    },
}

PROMPT = """\
You are the camera-matching step of a 3D previsualization tool. Look carefully at the attached \
image ({W}x{H} pixels). It shows an interior: a floor, walls (a back wall facing the camera and \
possibly left/right side walls; above the walls there may be plain background instead of a ceiling), \
and a few main solid objects standing on the floor. Each main object can be approximated by one \
box or one upright cylinder.

Report measurements as JSON following the output schema. Pixel coordinates: x from the left edge \
(0..{W}), y from the top edge (0..{H}). Values may lie outside the image when you extrapolate. \
Be precise: the 3D scene is reconstructed from your pixel coordinates by exact geometry.

camera
- vfov_deg: vertical field of view in degrees. Judge it from perspective: how strongly the \
floor/wall edges that run away from the camera converge, how quickly things shrink with distance, \
how foreshortened the floor and the tops of objects are. Narrow lens ~30-45, normal ~50-60, wide ~65-80.
- horizon_y_at_left, horizon_y_at_right: y of the horizon (eye-level line = vanishing line of the \
floor; edges between the floor and the side walls, and the top edges of the side walls, converge on \
it) at x=0 and at x={W}. They differ when the camera is rolled.
- pitch_deg: negative when looking down (horizon above the image center), positive when looking up.
- roll_deg: positive when the horizon is lower on the right side.
- height_m: camera height above the floor in meters (typically 1.0-1.8).

room: floor_color, wall_color, background_color (the area above the walls) as #rrggbb, and rough \
room size in meters (width_m left-right, depth_m front-back, height_m wall height).

walls: one entry per visible wall (side = back, left or right).
- bottom_edge: two points on the line where that wall meets the floor, far apart. Extrapolate \
the line behind objects or beyond the image border if necessary.
- top_edge: two points on the top edge of that wall if it is visible, otherwise [].

objects: every main object standing on the floor, nearest first. Ignore shadows, floor/wall \
texture, image noise and tiny items. Never list walls, floor or shadows as objects.
- shape: "box" or "cylinder" (upright).
- color: the base color of the object as #rrggbb (its lit side, not the shaded side).
- bbox: tight pixel bounding box of the whole object (left, top, right, bottom). If part of it is \
hidden behind another object, estimate the full extent. If it is cut off by the image border, use the border.
- bottom_corners: for boxes, the image points where each visible vertical edge meets the floor, \
left to right (usually 2 or 3 points). For cylinders: [].
- width_m, depth_m, height_m: estimated real size in meters, consistent with the camera height.

sun: unit vector pointing from the scene toward the main light, in camera-aligned axes \
(x = right, y = up, z = toward the camera). Infer it from shading and shadows.
"""
