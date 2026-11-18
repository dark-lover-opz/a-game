"""Claude Blender Bridge - lets Claude build 3D models in your own Blender.

The add-on runs a small server on 127.0.0.1 (your computer only). The
Claude Blender MCP server connects to it and sends commands such as
"create a cube" or "take a viewport screenshot". The first time Claude
connects in a Blender session, Blender asks you to Allow or Deny it.
Nothing runs until you click Allow.
"""

bl_info = {
    "name": "Claude Blender Bridge",
    "author": "a-game contributors",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > Claude",
    "description": "Let Claude build models in Blender from your reference images",
    "category": "Interface",
}

import base64
import contextlib
import io
import json
import math
import os
import queue
import socket
import tempfile
import threading
import time
import traceback

import bmesh
import bpy
import mathutils

HOST = "127.0.0.1"
DEFAULT_PORT = 9876
PERMISSION_TIMEOUT = 120.0  # seconds a request waits for you to click Allow
JOB_TIMEOUT = 600.0  # seconds a single command may take
TICK = 0.05

# Commands that are answered without asking for permission. They do not
# read or change your scene.
NO_PERMISSION_NEEDED = {"ping"}


# ---------------------------------------------------------------------------
# Shared state between the network thread and Blender's main thread
# ---------------------------------------------------------------------------


class _Job:
    __slots__ = ("command", "params", "created", "done", "response", "cancelled")

    def __init__(self, command, params):
        self.command = command
        self.params = params
        self.created = time.monotonic()
        self.done = threading.Event()
        self.response = None
        self.cancelled = False

    def finish(self, response):
        self.response = response
        self.done.set()


class _State:
    server_socket = None
    server_thread = None
    running = False
    port = DEFAULT_PORT
    jobs = queue.Queue()
    waiting = []  # jobs waiting for the user's permission
    # "unknown" -> nobody asked yet, "pending" -> waiting for a click,
    # "allowed" / "denied" -> the user's answer for this Blender session.
    permission = "unknown"
    log = []


def _log(message):
    line = time.strftime("%H:%M:%S ") + message
    _State.log.append(line)
    del _State.log[:-20]
    print("[Claude Bridge]", message)


def _prefs():
    addon = bpy.context.preferences.addons.get(__name__)
    return addon.preferences if addon else None


def _always_allowed():
    prefs = _prefs()
    return bool(prefs and prefs.always_allow)


def _redraw():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            area.tag_redraw()


# ---------------------------------------------------------------------------
# Network server (runs in a background thread, never touches bpy)
# ---------------------------------------------------------------------------


def _serve(server_socket):
    while _State.running:
        try:
            client, _addr = server_socket.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_handle_client, args=(client,), daemon=True).start()


def _handle_client(client):
    buffer = b""
    try:
        client.settimeout(None)
        while _State.running:
            chunk = client.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                response = _dispatch_line(line)
                client.sendall(json.dumps(response).encode("utf-8") + b"\n")
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            client.close()


def _dispatch_line(line):
    try:
        request = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {"status": "error", "message": "Invalid JSON: %s" % exc}
    request_id = request.get("id")
    job = _Job(request.get("command", ""), request.get("params") or {})
    _State.jobs.put(job)
    if not job.done.wait(JOB_TIMEOUT + PERMISSION_TIMEOUT):
        job.cancelled = True
        response = {"status": "error", "message": "Blender did not answer in time."}
    else:
        response = job.response
    response["id"] = request_id
    return response


def start_server(port=None):
    if _State.running:
        return
    port = port or DEFAULT_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    sock.listen(5)
    sock.settimeout(1.0)
    _State.server_socket = sock
    _State.port = port
    _State.running = True
    _State.server_thread = threading.Thread(target=_serve, args=(sock,), daemon=True)
    _State.server_thread.start()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=TICK, persistent=True)
    _log("Server started (port %d)" % port)


def stop_server():
    if not _State.running:
        return
    _State.running = False
    with contextlib.suppress(OSError):
        _State.server_socket.close()
    _State.server_socket = None
    _fail_waiting("The Blender server was stopped.")
    _log("Server stopped")


# ---------------------------------------------------------------------------
# Main-thread processing (bpy is only safe to use here)
# ---------------------------------------------------------------------------


def _tick():
    try:
        _process_jobs()
    except Exception:  # never let the timer die
        traceback.print_exc()
    return TICK if _State.running or not _State.jobs.empty() else None


