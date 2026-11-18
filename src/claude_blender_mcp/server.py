"""MCP server that lets Claude control Blender through the Claude Blender Bridge add-on."""

from __future__ import annotations

import base64
import itertools
import json
import os
import socket
from pathlib import Path
from typing import Any

try:  # mcp 2.x
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp import Image
    from mcp.server.fastmcp.exceptions import ToolError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
# Long enough for the user to click Allow (120 s) plus a slow command such as a video render.
SOCKET_TIMEOUT = 2000.0


class BlenderError(Exception):
    pass


class BlenderConnection:
    """Sends one JSON command per line to the Blender add-on and reads one JSON line back."""

    def __init__(self, host: str | None = None, port: int | None = None, timeout: float = SOCKET_TIMEOUT):
        self.host = host or os.environ.get("BLENDER_HOST", DEFAULT_HOST)
        self.port = int(port or os.environ.get("BLENDER_PORT", DEFAULT_PORT))
        self.timeout = timeout
        self._ids = itertools.count(1)

    def send(self, command: str, params: dict[str, Any] | None = None) -> Any:
        request = {"id": next(self._ids), "command": command, "params": params or {}}
        try:
            sock = socket.create_connection((self.host, self.port), timeout=5)
        except OSError as exc:
            raise BlenderError(
                f"Could not connect to Blender at {self.host}:{self.port} ({exc}). "
                "Ask the user to open Blender and make sure the 'Claude Blender Bridge' add-on is "
                "enabled and its server is running (3D Viewport > N sidebar > Claude > Start Server)."
            ) from exc
        with sock:
            sock.settimeout(self.timeout)
            sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            buffer = b""
            try:
                while b"\n" not in buffer:
                    chunk = sock.recv(1 << 20)
                    if not chunk:
                        raise BlenderError("Blender closed the connection without answering.")
                    buffer += chunk
            except socket.timeout as exc:
                raise BlenderError("Timed out waiting for Blender to answer.") from exc
        response = json.loads(buffer.split(b"\n", 1)[0])
        if response.get("status") != "ok":
            message = response.get("message", "Unknown error from Blender")
            if response.get("traceback") and "Traceback" not in message:
                message += "\n" + response["traceback"]
            raise BlenderError(message)
        return response.get("result")


INSTRUCTIONS = """\
These tools control the user's own Blender on their computer, through the
"Claude Blender Bridge" add-on.

Permission: the first command in a Blender session makes Blender show an
Allow / Deny question. If a tool says permission is pending or timed out,
tell the user to click Allow in Blender (3D Viewport > N sidebar > Claude)
and try again. If they denied it, stop and do not retry.

Building a model from a reference image:
1. Study the reference carefully: overall proportions, main parts, how the
   parts connect, colors and materials, and which details matter most.
   Describe your plan to the user in a few lines before you build.
2. Call get_scene_info. Ask before clearing a scene that already has work.
3. Block out the big shapes first with create_object, then refine. Use
   execute_blender_code (bpy/bmesh) for anything the simple tools cannot do:
   modifiers (bevel, subdivision, mirror, solidify, boolean), extrusions,
   curves, custom meshes. Name every object meaningfully. Blender units are
   meters and Z is up; -Y is the front.
4. After each major step, call get_viewport_screenshot (try views like
   front, right, top, iso) and compare against the reference. Fix
   proportions before adding detail.
5. To animate, set the range with set_animation_settings and pose objects,
   cameras or empties used as joints with set_keyframe on several frames.
6. Apply materials with set_material. Finish with a final screenshot and
   offer to save (save_blend_file) or export (export_model).
"""

mcp = MCPServer("blender", instructions=INSTRUCTIONS)
_blender = BlenderConnection()


def _call(command: str, **params: Any) -> Any:
    clean = {k: v for k, v in params.items() if v is not None}
    try:
        return _blender.send(command, clean)
    except BlenderError as exc:
        raise ToolError(str(exc)) from exc


def _text(result: Any) -> str:
    return json.dumps(result, indent=2)


@mcp.tool()
def blender_status() -> str:
    """Check that Blender is running and reachable, and whether the user has allowed Claude yet.
    This does not ask the user for permission."""
    return _text(_call("ping"))


@mcp.tool()
def get_scene_info() -> str:
    """List the objects in the current Blender scene with their type, location, rotation (degrees),
    scale, dimensions and materials."""
    return _text(_call("get_scene_info"))


@mcp.tool()
def get_object_info(name: str) -> str:
    """Detailed information about one object: bounding box, modifiers, materials, mesh counts."""
    return _text(_call("get_object_info", name=name))


