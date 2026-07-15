#!/usr/bin/env python3
"""A small, dependency-free MCP server for HiSilicon/Hipcam IP cameras.

Credentials are deliberately read only from environment variables.  The server
speaks MCP JSON-RPC over stdio, so it can be launched by Claude Desktop, Codex,
or another MCP host.
"""

import base64
import io
import json
import os
import sys
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


HOST = os.environ.get("CAMERA_HOST", "192.168.2.149")
USERNAME = os.environ.get("CAMERA_USERNAME", "admin")
PASSWORD = os.environ.get("CAMERA_PASSWORD")
HTTP_TIMEOUT = float(os.environ.get("CAMERA_TIMEOUT_SECONDS", "10"))
YOLO_MODEL = os.environ.get("CAMERA_YOLO_MODEL", "yolo11n.pt")
MAX_CENTERING_MOVES = 12
MIN_SEARCH_MOVE_STEPS = 4
PTZ_SETTLE_SECONDS = 2.0
PTZ_SETTLE_TIMEOUT_SECONDS = 8.0
PTZ_STABLE_FRAME_INTERVAL_SECONDS = 0.4
PTZ_SECONDS_PER_STEP = 0.25
_detector: Any | None = None


def camera_url(path: str, params: dict[str, str] | None = None) -> str:
    query = urllib.parse.urlencode(params or {})
    return f"http://{HOST}{path}" + (f"?{query}" if query else "")