def _process_jobs():
    while True:
        try:
            job = _State.jobs.get_nowait()
        except queue.Empty:
            break
        if job.cancelled:
            continue
        if job.command in NO_PERMISSION_NEEDED:
            job.finish(run_command(job.command, job.params))
            continue
        decision = "allowed" if _always_allowed() else _State.permission
        if decision == "allowed":
            job.finish(run_command(job.command, job.params))
        elif decision == "denied":
            job.finish(_denied_response())
        else:
            _State.waiting.append(job)
            if _State.permission == "unknown":
                _ask_permission()

    now = time.monotonic()
    still_waiting = []
    for job in _State.waiting:
        if job.cancelled:
            continue
        if now - job.created > PERMISSION_TIMEOUT:
            job.finish({
                "status": "error",
                "code": "permission_timeout",
                "message": "Nobody clicked Allow in Blender. Ask the user to open Blender "
                "and click Allow (View3D sidebar > Claude), then try again.",
            })
        else:
            still_waiting.append(job)
    _State.waiting = still_waiting
    if not still_waiting and _State.permission == "pending":
        # Every request gave up waiting. Ask again next time.
        _State.permission = "unknown"
        _redraw()


def _denied_response():
    return {
        "status": "error",
        "code": "permission_denied",
        "message": "The user denied Claude access to Blender. They can change this in "
        "Blender: View3D sidebar > Claude > Reset permission.",
    }


def _ask_permission():
    _State.permission = "pending"
    _log("Claude asked for permission")
    _redraw()
    wm = bpy.context.window_manager
    if bpy.app.background or wm is None or not wm.windows:
        return  # no screen to show a dialog on: the sidebar panel / Python API decide instead
    window = wm.windows[0]
    try:
        with bpy.context.temp_override(window=window, screen=window.screen):
            bpy.ops.claude_bridge.ask("INVOKE_DEFAULT")
    except Exception:
        traceback.print_exc()


def set_permission(decision, always=False):
    """decision is "allowed" or "denied"."""
    _State.permission = decision
    if always and decision == "allowed":
        prefs = _prefs()
        if prefs:
            prefs.always_allow = True
            with contextlib.suppress(Exception):
                bpy.ops.wm.save_userpref()
    _log("Permission %s%s" % (decision, " (always)" if always else ""))
    waiting, _State.waiting = _State.waiting, []
    for job in waiting:
        if job.cancelled:
            continue
        if decision == "allowed":
            job.finish(run_command(job.command, job.params))
        else:
            job.finish(_denied_response())
    _redraw()


def reset_permission():
    _State.permission = "unknown"
    prefs = _prefs()
    if prefs and prefs.always_allow:
        prefs.always_allow = False
        with contextlib.suppress(Exception):
            bpy.ops.wm.save_userpref()
    _log("Permission reset")
    _redraw()


def _fail_waiting(message):
    waiting, _State.waiting = _State.waiting, []
    for job in waiting:
        job.finish({"status": "error", "message": message})


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run_command(command, params):
    handler = COMMANDS.get(command)
    if handler is None:
        return {"status": "error", "message": "Unknown command: %r. Known: %s" % (command, ", ".join(sorted(COMMANDS)))}
    try:
        result = handler(**params)
        if command not in NO_PERMISSION_NEEDED:
            _log("Ran " + command)
        return {"status": "ok", "result": result}
    except Exception as exc:
        return {"status": "error", "message": "%s: %s" % (type(exc).__name__, exc), "traceback": traceback.format_exc()}


def _r(values, digits=4):
    return [round(float(v), digits) for v in values]


def _get_object(name):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError("No object named %r" % name)
    return obj


def _object_summary(obj):
    return {
        "name": obj.name,
        "type": obj.type,
        "location": _r(obj.location),
        "rotation_degrees": _r((math.degrees(a) for a in obj.rotation_euler), 2),
        "scale": _r(obj.scale),
        "dimensions": _r(obj.dimensions),
        "parent": obj.parent.name if obj.parent else None,
        "materials": [slot.material.name for slot in obj.material_slots if slot.material],
        "visible": obj.visible_get() if hasattr(obj, "visible_get") else True,
    }


def cmd_ping():
    return {
        "blender_version": bpy.app.version_string,
        "addon_version": ".".join(str(v) for v in bl_info["version"]),
        "permission": "allowed" if _always_allowed() else _State.permission,
        "file": bpy.data.filepath or None,
    }


def cmd_get_scene_info(limit=200):
    scene = bpy.context.scene
    objects = list(scene.objects)
    return {
        "scene": scene.name,
        "file": bpy.data.filepath or None,
        "unit_system": scene.unit_settings.system,
        "frame": scene.frame_current,
        "object_count": len(objects),
        "objects": [_object_summary(o) for o in objects[:limit]],
        "truncated": len(objects) > limit,
        "materials": [m.name for m in bpy.data.materials],
    }


