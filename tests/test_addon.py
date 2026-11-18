"""Tests for the Blender add-on. Needs the `bpy` module (pip install bpy); skipped otherwise."""

import os
import sys
import threading
import time

import pytest

bpy = pytest.importorskip("bpy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "addon"))
import claude_blender_bridge as bridge  # noqa: E402

from claude_blender_mcp.server import BlenderConnection, BlenderError  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def addon():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bridge.register()
    yield
    bridge.unregister()


@pytest.fixture(autouse=True)
def fresh_state():
    bridge._State.permission = "unknown"
    bridge._State.waiting = []
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    yield
    bridge.stop_server()


def ok(command, **params):
    response = bridge.run_command(command, params)
    assert response["status"] == "ok", response
    return response["result"]


def test_create_every_primitive():
    for kind in bridge.PRIMITIVES:
        info = ok("create_object", type=kind, name="P_" + kind, location=[1, 2, 3], rotation=[0, 0, 90])
        assert info["location"] == [1, 2, 3]
        assert info["rotation_degrees"][2] == 90
        mesh = bpy.data.objects[info["name"]].data
        assert len(mesh.polygons) > 0, kind
    cube = ok("get_object_info", name="P_cube")
    assert cube["dimensions"] == [2, 2, 2]
    assert cube["mesh"]["faces"] == 6


def test_other_object_types_and_material():
    for kind in ("empty", "camera", "point_light", "sun_light", "text"):
        ok("create_object", type=kind, name=kind)
    ok("create_object", type="cube", name="Box", color=[1, 0, 0], roughness=0.2, metallic=1)
    details = ok("get_object_info", name="Box")["material_details"][0]
    assert details["base_color"] == [1, 0, 0, 1]
    assert details["roughness"] == 0.2
    assert details["metallic"] == 1


def test_modify_delete_clear():
    ok("create_object", type="cube", name="A")
    ok("create_object", type="cube", name="B")
    info = ok("modify_object", name="A", scale=[1, 2, 3], new_name="Body", parent="B")
    assert info["name"] == "Body" and info["parent"] == "B" and info["dimensions"] == [2, 4, 6]
    assert ok("delete_objects", names=["B", "nope"]) == {"deleted": ["B"], "missing": ["nope"]}
    ok("create_object", type="camera", name="Cam")
    ok("clear_scene")
    assert [o.name for o in bpy.data.objects] == ["Cam"]


def test_execute_code_output_result_and_errors():
    out = ok("execute_code", code="print('hi')\nresult = {'n': len(bpy.data.objects)}")
    assert out["output"] == "hi\n" and out["result"] == {"n": 0}
    bad = bridge.run_command("execute_code", {"code": "print('before')\n1/0"})
    assert bad["status"] == "error"
    assert "ZeroDivisionError" in bad["message"] and "before" in bad["message"]


def test_unknown_command_and_bad_args():
    assert bridge.run_command("fly", {})["status"] == "error"
    assert "No object" in bridge.run_command("get_object_info", {"name": "ghost"})["message"]


def test_reference_image_and_export(tmp_path):
    img = bpy.data.images.new("ref", 4, 2)
    img.filepath_raw = str(tmp_path / "ref.png")
    img.file_format = "PNG"
    img.save()
    ref = ok("add_reference_image", path=str(tmp_path / "ref.png"), view="right")
    assert ref["view"] == "right" and ref["image_size"] == [4, 2]
    ok("create_object", type="monkey", name="Suzanne")
    for ext in ("glb", "obj", "stl", "fbx"):
        result = ok("export_model", path=str(tmp_path / ("out." + ext)))
        assert result["bytes"] > 0, ext
    saved = ok("save_blend_file", path=str(tmp_path / "scene.blend"))
    assert os.path.exists(saved["path"])


def test_screenshot_in_background_mode_fails_cleanly():
    # Headless Blender has no screen to capture; the error must come back, not crash.
    response = bridge.run_command("get_viewport_screenshot", {})
    assert response["status"] == "error" and "without a window" in response["message"]


def _request_in_thread(port, command, params=None):
    box = {}

    def go():
        try:
            box["result"] = BlenderConnection(port=port).send(command, params)
        except BlenderError as exc:
            box["error"] = str(exc)

    thread = threading.Thread(target=go)
    thread.start()
    return thread, box


def _pump(until, seconds=5):
    # bpy as a module has no event loop, so timers never fire: run the tick ourselves.
    end = time.time() + seconds
    while time.time() < end and not until():
        bridge._tick()
        time.sleep(0.01)
    bridge._tick()


def _start():
    sock = __import__("socket").socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    bridge.start_server(port)
    return port


def test_ping_needs_no_permission():
    port = _start()
    thread, box = _request_in_thread(port, "ping")
    _pump(lambda: not thread.is_alive())
    thread.join(2)
    assert box["result"]["permission"] == "unknown"
    assert bridge._State.permission == "unknown"


def test_first_command_asks_then_allow_runs_it():
    port = _start()
    thread, box = _request_in_thread(port, "create_object", {"type": "cube", "name": "Asked"})
    _pump(lambda: bridge._State.permission == "pending")
    assert bridge._State.permission == "pending"
    assert "Asked" not in bpy.data.objects  # nothing ran before Allow
    bridge.set_permission("allowed")
    thread.join(2)
    assert box["result"]["name"] == "Asked"
    # Later commands run without asking again.
    thread, box = _request_in_thread(port, "get_scene_info")
    _pump(lambda: not thread.is_alive())
    thread.join(2)
    assert box["result"]["object_count"] == 1


def test_deny_blocks_commands():
    port = _start()
    thread, box = _request_in_thread(port, "clear_scene")
    _pump(lambda: bridge._State.permission == "pending")
    bridge.set_permission("denied")
    thread.join(2)
    assert "denied" in box["error"]
    thread, box = _request_in_thread(port, "get_scene_info")
    _pump(lambda: not thread.is_alive())
    thread.join(2)
    assert "denied" in box["error"]


def test_permission_request_times_out(monkeypatch):
    monkeypatch.setattr(bridge, "PERMISSION_TIMEOUT", 0.2)
    port = _start()
    thread, box = _request_in_thread(port, "get_scene_info")
    _pump(lambda: not thread.is_alive())
    thread.join(2)
    assert "click Allow" in box["error"]
    assert bridge._State.permission == "unknown"  # asks again next time


def test_missing_numpy_export_error_gives_advice():
    crash = RuntimeError("Error: Python: ModuleNotFoundError: No module named 'numpy'")
    advice = bridge._explain_export_error("glb", crash)
    assert "no numpy" in str(advice) and "obj or stl" in str(advice)
    other = ValueError("disk full")
    assert bridge._explain_export_error("glb", other) is other


def test_keyframes_animate_between_frames():
    ok("create_object", type="cube", name="Mover")
    ok("set_animation_settings", frame_start=1, frame_end=24, fps=24)
    ok("set_keyframe", object="Mover", frame=1, location=[0, 0, 0], rotation=[0, 0, 0])
    ok("set_keyframe", object="Mover", frame=21, location=[10, 0, 0], rotation=[0, 0, 90], interpolation="linear")
    mid = ok("set_animation_settings", current_frame=11)
    assert mid["current_frame"] == 11
    obj = bpy.data.objects["Mover"]
    assert abs(obj.location.x - 5.0) < 0.6
    assert bridge.run_command("set_keyframe", {"object": "Mover", "frame": 5})["status"] == "error"
    ok("clear_animation", object="Mover")
    assert obj.animation_data is None


def test_render_image_and_animation(tmp_path):
    ok("create_object", type="monkey", name="Star", color=[1, 0.5, 0])
    ok("create_object", type="camera", name="Cam", location=[0, -6, 0], rotation=[90, 0, 0])
    ok("create_object", type="sun_light", name="Sun", rotation=[45, 0, 30])
    bpy.context.scene.render.resolution_x = 1920
    # Cycles renders on the CPU, so this works without a graphics card (Workbench/EEVEE need one)
    out = ok("render_image", engine="cycles", samples=1, resolution=[64, 36], save_path=str(tmp_path / "still.png"))
    assert (out["width"], out["height"]) == (64, 36) and out["engine"] == "CYCLES"
    assert (tmp_path / "still.png").stat().st_size > 0 and out["image_base64"]
    assert bpy.context.scene.render.resolution_x == 1920  # the user's settings come back
    ok("set_keyframe", object="Star", frame=1, rotation=[0, 0, 0])
    ok("set_keyframe", object="Star", frame=6, rotation=[0, 0, 90])
    video = ok("render_animation", path=str(tmp_path / "spin"), engine="cycles", samples=1, resolution=[64, 36],
               frame_start=1, frame_end=6, fps=12)
    assert video["path"].endswith("spin.mp4") and video["bytes"] > 0 and video["frames"] == 6


def test_render_without_camera_is_explained():
    response = bridge.run_command("render_image", {"engine": "cycles"})
    assert response["status"] == "error" and "no camera" in response["message"]


def test_blocky_character_is_posable():
    info = ok("create_blocky_character", name="Hero", location=[1, 0, 0], height=2.0, hair="fluffy", ears=True,
              colors={"shirt": [0.9, 0.9, 0.9]}, pose={"left_arm": [0, 175, 0]})
    assert "Hero_Shoulder_L" in info["joints"] and info["parts"] > 20
    hero = [o for o in bpy.data.objects if o.name.startswith("Hero_") and o.type == "MESH"]
    top = max((o.matrix_world @ v.co).z for o in hero for v in o.data.vertices)
    assert 2.0 <= top <= 2.4  # 2 m tall plus hair and ears
    hand = bpy.data.objects["Hero_Hand_L"]
    assert hand.matrix_world.translation.z > 1.9  # raised arm puts the hand above the head
    ok("set_keyframe", object="Hero_Shoulder_R", frame=1, rotation=[30, 0, 0])
    assert bridge.run_command("create_blocky_character", {"name": "Bad", "hair": "mohawk"})["status"] == "error"
    assert "Bad" not in bpy.data.collections  # nothing half-built is left behind
    assert bridge.run_command("create_blocky_character", {"name": "Hero"})["status"] == "error"  # name taken
