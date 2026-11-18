# Claude Blender Bridge

Let Claude build 3D models in **your own Blender, on your own computer**.
Send Claude a reference picture; it studies the object, builds it in Blender
step by step, takes viewport screenshots to compare against your reference,
and fixes what doesn't match.

```
 Claude (Desktop / Code)  ──MCP──▶  claude-blender-mcp  ──localhost:9876──▶  Blender add-on  ──▶  your scene
```

- **`addon/claude_blender_bridge.py`**: a Blender add-on. It listens on
  `127.0.0.1` only (nothing outside your computer can reach it) and runs
  Claude's commands inside Blender.
- **`src/claude_blender_mcp/`**: an MCP server. Claude starts it and talks to
  the add-on through it.

## You stay in control

The first time Claude tries to do anything in a Blender session, Blender
pops up a question:

> **Claude wants to use this Blender.** It will be able to create, change and
> delete objects and run Python scripts in this Blender.
> **Allow (this session)** · **Always allow** · **Deny**

Nothing runs until you click **Allow**. If the pop-up closes, the same buttons
are in the 3D Viewport sidebar (press **N**, then open the **Claude** tab).
**Reset permission** in that tab takes your answer back at any time, and
**Stop Server** disconnects Claude completely.

Claude Desktop and Claude Code also ask you before each tool is used, unless
you have allowed that tool.

## Setup

You need Blender 3.6 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/).

### 1. Install the Blender add-on

1. Download [`addon/claude_blender_bridge.py`](addon/claude_blender_bridge.py).
2. In Blender: **Edit → Preferences → Add-ons**. Open the menu at the top right
   of that window (in Blender 4.2 and newer) and choose **Install from Disk…**,
   or click **Install…** in older versions. Pick the file.
3. Tick **Claude Blender Bridge** to enable it.

The server starts on its own when Blender opens. To check, open the
**Claude** tab in the 3D Viewport sidebar (**N**); it should say
*Server running on port 9876*.

### 2. Connect Claude

**Claude Desktop:** open **Settings → Developer → Edit Config** and add this
to `claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "blender": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/dark-lover-opz/a-game", "claude-blender-mcp"]
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add blender -- uvx --from git+https://github.com/dark-lover-opz/a-game claude-blender-mcp
```

If you cloned this repository, you can point at your copy instead:
`uv run --directory /path/to/a-game claude-blender-mcp`.

## Using it

1. Open Blender.
2. In Claude, attach your reference image and ask, for example:
   *"Build this chair in my Blender."* In Claude Desktop you can also choose
   the **model_from_reference** prompt from the attachment menu.
3. Click **Allow** when Blender asks.
4. Watch it build. Ask for changes ("make the legs thinner", "use a dark wood
   material"), then ask Claude to save the `.blend` file or export it
   (`.glb`, `.fbx`, `.obj`, `.stl`, `.usd`).

You can also give Claude the path of an image on your computer. It can look
at it (`view_image_file`) and place it in the scene as a see-through
tracing guide (`add_reference_image`).

## Tools Claude gets

| Tool | What it does |
|---|---|
| `blender_status` | Is Blender reachable, and has the user allowed Claude yet? |
| `get_scene_info`, `get_object_info` | Read what's in the scene |
| `create_object` | Cube, spheres, cylinder, cone, torus, plane, monkey, empty, text, camera, lights |
| `modify_object` | Move / rotate / scale / rename / hide / parent |
| `set_material` | Color, metal, roughness, emission, transparency |
| `delete_objects`, `clear_scene` | Remove objects |
| `execute_blender_code` | Run Python (`bpy`, `bmesh`) for anything else: modifiers, extrusions, curves, custom meshes |
| `set_view`, `get_viewport_screenshot` | Look at the model from any side to compare it with the reference |
| `view_image_file`, `add_reference_image` | Look at a reference image on disk / place it in the scene |
| `export_model`, `save_blend_file` | Save the result |

## Settings

- **Port:** set in the add-on's preferences. Pass the same number to the MCP
  server with the `BLENDER_PORT` environment variable.
- **Start server when Blender opens:** on by default.
- **Always allow Claude:** off by default. Only turn it on if you trust every
  program on your computer, because any local program could then send commands.

## Safety

`execute_blender_code` runs any Python Claude writes, with the same access to
your computer as Blender itself. Allow it only while you are working with
Claude, save your work first, and use **Deny**, **Reset permission** or
**Stop Server** whenever you want Claude out.

## Development

```bash
uv venv && uv pip install -e ".[test]" bpy   # bpy is Blender as a Python module, used by the add-on tests
uv run pytest
```