def _material_info(mat):
    info = {"name": mat.name}
    bsdf = _principled(mat, create=False)
    if bsdf is not None:
        for key, socket_name in (("base_color", "Base Color"), ("metallic", "Metallic"), ("roughness", "Roughness")):
            sock = bsdf.inputs.get(socket_name)
            if sock is not None:
                value = sock.default_value
                info[key] = _r(value) if hasattr(value, "__len__") else round(float(value), 4)
    return info


def cmd_get_object_info(name):
    obj = _get_object(name)
    info = _object_summary(obj)
    info["bounding_box_world"] = [_r(obj.matrix_world @ mathutils.Vector(c)) for c in obj.bound_box]
    info["modifiers"] = [{"name": m.name, "type": m.type} for m in obj.modifiers]
    info["material_details"] = [_material_info(s.material) for s in obj.material_slots if s.material]
    info["children"] = [c.name for c in obj.children]
    if obj.type == "MESH":
        mesh = obj.data
        info["mesh"] = {"vertices": len(mesh.vertices), "edges": len(mesh.edges), "faces": len(mesh.polygons)}
    return info


def _link(obj):
    collection = bpy.context.collection or bpy.context.scene.collection
    collection.objects.link(obj)
    return obj


def _set_transform(obj, location=None, rotation=None, scale=None):
    if location is not None:
        obj.location = location
    if rotation is not None:
        obj.rotation_euler = [math.radians(a) for a in rotation]
    if scale is not None:
        obj.scale = [scale] * 3 if isinstance(scale, (int, float)) else scale
    bpy.context.view_layer.update()  # so dimensions and matrices are current


def _mesh_from_bmesh(name, build):
    bm = bmesh.new()
    try:
        build(bm)
        mesh = bpy.data.meshes.new(name)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    return mesh


PRIMITIVES = ("cube", "uv_sphere", "ico_sphere", "cylinder", "cone", "torus", "plane", "monkey")