@mcp.tool()
def create_object(
    type: str = "cube",
    name: str | None = None,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    scale: list[float] | None = None,
    size: float = 2.0,
    segments: int = 32,
    color: list[float] | None = None,
    metallic: float | None = None,
    roughness: float | None = None,
    text: str | None = None,
) -> str:
    """Create an object in Blender.

    type: cube, uv_sphere, ico_sphere, cylinder, cone, torus, plane, monkey, empty, text,
          camera, point_light, sun_light, area_light, spot_light.
    location: [x, y, z] in meters. rotation: [x, y, z] in degrees. scale: [x, y, z].
    size: overall size (diameter / edge length) before scaling. segments: roundness.
    color: [r, g, b] or [r, g, b, a], each 0-1 (linear). text: the words for a text object.
    """
    return _text(_call("create_object", type=type, name=name, location=location, rotation=rotation,
                       scale=scale, size=size, segments=segments, color=color, metallic=metallic,
                       roughness=roughness, text=text))


@mcp.tool()
def create_blocky_character(
    name: str = "Blocky",
    location: list[float] | None = None,
    facing: float = 0.0,
    height: float = 2.0,
    colors: dict[str, list[float]] | None = None,
    hair: str = "short",
    ears: bool = False,
    pose: dict[str, list[float]] | None = None,
) -> str:
    """Build a posable blocky (voxel-style) character with pixel-textured materials.
    colors: any of skin, hair, eyes, shirt, pants, shoes as [r, g, b] 0-1.
    hair: none, short, fluffy or long. facing: degrees around Z (0 faces -Y, the front view).
    pose: degrees [x, y, z] for neck, left_arm, right_arm, left_leg, right_leg
    (e.g. {"left_arm": [0, 175, 0]} raises the left arm). The joints it returns can be
    animated with set_keyframe. Add details (ties, belts, logos) with create_object or code."""
    return _text(_call("create_blocky_character", name=name, location=location, facing=facing, height=height,
                       colors=colors, hair=hair, ears=ears, pose=pose))


@mcp.tool()
def modify_object(
    name: str,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    scale: list[float] | None = None,
    new_name: str | None = None,
    visible: bool | None = None,
    parent: str | None = None,
) -> str:
    """Move, rotate (degrees), scale, rename, hide/show or parent an existing object.
    Pass parent="" to clear the parent."""
    return _text(_call("modify_object", name=name, location=location, rotation=rotation, scale=scale,
                       new_name=new_name, visible=visible, parent=parent))


@mcp.tool()
def set_material(
    object: str,
    color: list[float] | None = None,
    metallic: float | None = None,
    roughness: float | None = None,
    emission_strength: float | None = None,
    alpha: float | None = None,
    material_name: str | None = None,
) -> str:
    """Give an object a Principled BSDF material. color is [r, g, b] 0-1. Reuse a material across
    objects by passing the same material_name."""
    return _text(_call("set_material", object=object, color=color, metallic=metallic, roughness=roughness,
                       emission_strength=emission_strength, alpha=alpha, material_name=material_name))


@mcp.tool()
def delete_objects(names: list[str]) -> str:
    """Delete objects by name."""
    return _text(_call("delete_objects", names=names))


@mcp.tool()
def clear_scene(keep_cameras_and_lights: bool = True) -> str:
    """Delete every object in the scene (cameras and lights are kept unless told otherwise).
    Ask the user first if the scene has their own work in it."""
    return _text(_call("clear_scene", keep_cameras_and_lights=keep_cameras_and_lights))


@mcp.tool()
def execute_blender_code(code: str) -> str:
    """Run Python inside Blender. bpy, bmesh, mathutils and math are already imported.
    Use print() to report values, or set a variable named `result` to return it.
    Prefer bpy.data / bmesh over bpy.ops where possible; bpy.ops calls that need a
    3D Viewport may fail because the code runs from a timer."""
    return _text(_call("execute_code", code=code))


@mcp.tool()
def set_view(view: str | None = None, frame_all: bool = True, perspective: bool | None = None) -> str:
    """Point the 3D Viewport: view is front, back, left, right, top, bottom, iso or camera.
    frame_all zooms to fit everything. perspective False switches to orthographic."""
    return _text(_call("set_view", view=view, frame_all=frame_all, perspective=perspective))


@mcp.tool()
def set_keyframe(
    object: str,
    frame: int,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    scale: list[float] | None = None,
    interpolation: str | None = None,
) -> str:
    """Animate: put an object (or camera, or a pivot/empty used as a joint) at a pose on a frame and
    keyframe it. rotation is in degrees. interpolation: bezier (smooth), linear or constant.
    Blender plays the motion between keyframes."""
    return _text(_call("set_keyframe", object=object, frame=frame, location=location, rotation=rotation,
                       scale=scale, interpolation=interpolation))