def request(path: str, params: dict[str, str] | None = None) -> tuple[bytes, str]:
    if not PASSWORD:
        raise ValueError("CAMERA_PASSWORD is not configured")
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    req = urllib.request.Request(camera_url(path, params), headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            return response.read(), response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise ValueError("camera rejected the configured username or password") from error
        raise RuntimeError(f"camera returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"cannot connect to camera at {HOST}: {error.reason}") from error


def capture_rtsp_frame(quality: str) -> bytes:
    """Return one JPEG frame. Stream 11 is main and stream 12 is sub."""
    if not PASSWORD:
        raise ValueError("CAMERA_PASSWORD is not configured")
    stream = "11" if quality == "main" else "12"
    username = urllib.parse.quote(USERNAME, safe="")
    password = urllib.parse.quote(PASSWORD, safe="")
    rtsp_url = f"rtsp://{username}:{password}@{HOST}:554/{stream}"
    try:
        capture = subprocess.run(
            ["/usr/bin/ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", rtsp_url, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=HTTP_TIMEOUT + 10, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"could not capture an RTSP frame from stream /{stream}") from error
    if not capture.stdout:
        raise RuntimeError(f"stream /{stream} returned an empty frame")
    return capture.stdout


def move_ptz(direction: str, speed: int, steps: int) -> None:
    codes = {"up": "up", "down": "down", "left": "left", "right": "right", "home": "home"}
    if direction not in codes:
        raise ValueError("unsupported direction")
    if direction == "home":
        request("/cgi-bin/hi3510/ptzctrl.cgi", {"-step": "0", "-act": "home", "-speed": str(speed)})
        return
    # This firmware treats -step=0 as a continuous move. Bound every command
    # ourselves, then explicitly stop it before analysing another frame.
    request("/cgi-bin/hi3510/ptzctrl.cgi", {"-step": "0", "-act": codes[direction], "-speed": str(speed)})
    time.sleep(PTZ_SECONDS_PER_STEP * steps)
    request("/cgi-bin/hi3510/ptzctrl.cgi", {"-step": "0", "-act": "stop", "-speed": str(speed)})


def wait_for_ptz_settle() -> None:
    """Wait for consecutive stable video frames before running detection.

    The camera exposes no PTZ-position/status endpoint. If it remains in
    motion beyond the bounded timeout, issue its documented stop CGI before
    returning, so later detection never evaluates a moving scene.
    """
    from PIL import Image, ImageChops, ImageStat

    def grayscale_frame() -> Image.Image:
        return Image.open(io.BytesIO(capture_rtsp_frame("sub"))).convert("L").resize((80, 45))

    time.sleep(PTZ_SETTLE_SECONDS)
    previous = grayscale_frame()
    stable_pairs = 0
    deadline = time.monotonic() + PTZ_SETTLE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(PTZ_STABLE_FRAME_INTERVAL_SECONDS)
        current = grayscale_frame()
        difference = ImageStat.Stat(ImageChops.difference(previous, current)).mean[0]
        if difference < 3.0:
            stable_pairs += 1
            if stable_pairs >= 2:
                return
        else:
            stable_pairs = 0
        previous = current
    request("/cgi-bin/hi3510/ptzctrl.cgi", {"-step": "0", "-act": "stop", "-speed": "1"})
    time.sleep(PTZ_SETTLE_SECONDS)


def select_target(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(matches, key=lambda match: match["confidence"], default=None)


def target_offset(target: dict[str, Any]) -> tuple[float, float]:
    x1, y1, x2, y2 = target["box_xyxy"]
    width, height = target.get("image_size", [640, 360])
    dx = ((x1 + x2) / 2 / width) - 0.5
    dy = ((y1 + y2) / 2 / height) - 0.5
    target["center_offset"] = {"x": round(dx, 3), "y": round(dy, 3)}
    return dx, dy


def center_target(target: dict[str, Any], tolerance: float, speed: int, reverse_x: bool, reverse_y: bool) -> tuple[bool, str | None, float]:
    """Move one axis and return its pre-move error for feedback calibration."""
    dx, dy = target_offset(target)
    if abs(dx) <= tolerance and abs(dy) <= tolerance:
        return True, None, 0.0
    axis = "x" if abs(dx) >= abs(dy) else "y"
    error = dx if axis == "x" else dy
    if axis == "x":
        direction = "right" if error > 0 else "left"
        if reverse_x:
            direction = "left" if direction == "right" else "right"
    else:
        direction = "down" if error > 0 else "up"
        if reverse_y:
            direction = "up" if direction == "down" else "down"
    # Once a target exists, use one small axis-only correction per frame.
    move_ptz(direction, speed, 1)
    wait_for_ptz_settle()
    return False, axis, abs(error)


def detect_objects(jpeg: bytes, targets: list[str], colors: list[str], confidence: float) -> list[dict[str, Any]]:
    """Run YOLO lazily so ordinary camera tools have no ML startup cost."""
    global _detector
    try:
        import numpy as np
        from PIL import Image
        from ultralytics import YOLO
    except ImportError as error:
        raise RuntimeError("YOLO is not installed; install dependencies with: python3 -m pip install -r requirements.txt") from error
    if _detector is None:
        _detector = YOLO(YOLO_MODEL)  # Downloads the selected official weight file on first use.
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    result = _detector(image, conf=confidence, verbose=False)[0]
    if result.boxes is None:
        return []
    wanted = {target.lower() for target in targets}
    requested_colors = {color.lower() for color in colors}
    matches: list[dict[str, Any]] = []
    for box in result.boxes:
        class_id = int(box.cls[0].item())
        label = str(result.names[class_id])
        if wanted and label.lower() not in wanted:
            continue
        x1, y1, x2, y2 = (round(float(value), 1) for value in box.xyxy[0].tolist())
        # Use the interior of the detected object to reduce surrounding pavement
        # and background. This is an appearance filter, not a trained color model.
        width, height = x2 - x1, y2 - y1
        inset = image.crop((x1 + width * 0.15, y1 + height * 0.15, x2 - width * 0.15, y2 - height * 0.15))
        pixels = np.asarray(inset, dtype=np.float32)
        brightness = float(pixels.mean(axis=2).mean()) if pixels.size else 255.0
        dark_ratio = float((pixels.mean(axis=2) < 75).mean()) if pixels.size else 0.0
        appearance = "black" if brightness < 105 and dark_ratio > 0.35 else "not_black"
        if requested_colors and appearance not in requested_colors:
            continue
        matches.append({"label": label, "appearance": appearance, "appearance_metrics": {"mean_brightness": round(brightness, 1), "dark_pixel_ratio": round(dark_ratio, 3)}, "confidence": round(float(box.conf[0].item()), 3), "box_xyxy": [x1, y1, x2, y2], "image_size": [image.width, image.height]})
    return matches


def detect_current_view(targets: list[str], colors: list[str], confidence: float, attempts: int = 3) -> list[dict[str, Any]]:
    """Require only one hit across a few frames to handle low-light variation."""
    for attempt in range(attempts):
        matches = detect_objects(capture_rtsp_frame("sub"), targets, colors, confidence)
        if matches:
            return matches
        if attempt + 1 < attempts:
            time.sleep(0.15)
    return []


TOOLS: list[dict[str, Any]] = [
    {"name": "camera_info", "description": "Read the camera model, hardware version, firmware, and storage status.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "snapshot", "description": "Capture a current JPEG still image from the selected RTSP stream.", "inputSchema": {"type": "object", "properties": {"quality": {"type": "string", "enum": ["main", "sub"], "default": "sub", "description": "Main is stream /11; sub is stream /12."}}}},
    {"name": "rtsp_url", "description": "Return an RTSP stream URL without exposing credentials. Use it with a client configured with the same camera credentials if requested.", "inputSchema": {"type": "object", "properties": {"quality": {"type": "string", "enum": ["main", "sub"], "default": "sub"}}}},
    {"name": "ptz_move", "description": "Move a supported PTZ camera by a bounded number of steps. Large moves up to 30 steps are allowed for broad visual coverage; every directional move is followed by an explicit stop. This changes the physical camera position.", "inputSchema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down", "left", "right", "home"]}, "speed": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}, "steps": {"type": "integer", "minimum": 1, "maximum": 30, "default": 1}}, "required": ["direction"]}},
    {"name": "patrol_detect", "description": "Check the current view, then execute a bounded large-step PTZ search plan only if no target is found. Broad searches of up to 24 moves and 30 steps per move are allowed. On detection it changes to single-step, axis-by-axis feedback moves until the target is near image centre, then returns a high-resolution image. It does not continuously track moving objects.", "inputSchema": {"type": "object", "properties": {"targets": {"type": "array", "items": {"type": "string"}, "default": ["person", "car", "truck", "bus", "motorcycle", "bicycle", "cat", "dog"]}, "colors": {"type": "array", "items": {"type": "string", "enum": ["black"]}, "default": []}, "moves": {"type": "array", "items": {"type": "string", "enum": ["up", "down", "left", "right"]}, "maxItems": 24, "default": ["left", "left", "left", "left"], "description": "Search plan used only when current view has no match. Each search move is large."}, "speed": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3}, "steps_per_move": {"type": "integer", "minimum": 1, "maximum": 30, "default": 2, "description": "Requested search-move size; the server enforces a minimum large move."}, "confidence": {"type": "number", "minimum": 0.1, "maximum": 0.95, "default": 0.5}, "center_tolerance": {"type": "number", "minimum": 0.03, "maximum": 0.3, "default": 0.12}}, "required": ["targets", "moves"]}},
]