def _build_primitive(kind, name, size, segments):
    half = size / 2.0
    if kind == "cube":
        return _mesh_from_bmesh(name, lambda bm: bmesh.ops.create_cube(bm, size=size))
    if kind == "uv_sphere":
        return _mesh_from_bmesh(name, lambda bm: bmesh.ops.create_uvsphere(
            bm, u_segments=segments, v_segments=max(3, segments // 2), radius=half))
    if kind == "ico_sphere":
        return _mesh_from_bmesh(name, lambda bm: bmesh.ops.create_icosphere(
            bm, subdivisions=max(1, min(6, segments // 8)), radius=half))
    if kind in ("cylinder", "cone"):
        top = half if kind == "cylinder" else 0.0
        return _mesh_from_bmesh(name, lambda bm: bmesh.ops.create_cone(
            bm, cap_ends=True, cap_tris=False, segments=segments, radius1=half, radius2=top, depth=size))
    if kind == "plane":
        return _mesh_from_bmesh(name, lambda bm: bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=half))
    if kind == "monkey":
        def build(bm):
            bmesh.ops.create_monkey(bm)
            bmesh.ops.scale(bm, vec=(half, half, half), verts=bm.verts)
        return _mesh_from_bmesh(name, build)
    if kind == "torus":
        major, minor = half * 0.75, half * 0.25
        minor_segments = max(3, segments // 2)

        def build(bm):
            rings = []
            for i in range(segments):
                u = 2 * math.pi * i / segments
                ring = []
                for j in range(minor_segments):
                    v = 2 * math.pi * j / minor_segments
                    r = major + minor * math.cos(v)
                    ring.append(bm.verts.new((r * math.cos(u), r * math.sin(u), minor * math.sin(v))))
                rings.append(ring)
            for i in range(segments):
                a, b = rings[i], rings[(i + 1) % segments]
                for j in range(minor_segments):
                    k = (j + 1) % minor_segments
                    bm.faces.new((a[j], b[j], b[k], a[k]))
        return _mesh_from_bmesh(name, build)
    raise ValueError("Unknown primitive %r" % kind)


def cmd_create_object(type="cube", name=None, location=(0, 0, 0), rotation=None, scale=None,
                      size=2.0, segments=32, color=None, metallic=None, roughness=None, text=None):
    kind = type.lower()
    name = name or kind.replace("_", " ").title().replace(" ", "")
    if kind in PRIMITIVES:
        mesh = _build_primitive(kind, name, float(size), max(3, int(segments)))
        obj = bpy.data.objects.new(name, mesh)
        if kind in ("uv_sphere", "ico_sphere", "cylinder", "cone", "torus", "monkey"):
            for poly in mesh.polygons:
                poly.use_smooth = kind != "cylinder" and kind != "cone"
    elif kind == "empty":
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_size = float(size) / 2
    elif kind == "text":
        curve = bpy.data.curves.new(name, "FONT")
        curve.body = text or name
        curve.size = float(size) / 2
        obj = bpy.data.objects.new(name, curve)
    elif kind == "camera":
        obj = bpy.data.objects.new(name, bpy.data.cameras.new(name))
        if bpy.context.scene.camera is None:
            bpy.context.scene.camera = obj
    elif kind in ("point_light", "sun_light", "area_light", "spot_light"):
        light = bpy.data.lights.new(name, kind.split("_")[0].upper())
        light.energy = 3.0 if kind == "sun_light" else 1000.0
        obj = bpy.data.objects.new(name, light)
    else:
        raise ValueError("Unknown type %r. Use one of: %s, empty, text, camera, point_light, sun_light, "
                         "area_light, spot_light" % (type, ", ".join(PRIMITIVES)))
    _link(obj)
    _set_transform(obj, location, rotation, scale)
    if color is not None and obj.type in ("MESH", "CURVE", "FONT", "SURFACE", "META"):
        cmd_set_material(obj.name, color=color, metallic=metallic, roughness=roughness)
    return _object_summary(obj)


def cmd_modify_object(name, location=None, rotation=None, scale=None, new_name=None, visible=None, parent=None):
    obj = _get_object(name)
    _set_transform(obj, location, rotation, scale)
    if new_name:
        obj.name = new_name
    if visible is not None:
        obj.hide_viewport = not visible
        obj.hide_render = not visible
    if parent is not None:
        obj.parent = _get_object(parent) if parent else None
    return _object_summary(obj)


def _principled(mat, create=True):
    if create and bpy.app.version < (5, 0, 0) and not mat.use_nodes:
        with contextlib.suppress(Exception):
            mat.use_nodes = True
    if mat.node_tree is None:
        return None
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    if not create:
        return None
    nodes = mat.node_tree.nodes
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    output = next((n for n in nodes if n.type == "OUTPUT_MATERIAL"), None) or nodes.new("ShaderNodeOutputMaterial")
    mat.node_tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    return bsdf


def cmd_set_material(object, color=None, metallic=None, roughness=None, emission_strength=None,
                     material_name=None, alpha=None):
    obj = _get_object(object)
    if obj.data is None or not hasattr(obj.data, "materials"):
        raise ValueError("%s (%s) cannot have materials" % (obj.name, obj.type))
    mat = None
    if material_name:
        mat = bpy.data.materials.get(material_name)
    elif obj.material_slots and obj.material_slots[0].material:
        mat = obj.material_slots[0].material
    if mat is None:
        mat = bpy.data.materials.new(material_name or obj.name + "_Material")
    if not obj.data.materials:
        obj.data.materials.append(mat)
    else:
        obj.data.materials[0] = mat
    bsdf = _principled(mat)
    rgba = None
    if color is not None:
        rgba = list(color) + [1.0] * (4 - len(color))
        mat.diffuse_color = rgba
    if bsdf is not None:
        inputs = bsdf.inputs
        if rgba is not None:
            inputs["Base Color"].default_value = rgba
        if metallic is not None:
            inputs["Metallic"].default_value = float(metallic)
        if roughness is not None:
            inputs["Roughness"].default_value = float(roughness)
        if alpha is not None:
            inputs["Alpha"].default_value = float(alpha)
        if emission_strength is not None:
            emit = inputs.get("Emission Color") or inputs.get("Emission")
            if emit is not None and rgba is not None:
                emit.default_value = rgba
            if inputs.get("Emission Strength") is not None:
                inputs["Emission Strength"].default_value = float(emission_strength)
    return _material_info(mat)


def cmd_delete_objects(names):
    if isinstance(names, str):
        names = [names]
    deleted, missing = [], []
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            missing.append(name)
            continue
        bpy.data.objects.remove(obj, do_unlink=True)
        deleted.append(name)
    return {"deleted": deleted, "missing": missing}


def cmd_clear_scene(keep_cameras_and_lights=True):
    keep = {"CAMERA", "LIGHT"} if keep_cameras_and_lights else set()
    names = [o.name for o in bpy.context.scene.objects if o.type not in keep]
    result = cmd_delete_objects(names)
    for collection in (bpy.data.meshes, bpy.data.materials, bpy.data.curves):
        for block in list(collection):
            if block.users == 0:
                collection.remove(block)
    return result


def cmd_execute_code(code):
    namespace = {"bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math, "__name__": "__claude__"}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(code, "<claude>", "exec"), namespace)
    except Exception:
        raise RuntimeError("The code failed.\n--- printed output ---\n%s\n--- error ---\n%s"
                           % (out.getvalue()[-4000:], traceback.format_exc(limit=5)))
    result = {"output": out.getvalue()[-20000:]}
    if "result" in namespace:
        try:
            json.dumps(namespace["result"])
            result["result"] = namespace["result"]
        except (TypeError, ValueError):
            result["result"] = repr(namespace["result"])
    return result


VIEWS = {"front": "FRONT", "back": "BACK", "left": "LEFT", "right": "RIGHT", "top": "TOP", "bottom": "BOTTOM"}


def _find_view3d():
    wm = bpy.context.window_manager
    if wm is None:
        return None
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return window, area, region
    return None


def cmd_set_view(view=None, frame_all=True, perspective=None):
    found = _find_view3d()
    if found is None:
        raise RuntimeError("No 3D Viewport is open in Blender.")
    window, area, region = found
    space = area.spaces.active
    view_prefs = bpy.context.preferences.view
    smooth, view_prefs.smooth_view = view_prefs.smooth_view, 0  # jump, don't animate
    try:
        _set_view(window, area, region, space, view, frame_all, perspective)
    finally:
        view_prefs.smooth_view = smooth
    return {"view": view, "perspective": space.region_3d.view_perspective}


def _set_view(window, area, region, space, view, frame_all, perspective):
    with bpy.context.temp_override(window=window, area=area, region=region):
        if view:
            view = view.lower()
            if view == "camera":
                if bpy.context.scene.camera is None:
                    raise ValueError("The scene has no camera.")
                space.region_3d.view_perspective = "CAMERA"  # view_camera() would toggle
            elif view in VIEWS:
                bpy.ops.view3d.view_axis(type=VIEWS[view])
            elif view == "iso":
                r3d = space.region_3d
                r3d.view_rotation = mathutils.Euler((math.radians(60), 0, math.radians(45))).to_quaternion()
                r3d.view_perspective = "PERSP"
            else:
                raise ValueError("view must be one of: %s, camera, iso" % ", ".join(VIEWS))
        if perspective is not None:
            space.region_3d.view_perspective = "PERSP" if perspective else "ORTHO"
        if frame_all and view != "camera":
            bpy.ops.view3d.view_all(center=False)


def _capture_offscreen(area, region, path, max_size):
    """Draw the viewport into an off-screen image. Works even when Blender is covered or
    minimized, and on systems where reading the screen back returns a black image."""
    import gpu

    space = area.spaces.active
    r3d = space.region_3d
    r3d.update()  # view changes made in this same step are not applied until a redraw
    factor = min(1.0, max_size / float(max(region.width, region.height)))
    width, height = max(1, int(region.width * factor)), max(1, int(region.height * factor))
    offscreen = gpu.types.GPUOffScreen(width, height)
    try:
        offscreen.draw_view3d(bpy.context.scene, bpy.context.view_layer, space, region,
                              r3d.view_matrix, r3d.window_matrix, do_color_management=True)
        rows = offscreen.texture_color.read().to_list()
    finally:
        offscreen.free()
    pixels = [c / 255.0 for row in rows for px in row for c in px]
    if not any(pixels[i] for i in range(0, len(pixels), 4)) and not any(pixels[i] for i in range(1, len(pixels), 4)):
        raise RuntimeError("off-screen capture came back empty")
    image = bpy.data.images.new("claude_capture", width, height, alpha=True)
    try:
        image.pixels.foreach_set(pixels)
        image.filepath_raw = path
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)
    return width, height


def _capture_screen(window, area, region, path, max_size):
    """Fallback: grab the viewport's pixels from the screen."""
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.screen.screenshot_area(filepath=path)
    image = bpy.data.images.load(path, check_existing=False)
    try:
        width, height = image.size
        if max(width, height) > max_size:
            factor = max_size / float(max(width, height))
            image.scale(max(1, int(width * factor)), max(1, int(height * factor)))
            image.filepath_raw = path
            image.file_format = "PNG"
            image.save()
        return tuple(image.size)
    finally:
        bpy.data.images.remove(image)


def cmd_get_viewport_screenshot(max_size=800, view=None, frame_all=False, shading=None):
    if bpy.app.background:
        raise RuntimeError("Blender is running without a window (background mode), so there is no viewport to capture.")
    found = _find_view3d()
    if found is None:
        raise RuntimeError("No 3D Viewport is open in Blender.")
    window, area, region = found
    if shading:
        area.spaces.active.shading.type = shading.upper()
    if view or frame_all:
        cmd_set_view(view=view, frame_all=frame_all)
    path = os.path.join(tempfile.gettempdir(), "claude_viewport_%d.png" % os.getpid())
    try:
        width, height = _capture_offscreen(area, region, path, max_size)
        method = "offscreen"
    except Exception as exc:
        _log("Off-screen capture failed (%s), grabbing the screen instead" % exc)
        width, height = _capture_screen(window, area, region, path, max_size)
        method = "screen"
    with open(path, "rb") as handle:
        data = base64.b64encode(handle.read()).decode("ascii")
    with contextlib.suppress(OSError):
        os.remove(path)
    return {"width": width, "height": height, "format": "png", "method": method, "image_base64": data}


INTERPOLATIONS = {"bezier": "BEZIER", "linear": "LINEAR", "constant": "CONSTANT"}


def cmd_set_keyframe(object, frame, location=None, rotation=None, scale=None, interpolation=None):
    """Pose an object at a frame and keyframe what was given. rotation is in degrees."""
    obj = _get_object(object)
    frame = int(frame)
    keyed = []
    for prop, value in (("location", location), ("rotation_euler", rotation), ("scale", scale)):
        if value is None:
            continue
        if prop == "rotation_euler":
            value = [math.radians(a) for a in value]
        elif prop == "scale" and isinstance(value, (int, float)):
            value = [value] * 3
        setattr(obj, prop, value)
        obj.keyframe_insert(data_path=prop, frame=frame)
        keyed.append(prop)
    if not keyed:
        raise ValueError("Give at least one of location, rotation or scale.")
    if interpolation:
        mode = INTERPOLATIONS.get(interpolation.lower())
        if mode is None:
            raise ValueError("interpolation must be one of: %s" % ", ".join(INTERPOLATIONS))
        for fcurve in _fcurves(obj):
            for point in fcurve.keyframe_points:
                if int(round(point.co.x)) == frame:
                    point.interpolation = mode
    return {"object": obj.name, "frame": frame, "keyed": keyed}


def _fcurves(obj):
    action = obj.animation_data.action if obj.animation_data else None
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    # Blender 4.4+ layered actions keep the curves in channelbags
    curves = []
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                curves.extend(bag.fcurves)
    return curves


def cmd_set_animation_settings(frame_start=None, frame_end=None, fps=None, current_frame=None):
    scene = bpy.context.scene
    if frame_start is not None:
        scene.frame_start = int(frame_start)
    if frame_end is not None:
        scene.frame_end = int(frame_end)
    if fps is not None:
        scene.render.fps = int(fps)
        scene.render.fps_base = 1.0
    if current_frame is not None:
        scene.frame_set(int(current_frame))
    return {"frame_start": scene.frame_start, "frame_end": scene.frame_end,
            "fps": scene.render.fps, "current_frame": scene.frame_current}


def cmd_clear_animation(object):
    obj = _get_object(object)
    obj.animation_data_clear()
    return {"object": obj.name, "cleared": True}


REFERENCE_ROTATIONS = {
    "front": (90, 0, 0),
    "back": (90, 0, 180),
    "right": (90, 0, 90),
    "left": (90, 0, -90),
    "top": (0, 0, 0),
    "bottom": (180, 0, 0),
}


def cmd_add_reference_image(path, view="front", size=5.0, opacity=0.5, location=(0, 0, 0), name=None):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    view = view.lower()
    if view not in REFERENCE_ROTATIONS:
        raise ValueError("view must be one of: %s" % ", ".join(REFERENCE_ROTATIONS))
    image = bpy.data.images.load(path, check_existing=True)
    obj = bpy.data.objects.new(name or "Reference_" + view, None)
    obj.empty_display_type = "IMAGE"
    obj.data = image
    obj.empty_display_size = float(size)
    obj.empty_image_offset = (-0.5, -0.5)
    obj.use_empty_image_alpha = True
    obj.color[3] = float(opacity)
    with contextlib.suppress(AttributeError, TypeError):
        obj.empty_image_depth = "BACK"
    with contextlib.suppress(AttributeError):
        obj.empty_image_side = "FRONT"
    obj.hide_select = True
    obj.hide_render = True
    _link(obj)
    _set_transform(obj, location=location, rotation=REFERENCE_ROTATIONS[view])
    return {"name": obj.name, "image": image.name, "image_size": list(image.size), "view": view}


MISSING_NUMPY_HINT = (
    "This Blender build has no numpy, which Blender's own %s exporter needs. Linux distribution "
    "packages (for example Ubuntu's `apt install blender`) often leave it out. Export as obj or stl "
    "instead, or install Blender from blender.org, which includes numpy."
)


def _explain_export_error(fmt, exc):
    """Turn a confusing exporter crash into advice when the cause is a missing numpy."""
    if "numpy" in str(exc):
        return RuntimeError(MISSING_NUMPY_HINT % fmt.upper())
    return exc


def cmd_export_model(path, format=None, selected_only=False):
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fmt = (format or os.path.splitext(path)[1].lstrip(".")).lower()
    try:
        _export(path, fmt, selected_only)
    except Exception as exc:
        raise _explain_export_error(fmt, exc) from exc
    return {"path": path, "format": fmt, "bytes": os.path.getsize(path) if os.path.exists(path) else None}


def _export(path, fmt, selected_only):
    if fmt in ("glb", "gltf"):
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE",
                                  use_selection=selected_only)
    elif fmt == "fbx":
        bpy.ops.export_scene.fbx(filepath=path, use_selection=selected_only)
    elif fmt == "obj":
        bpy.ops.wm.obj_export(filepath=path, export_selected_objects=selected_only)
    elif fmt == "stl":
        if "stl_export" in dir(bpy.ops.wm):  # Blender 4.2+ (hasattr is always True on bpy.ops)
            bpy.ops.wm.stl_export(filepath=path, export_selected_objects=selected_only)
        else:
            bpy.ops.export_mesh.stl(filepath=path, use_selection=selected_only)
    elif fmt == "usd" or fmt == "usdc":
        bpy.ops.wm.usd_export(filepath=path, selected_objects_only=selected_only)
    else:
        raise ValueError("Unsupported format %r. Use glb, gltf, fbx, obj, stl or usd." % fmt)


def cmd_save_blend_file(path=None):
    if path:
        path = os.path.abspath(os.path.expanduser(path))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=path)
    elif bpy.data.filepath:
        bpy.ops.wm.save_mainfile()
    else:
        raise ValueError("This .blend file has never been saved. Give a path.")
    return {"path": bpy.data.filepath}


COMMANDS = {
    "ping": cmd_ping,
    "get_scene_info": cmd_get_scene_info,
    "get_object_info": cmd_get_object_info,
    "create_object": cmd_create_object,
    "modify_object": cmd_modify_object,
    "set_material": cmd_set_material,
    "delete_objects": cmd_delete_objects,
    "clear_scene": cmd_clear_scene,
    "execute_code": cmd_execute_code,
    "set_view": cmd_set_view,
    "set_keyframe": cmd_set_keyframe,
    "set_animation_settings": cmd_set_animation_settings,
    "clear_animation": cmd_clear_animation,
    "get_viewport_screenshot": cmd_get_viewport_screenshot,
    "add_reference_image": cmd_add_reference_image,
    "export_model": cmd_export_model,
    "save_blend_file": cmd_save_blend_file,
}


# ---------------------------------------------------------------------------
# User interface
# ---------------------------------------------------------------------------


class CLAUDEBRIDGE_Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    port: bpy.props.IntProperty(name="Port", default=DEFAULT_PORT, min=1024, max=65535)
    auto_start: bpy.props.BoolProperty(name="Start server when Blender opens", default=True)
    always_allow: bpy.props.BoolProperty(
        name="Always allow Claude (don't ask)", default=False,
        description="Skip the permission question. Only turn this on if you trust every program on this computer")

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "port")
        layout.prop(self, "auto_start")
        layout.prop(self, "always_allow")


class CLAUDEBRIDGE_OT_start(bpy.types.Operator):
    bl_idname = "claude_bridge.start"
    bl_label = "Start Server"
    bl_description = "Start listening for Claude on this computer"

    def execute(self, _context):
        prefs = _prefs()
        try:
            start_server(prefs.port if prefs else DEFAULT_PORT)
        except OSError as exc:
            self.report({"ERROR"}, "Could not start server: %s" % exc)
            return {"CANCELLED"}
        return {"FINISHED"}


class CLAUDEBRIDGE_OT_stop(bpy.types.Operator):
    bl_idname = "claude_bridge.stop"
    bl_label = "Stop Server"
    bl_description = "Stop listening for Claude"

    def execute(self, _context):
        stop_server()
        return {"FINISHED"}


class CLAUDEBRIDGE_OT_ask(bpy.types.Operator):
    """The permission dialog. It stays open until you answer (moving the mouse does not close it)."""

    bl_idname = "claude_bridge.ask"
    bl_label = "Allow Claude to use Blender?"
    bl_options = {"INTERNAL"}

    always: bpy.props.BoolProperty(name="Don't ask again", default=False)

    def invoke(self, context, _event):
        try:  # Blender 4.1+ can name the buttons
            return context.window_manager.invoke_props_dialog(
                self, width=380, title="Allow Claude to use Blender?", confirm_text="Allow")
        except TypeError:
            return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, _context):
        col = self.layout.column(align=True)
        col.label(text="Claude wants to use this Blender.", icon="QUESTION")
        col.label(text="It will be able to create, change and delete objects")
        col.label(text="and run Python scripts in this Blender.")
        col.separator()
        col.label(text="Click Allow (OK) to let it start. Closing this box")
        col.label(text="keeps it waiting: answer in the sidebar's Claude tab.")
        self.layout.prop(self, "always")
        self.layout.operator(CLAUDEBRIDGE_OT_deny.bl_idname, icon="CANCEL")

    def execute(self, _context):
        # Counts even if Claude's request already gave up waiting: the next one runs straight away.
        set_permission("allowed", always=self.always)
        return {"FINISHED"}


class CLAUDEBRIDGE_OT_allow(bpy.types.Operator):
    bl_idname = "claude_bridge.allow"
    bl_label = "Allow (this session)"
    bl_description = "Let Claude use Blender until you close Blender"

    def execute(self, _context):
        set_permission("allowed")
        return {"FINISHED"}


class CLAUDEBRIDGE_OT_allow_always(bpy.types.Operator):
    bl_idname = "claude_bridge.allow_always"
    bl_label = "Always allow"
    bl_description = "Let Claude use Blender now and in future sessions without asking"

    def execute(self, _context):
        set_permission("allowed", always=True)
        return {"FINISHED"}


class CLAUDEBRIDGE_OT_deny(bpy.types.Operator):
    bl_idname = "claude_bridge.deny"
    bl_label = "Deny"
    bl_description = "Do not let Claude use Blender"

    def execute(self, _context):
        set_permission("denied")
        return {"FINISHED"}


class CLAUDEBRIDGE_OT_reset(bpy.types.Operator):
    bl_idname = "claude_bridge.reset"
    bl_label = "Reset permission"
    bl_description = "Forget your answer. Claude will have to ask again"

    def execute(self, _context):
        reset_permission()
        return {"FINISHED"}


class CLAUDEBRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "Claude"
    bl_idname = "CLAUDEBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Claude"

    def draw(self, _context):
        layout = self.layout
        if _State.running:
            layout.label(text="Running (port %d)" % _State.port, icon="LINKED")
            layout.operator(CLAUDEBRIDGE_OT_stop.bl_idname, icon="PAUSE")
        else:
            layout.label(text="Server stopped", icon="UNLINKED")
            layout.operator(CLAUDEBRIDGE_OT_start.bl_idname, icon="PLAY")

        box = layout.box()
        permission = "allowed (always)" if _always_allowed() else _State.permission
        if permission == "pending":
            box.alert = True
            box.label(text="Claude asks to use", icon="QUESTION")
            box.label(text="this Blender. It can edit")
            box.label(text="the scene and run Python.")
            box.operator(CLAUDEBRIDGE_OT_allow.bl_idname, icon="CHECKMARK")
            box.operator(CLAUDEBRIDGE_OT_allow_always.bl_idname, icon="LOCKED")
            box.operator(CLAUDEBRIDGE_OT_deny.bl_idname, icon="CANCEL")
        else:
            labels = {"unknown": "Claude has not asked yet", "allowed": "Claude is allowed",
                      "denied": "Claude is denied", "allowed (always)": "Claude is always allowed"}
            box.label(text=labels.get(permission, permission))
            if permission != "unknown":
                box.operator(CLAUDEBRIDGE_OT_reset.bl_idname, icon="LOOP_BACK")

        if _State.log:
            col = layout.column(align=True)
            col.label(text="Recent activity:")
            for line in _State.log[-6:]:
                col.label(text=line[9:])  # drop the clock time to fit the sidebar


CLASSES = (
    CLAUDEBRIDGE_Preferences,
    CLAUDEBRIDGE_OT_ask,
    CLAUDEBRIDGE_OT_start,
    CLAUDEBRIDGE_OT_stop,
    CLAUDEBRIDGE_OT_allow,
    CLAUDEBRIDGE_OT_allow_always,
    CLAUDEBRIDGE_OT_deny,
    CLAUDEBRIDGE_OT_reset,
    CLAUDEBRIDGE_PT_panel,
)


def _auto_start():
    prefs = _prefs()
    if prefs is None or prefs.auto_start:
        try:
            start_server(prefs.port if prefs else DEFAULT_PORT)
        except OSError as exc:
            _log("Could not start server: %s" % exc)
    return None


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    # persistent: otherwise opening a .blend file (e.g. by double-clicking it) cancels the auto-start
    bpy.app.timers.register(_auto_start, first_interval=1.0, persistent=True)


def unregister():
    stop_server()
    for timer in (_auto_start, _tick):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