@mcp.tool()
def set_animation_settings(
    frame_start: int | None = None,
    frame_end: int | None = None,
    fps: int | None = None,
    current_frame: int | None = None,
) -> str:
    """Set the animation's frame range and frames per second, or jump to a frame to see the pose there."""
    return _text(_call("set_animation_settings", frame_start=frame_start, frame_end=frame_end, fps=fps,
                       current_frame=current_frame))


@mcp.tool()
def clear_animation(object: str) -> str:
    """Remove all keyframes from an object."""
    return _text(_call("clear_animation", object=object))


@mcp.tool()
def get_viewport_screenshot(
    view: str | None = None,
    frame_all: bool = False,
    shading: str | None = None,
    max_size: int = 800,
) -> Image:
    """Take a picture of the 3D Viewport so you can compare the model with the reference.
    view: front, back, left, right, top, bottom, iso or camera (optional).
    shading: solid, material, rendered or wireframe (optional)."""
    result = _call("get_viewport_screenshot", view=view, frame_all=frame_all, shading=shading,
                   max_size=max_size)
    return Image(data=base64.b64decode(result["image_base64"]), format="png")


@mcp.tool()
def render_image(
    engine: str | None = None,
    resolution: list[int] | None = None,
    samples: int | None = None,
    camera: str | None = None,
    frame: int | None = None,
    save_path: str | None = None,
) -> Image:
    """Render the final picture through the scene camera and look at it.
    engine: eevee (fast), cycles (realistic, slower) or workbench (quick preview); default is the
    scene's engine. resolution: [width, height]. save_path also keeps the PNG on the user's computer.
    The user's own render settings are restored afterwards."""
    result = _call("render_image", engine=engine, resolution=resolution, samples=samples, camera=camera,
                   frame=frame, save_path=save_path)
    return Image(data=base64.b64decode(result["image_base64"]), format="png")


@mcp.tool()
def render_animation(
    path: str,
    engine: str | None = None,
    resolution: list[int] | None = None,
    samples: int | None = None,
    camera: str | None = None,
    frame_start: int | None = None,
    frame_end: int | None = None,
    fps: int | None = None,
) -> str:
    """Render the animation to an MP4 video file. This can take minutes: tell the user first, and
    keep resolution and samples modest (for example [1280, 720] and 16-32 samples)."""
    return _text(_call("render_animation", path=path, engine=engine, resolution=resolution, samples=samples,
                       camera=camera, frame_start=frame_start, frame_end=frame_end, fps=fps))


IMAGE_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}


@mcp.tool()
def view_image_file(path: str) -> Image:
    """Look at a reference image saved on the user's computer (png, jpg, gif or webp)."""
    file = Path(path).expanduser()
    fmt = IMAGE_FORMATS.get(file.suffix.lower())
    if fmt is None:
        raise ToolError(f"Unsupported image type {file.suffix!r}. Use png, jpg, gif or webp.")
    if not file.is_file():
        raise ToolError(f"File not found: {file}")
    return Image(data=file.read_bytes(), format=fmt)


@mcp.tool()
def add_reference_image(
    path: str,
    view: str = "front",
    size: float = 5.0,
    opacity: float = 0.5,
    location: list[float] | None = None,
) -> str:
    """Place a reference image in the scene behind the model so it can be traced.
    view: front, back, left, right, top or bottom (the view the picture was taken from).
    size: width of the image in meters."""
    return _text(_call("add_reference_image", path=path, view=view, size=size, opacity=opacity,
                       location=location))


@mcp.tool()
def export_model(path: str, format: str | None = None, selected_only: bool = False) -> str:
    """Export the scene to a file: glb, gltf, fbx, obj, stl or usd (taken from the extension if
    format is not given)."""
    return _text(_call("export_model", path=path, format=format, selected_only=selected_only))


@mcp.tool()
def save_blend_file(path: str | None = None) -> str:
    """Save the .blend file, optionally to a new path."""
    return _text(_call("save_blend_file", path=path))


@mcp.prompt()
def model_from_reference(description: str = "") -> str:
    """Build a 3D model in Blender from a reference image the user attached."""
    extra = f"\nNotes from the user: {description}" if description else ""
    return (
        "Build the object in the attached reference image as a 3D model in my Blender.\n"
        "First describe what you see: main parts, proportions, colors and materials. "
        "Then block out the large shapes, check with viewport screenshots from several angles, "
        "refine, add materials, and show me the result." + extra
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