def text_result(value: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}]}



def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "camera_info":
        body, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getserverinfo"})
        return text_result(body.decode("utf-8", errors="replace"))
    if name == "snapshot":
        jpeg = capture_rtsp_frame(arguments.get("quality", "sub"))
        return {"content": [{"type": "image", "data": base64.b64encode(jpeg).decode(), "mimeType": "image/jpeg"}]}
    if name == "rtsp_url":
        stream = "11" if arguments.get("quality", "sub") == "main" else "12"
        return text_result(f"rtsp://{HOST}:554/{stream}")
    if name == "ptz_move":
        direction = arguments["direction"]
        speed = int(arguments.get("speed", 5))
        steps = int(arguments.get("steps", 1))
        if not 1 <= speed <= 10:
            raise ValueError("speed must be between 1 and 10")
        if not 1 <= steps <= 30:
            raise ValueError("steps must be between 1 and 30")
        move_ptz(direction, speed, steps)
        return text_result("PTZ command sent.")
    if name == "patrol_detect":
        targets = arguments.get("targets", ["person", "car", "truck", "bus", "motorcycle", "bicycle", "cat", "dog"])
        colors = arguments.get("colors", [])
        moves = arguments.get("moves", ["left", "left", "left", "left"])
        if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
            raise ValueError("targets must be an array of YOLO class names")
        if not isinstance(colors, list) or not all(color == "black" for color in colors):
            raise ValueError("colors may contain only the supported appearance filter: black")
        if not isinstance(moves, list) or len(moves) > 24 or not all(move in {"up", "down", "left", "right"} for move in moves):
            raise ValueError("moves must contain at most 24 up/down/left/right values")
        confidence = float(arguments.get("confidence", 0.5))
        if not 0.1 <= confidence <= 0.95:
            raise ValueError("confidence must be between 0.1 and 0.95")
        speed, steps = int(arguments.get("speed", 3)), int(arguments.get("steps_per_move", 2))
        if not 1 <= speed <= 10 or not 1 <= steps <= 30:
            raise ValueError("speed must be between 1 and 10; steps_per_move must be between 1 and 30")
        center_tolerance = float(arguments.get("center_tolerance", 0.12))
        if not 0.03 <= center_tolerance <= 0.3:
            raise ValueError("center_tolerance must be 0.03–0.3")
        positions = ["current view", *[f"after {move} move {index + 1}" for index, move in enumerate(moves)]]
        for index, position in enumerate(positions):
            matches = detect_current_view(targets, colors, confidence)
            if matches:
                target = select_target(matches)
                assert target is not None
                centered = False
                adjustments = 0
                reverse_x = False
                reverse_y = False
                while adjustments < MAX_CENTERING_MOVES:
                    centered, axis, previous_error = center_target(target, center_tolerance, speed, reverse_x, reverse_y)
                    if centered:
                        centered = True
                        break
                    adjustments += 1
                    refreshed = detect_current_view(targets, colors, confidence)
                    target = select_target(refreshed)
                    if target is None:
                        break
                    new_dx, new_dy = target_offset(target)
                    new_error = abs(new_dx if axis == "x" else new_dy)
                    if abs(new_dx) <= center_tolerance and abs(new_dy) <= center_tolerance:
                        centered = True
                        break
                    # If the selected-axis error did not shrink, this firmware's
                    # movement direction is opposite our initial mapping. Flip
                    # that mapping for the remaining bounded adjustments.
                    if new_error >= previous_error * 0.98:
                        if axis == "x":
                            reverse_x = not reverse_x
                        else:
                            reverse_y = not reverse_y
                if target is None:
                    return text_result(json.dumps({"found": False, "target_lost_during_centering": True, "views_checked": index + 1}, ensure_ascii=False))
                high_res = capture_rtsp_frame("main")
                summary = {"found": True, "position": position, "object": target, "centered": centered, "centering_adjustments": adjustments, "direction_calibration": {"horizontal_reversed": reverse_x, "vertical_reversed": reverse_y}, "camera_locked_on_position": True, "note": "The scan stopped at its final centering position."}
                return {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}, {"type": "image", "data": base64.b64encode(high_res).decode(), "mimeType": "image/jpeg"}]}
            if index < len(moves):
                # No target in this view: cover ground quickly before sampling again.
                move_ptz(moves[index], speed, max(MIN_SEARCH_MOVE_STEPS, steps))
                wait_for_ptz_settle()
        return text_result(json.dumps({"found": False, "views_checked": len(positions), "camera_locked_on_position": False}, ensure_ascii=False))
    raise ValueError(f"unknown tool: {name}")


def respond(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return
    try:
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "hisilicon-ip-camera", "version": "1.1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params", {})
            result = call_tool(params["name"], params.get("arguments", {}))
        else:
            raise ValueError(f"unsupported method: {method}")
        respond({"jsonrpc": "2.0", "id": request_id, "result": result})
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        respond({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(error)}], "isError": True}})


def main() -> None:
    for line in sys.stdin:
        try:
            handle(json.loads(line))
        except json.JSONDecodeError:
            continue


if __name__ == "__main__":
    main()
