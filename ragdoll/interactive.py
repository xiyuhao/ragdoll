"""Functionality for menus

This module is where we deal with the messy nature of user
input and try to be as forgiving as possible to what goes in,
and strict as possible about what goes out.

  ________________             _________
 |                |           |         |
 | User Selection |---- + --->|         |
 |________________|           |         |
  __________________          |         |    _______________
 |                  |         |         |   |               |
 | User preferences |-- + --->| Ragdoll |-->| User Feedback |
 |__________________|         |         |   |_______________|
  ____________                |         |
 |            |               |         |
 | Maya state |-------- + --->|         |
 |____________|               |_________|


Each command..

- Takes an optional (selection=) as its first argument
- Use persistent optionVars stored in Maya preferences
- Cannot throw an exception, these must be actionable messages
    for the end-user

These build on functionality found in ragdoll.commands
and can but generally *should not* be used for scripting.

"""

import os
import sys
import copy
import json
import time
import logging
import functools
import contextlib

from maya import cmds, mel
from maya.utils import MayaGuiLogHandler
from maya.api import OpenMaya as om
from .vendor import cmdx, qargparse
from . import (
    commands,
    tools,
    upgrade,
    ui,
    options,
    licence,
    __
)

# Environment variables
RAGDOLL_DEVELOPER = bool(os.getenv("RAGDOLL_DEVELOPER"))
RAGDOLL_PLUGIN = os.getenv("RAGDOLL_PLUGIN", "ragdoll")
RAGDOLL_NO_STARTUP_DIALOG = bool(os.getenv("RAGDOLL_NO_STARTUP_DIALOG"))
RAGDOLL_AUTO_SERIAL = os.getenv("RAGDOLL_AUTO_SERIAL")

CREATE_NEW_SOLVER = "Create new solver"

log = logging.getLogger("ragdoll")
Warning = ValueError
DoNothing = None
Cancelled = False

kSuccess = True
kFailure = False


# Internal
__.previousvars = {
    "MAYA_SCRIPT_PATH": os.getenv("MAYA_SCRIPT_PATH", ""),
    "XBMLANGPATH": os.getenv("XBMLANGPATH", ""),
}


# Recording-related data
_recorded_actions = []


def _print_exception():
    if RAGDOLL_DEVELOPER:
        import traceback
        traceback.print_exc()


def _resource(*fname):
    dirname = os.path.dirname(__file__)
    resdir = os.path.join(dirname, "resources")
    return os.path.normpath(os.path.join(resdir, *fname))


def _is_standalone():
    """Is Maya running without a GUI?"""
    return not hasattr(cmds, "about") or cmds.about(batch=True)


def _on_scene_open(*args):
    """Handle upgrades of nodes saved with an older version of Ragdoll"""

    if options.read("upgradeOnSceneOpen"):
        _evaluate_need_to_upgrade()


def _graphical(func):
    """Wrapper for functions that rely on being displayed

    Mostly for CI. These are simply ignored.

    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if _is_standalone():
            return True
        return func(*args, **kwargs)
    return wrapper


def _selected_channels():
    """Get currently selected attributes from the channelbox

    Reference: http://forums.cgsociety.org/showthread.php
                      ?f=89&t=892246&highlight=main+channelbox

    """

    channel_box = mel.eval(
        "global string $gChannelBoxName; $temp=$gChannelBoxName;"
    )

    attrs = cmds.channelBox(channel_box,
                            selectedMainAttributes=True,
                            query=True) or []

    attrs += cmds.channelBox(channel_box,
                             selectedShapeAttributes=True,
                             query=True) or []

    attrs += cmds.channelBox(channel_box,
                             selectedHistoryAttributes=True,
                             query=True) or []

    attrs += cmds.channelBox(channel_box,
                             selectedOutputAttributes=True,
                             query=True) or []

    # Returned attributes are shortest possible,
    # e.g. 'tx' instead of 'translateX'
    return attrs


# Every argument used by UIs
#
# These can be accessed/modified via cmds.optionVar
# as <prefix><name> where <prefix> is "ragdoll" and are
# all stored persistently alongside Maya's native preferences
with open(_resource("options.json")) as f:
    __.optionvars = json.load(f)


# Every menu item
with open(_resource("menu.json")) as f:
    __.menuitems = json.load(f)


def install():
    install_logger()
    install_plugin()
    licence.install(RAGDOLL_AUTO_SERIAL)
    options.install()
    cmdx.install()

    if not _is_standalone():
        install_callbacks()

        # Give Maya's GUI a chance to boot up
        cmds.evalDeferred(install_menu)

        if not RAGDOLL_NO_STARTUP_DIALOG and options.read("firstLaunch2"):
            cmds.evalDeferred(welcome_user)
            options.write("firstLaunch2", False)

    __.installed = True


def uninstall():
    if "RAGDOLL_DO_NOT_LOAD_BINDING" not in os.environ:
        return log.error(
            "Can't uninstall this, the Python bindings are loaded. "
            "Set RAGDOLL_DO_NOT_LOAD_BINDING to avoid that."
        )

    if not __.installed:
        return log.warning("Ragdoll not installed")

    uninstall_callbacks()
    uninstall_menu()
    uninstall_logger()
    options.uninstall()

    uninstall_pyragdoll()

    # Call last, for Maya to properly unload and clean up
    uninstall_plugin()

    cmdx.uninstall()

    # Erase all trace
    for module in sys.modules.copy():
        if module.startswith("ragdoll"):
            sys.modules.pop(module)


class RagdollGuiLogHandler(MayaGuiLogHandler):
    """Gather errors and warnings for the Message Board"""

    history = []

    def emit(self, record):
        if record.levelno > logging.INFO:
            RagdollGuiLogHandler.history.append(record)
            update_menu()

        return super(RagdollGuiLogHandler, self).emit(record)


def install_logger():
    fmt = logging.Formatter(
        "ragdoll.%(funcName)s() - %(message)s"
    )

    # NOTE: This affects logging outside of Ragdoll as well,
    # how else can we avoid all-caps "WARNING" level names?
    logging.addLevelName(logging.INFO, "Info")
    logging.addLevelName(logging.WARNING, "Warning")

    # This one works like logging.StreamHandler,
    # except it also colors the Command Line nicely
    handler = RagdollGuiLogHandler()

    handler.setFormatter(fmt)
    log.addHandler(handler)
    log.propagate = False


def uninstall_logger():
    log.handlers[:] = []
    log.propagate = True  # Defer to root logger


def install_callbacks():
    __.callback = om.MSceneMessage.addCallback(
        om.MSceneMessage.kAfterOpen,
        _on_scene_open
    )


def uninstall_callbacks():
    om.MMessage.removeCallback(__.callback)


def install_plugin():
    os.environ["XBMLANGPATH"] = os.pathsep.join([
        _resource(__.xbmlangpath),
        __.previousvars["XBMLANGPATH"]
    ])

    os.environ["MAYA_SCRIPT_PATH"] = os.pathsep.join([
        _resource(__.aetemplates),
        __.previousvars["MAYA_SCRIPT_PATH"]
    ])

    # Override with RAGDOLL_PLUGIN environment variable
    cmds.loadPlugin(RAGDOLL_PLUGIN)

    # Required by tools.py
    cmds.loadPlugin("matrixNodes", quiet=True)

    __.version_str = cmds.pluginInfo("ragdoll", query=True, version=True)

    # Debug builds come with a `.debug` suffix, e.g. `2020.10.15.debug`
    __.version = int("".join(__.version_str.split(".")[:3]))


def uninstall_plugin(force=True):
    cmds.file(new=True, force=force)

    try:
        cmds.unloadPlugin(os.path.basename(RAGDOLL_PLUGIN))
    except RuntimeError as e:
        # This is fine
        log.warning(str(e))

    # Restore environment
    os.environ["MAYA_SCRIPT_PATH"] = (
        os.environ["MAYA_SCRIPT_PATH"].replace(
            _resource(__.aetemplates) + os.pathsep, "")
    )
    os.environ["XBMLANGPATH"] = (
        os.environ["XBMLANGPATH"].replace(
            _resource(__.xbmlangpath) + os.pathsep, "")
    )


def uninstall_pyragdoll():
    """Clean up compiled Python extension

    Compiled Python extensions are special. Normally, when a `.py`
    module is no longer in `sys.modules` the module is gone. On
    re-import, the file is read from disk anew. But, the file handle
    of a compiled library is never really closed. It can't be.
    Instead, the file handle remains open for the duration of the
    Python interpreter; which in the case of Maya means until Maya
    itself is shutdown.

    This is bad news for uninstalling Ragdoll.

    """

    import ctypes
    import _ctypes

    # Get rid of any direct reference
    pyragdoll = sys.modules.pop("pyragdoll", None)

    # It may not have been loaded, that's ok
    if not pyragdoll:
        return

    # Now release the file handle
    fname = pyragdoll.__file__

    if fname.endswith(".pyd"):
        log.info("Freeing pyragdoll..")

        dll = ctypes.CDLL(fname)

        # The following is safe, as we can guarantee that there is no
        # code being executed on release of this file handle.
        _ctypes.FreeLibrary(dll._handle)
        _ctypes.FreeLibrary(dll._handle)

        # Called twice? Yes, it doesn't seem to have an effect otherwise :S
        # Maybe one is for this call?


def install_menu():
    if __.menu:
        uninstall_menu()

    __.menu = cmds.menu(label="Ragdoll",
                        tearOff=True,
                        parent="MayaWindow")

    def item(key, command=None, option=None, label=None, visible=True):
        menuitem = __.menuitems[key]

        kwargs = {
            "label": menuitem.get("label", label or key),
            "enable": menuitem.get("enable", True),
            "echoCommand": True,
            "image": "bad.png",

            # These show up in the Maya "Help Line" on hover
            "annotation": menuitem.get("summary", ""),
        }

        if command:
            # Store as string instead of Python function,
            # so as to facilitate saving menu items to Shelf
            # via Ctrl + Shift + Click.
            script = "from ragdoll import interactive as ri\n"
            script += "ri.%s()" % command.__name__
            kwargs["command"] = script

            # Create a reverse-mapping for recording
            __.actiontokey[command.__name__] = key

        if "icon" in menuitem:
            icon = _resource(os.path.join("icons", menuitem["icon"]))
            kwargs["image"] = icon

        if "checkbox" in menuitem:
            kwargs["checkBox"] = True

        menuitem["path"] = cmds.menuItem(**kwargs)

        if option:
            cmds.menuItem(command=option, optionBox=True)

        if not visible:
            # The cmds.menuItem(visible=) flag was introduced in Maya 2019
            ui.hide_menuitem(menuitem["path"])

        return menuitem["path"]

    @contextlib.contextmanager
    def submenu(label, icon=None):
        kwargs = {
            "subMenu": True,
            "tearOff": True,
        }

        if icon:
            kwargs["image"] = _resource(os.path.join("icons", icon))

        previous_parent = cmds.setParent(menu=True, query=True)
        cmds.menuItem(label, **kwargs)
        yield
        cmds.setParent(previous_parent, menu=True)

    def divider(label=None):
        cmds.menuItem(divider=True, dividerLabel=label)

    item("showMessages",
         command=show_messageboard,

         # Programatically displayed during logging
         visible=False)

    divider("Create")

    item("activeRigid", create_active_rigid, create_rigid_options)
    item("passiveRigid", create_passive_rigid, create_passive_options)
    item("soft")
    item("cloth")
    item("muscle", create_muscle, create_muscle_options)
    item("fluid")

    divider("Constrain")

    item("point", create_point_constraint, _constraint_options("Point"))
    item("orient", create_orient_constraint, _constraint_options("Orient"))
    item("parent", create_parent_constraint, _constraint_options("Parent"))
    item("hinge", create_hinge_constraint, _constraint_options("Hinge"))
    item("socket", create_socket_constraint, _constraint_options("Socket"))

    divider("Control")

    item("kinematic", create_kinematic_control,
         create_kinematic_control_options)
    item("guide", create_driven_control,
         create_driven_control_options)
    item("motor")
    item("actuator")
    item("trigger")

    divider("Force")

    item("push", create_push_force, create_push_force_options)
    item("pull", create_pull_force, create_pull_force_options)
    item("directional", create_uniform_force, create_uniform_force_options)
    item("wind", create_turbulence_force, create_turbulence_force_options)

    divider()

    item("visualiser", create_slice)
    item("assignToSelected", assign_force)

    divider("Emit")

    item("particles")

    divider("Assists")

    item("character", create_character, create_character_options)
    item("trajectory")
    item("momentOfInertia")
    item("centerOfMass")

    divider("Utilities")

    with submenu("Animation", icon="animation.png"):
        item("createDynamicControl",
             create_dynamic_control,
             create_dynamic_control_options)
        item("bakeSimulation")
        item("exportPhysics")
        item("importPhysics")
        item("multiplyRigids",
             multiply_rigids,
             multiply_rigids_options)
        item("multiplyConstraints",
             multiply_constraints,
             multiply_constraints_options)

    with submenu("Rigging", icon="rigging.png"):
        item("editConstraintFrames", edit_constraint_frames)
        item("duplicateSelected", duplicate_selected)
        item("transferAttributes", transfer_selected)
        item("convertToPolygons", convert_to_polygons)
        item("normaliseShapes", normalise_shapes)
        item("setInitialState", set_initial_state)

    with submenu("Select", icon="select.png"):
        item("selectRigids",
             select_rigids,
             select_rigids_options)
        item("selectConstraints",
             select_constraints,
             select_constraints_options)

    with submenu("System", icon="system.png"):
        item("deleteAllPhysics", delete_physics, delete_physics_options)
        item("globalPreferences", global_preferences)

    divider()

    item("ragdoll", welcome_user, label="Ragdoll %s" % __.version_str)


def uninstall_menu():
    if __.menu:
        cmds.deleteUI(__.menu, menu=True)

    __.menu = None


def show_messageboard():
    win = ui.MessageBoard(RagdollGuiLogHandler.history, parent=ui.MayaWindow())
    win.show()

    # Auto-clear on show, message has been received
    RagdollGuiLogHandler.history[:] = []
    update_menu()


def replay(actions):
    this = sys.modules[__name__]

    for action in actions:
        func = getattr(this, action["action"])

        # Avoid recursion
        func = this._replay(func)

        options = action["options"]
        cmds.select(action["selection"])
        func(**options)


def show_replayer():
    win = ui.Replayer(_recorded_actions, parent=ui.MayaWindow())
    win.replay_clicked.connect(replay)
    win.show()


def update_menu():
    count = len(RagdollGuiLogHandler.history)

    label = (
        "Ragdoll (%d)" % count
        if count else "Ragdoll"
    )

    menu_item = __.menuitems["showMessages"]
    menu_kwargs = {
        "label": "%s (%d)" % (menu_item["label"], count),
        "edit": True,
    }

    change_visibility = ui.show_menuitem if count else ui.hide_menuitem
    change_visibility(menu_item["path"])

    # Help the user understand there's a problem somewhere
    cmds.menu(__.menu, edit=True, label=label)
    cmds.menuItem(menu_item["path"], **menu_kwargs)


"""

# Upgrade Path

This next part is what maintains backwards compatibility when changes
have been made to the plug-in in such a way that it alters the behavior
of your scene. In such cases, an upgrade is performed to convert your
scene into one that behaves identically to before.

"""


def _upgrade():
    # Sometimes, Maya doesn't see the global scope
    # so we better re-import it here
    from .vendor import cmdx

    def __upgrade():
        upgraded_count = 0

        for scene in cmdx.ls(type="rdScene"):
            scene_version = scene["version"].read()

            if scene_version < __.version:
                upgrade.scene(scene, scene_version, __.version)
                upgraded_count += 1

        for rigid in cmdx.ls(type="rdRigid"):
            rigid_version = rigid["version"].read()

            if rigid_version < __.version:
                upgrade.rigid(rigid, rigid_version, __.version)
                upgraded_count += 1

        return upgraded_count

    try:
        upgraded_count = __upgrade()

        if upgraded_count:
            log.warning("%d Ragdoll nodes were upgraded" % upgraded_count)
        else:
            log.warning("Ragdoll nodes already up to date!")

    except Exception:
        import traceback
        traceback.print_exc()
        log.warning(
            "I had trouble upgrading, it should still "
            "work but you may want to consider restarting Maya"
        )


def _needs_upgrade():
    needs_upgrade = 0
    oldest_version = __.version

    for scene in cmdx.ls(type=("rdRigid", "rdScene")):
        node_version = scene["version"].read()

        if upgrade.has_upgrade(scene, node_version):
            needs_upgrade += 1

        if node_version < oldest_version:
            oldest_version = node_version

    return oldest_version, needs_upgrade


def _evaluate_need_to_upgrade():
    oldest, needed = _needs_upgrade()

    if not needed:
        return

    saved_version = oldest
    current_version = __.version

    message = """\
This file was created with an older version of Ragdoll %s

Would you like to convert %d nodes to Ragdoll %s? Not converting \
may break the behavior from your previous scene.
""" % (saved_version, needed, current_version)

    if ui.MessageBox("%d Ragdoll nodes can be upgraded" % needed, message):
        _upgrade()


def _find_current_scene(autocreate=True):
    scene = options.read("solver")

    # No questions asked, just make a new one
    if scene == CREATE_NEW_SOLVER:
        scene = create_scene()

    else:
        # The one stored persistently may not actually exist,
        # it may come from another scene or at a time when it
        # did exist but got deleted
        try:
            scene = cmdx.encode(scene)

        except cmdx.ExistError:
            # Ok, no persistent clue or request for a new scene
            try:
                scene = cmdx.ls(type="rdScene")[0]

            # Nothing in sight, now it's up to the function
            except IndexError:
                if autocreate:
                    scene = create_scene()
                else:
                    raise cmdx.ExistError("No Ragdoll scene was found")

    # Use this from now on
    options.write("solver", scene.shortest_path())

    return scene


@_graphical
def warn_about_dg():
    def ignore():
        return True

    def enable_parallel():
        cmds.evaluationManager(mode="parallel")
        log.info("Enabled Parallel Evaluation")
        return True

    return ui.warn(
        option="validateEvaluationMode",
        title="DG Evaluation Mode Detected",
        message=(
            "Maya is currently evaluating in the old DG mode, "
            "Ragdoll is most optimal with with Parallel Evaluation or "
            "at the very least Serial. If you experience issues with "
            "performance or drawing in the viewport, switch to Parallel."
        ),
        call_to_action="What would you like to do?",
        actions=[
            ("Ignore", ignore),
            ("Enable Parallel Evaluation", enable_parallel),
            ("Cancel", lambda: False)
        ]
    )


@_graphical
def warn_about_pivot():
    return ui.warn(
        option="validateRotatePivot",
        title="Custom Rotate Pivot Found",
        message=(
            "Non-zero rotate pivots were found. These are currently "
            "unsupported and need to be zeroed out, "
            "see Script Editor for details."
        ),
        call_to_action="What would you like to do?",
        actions=[

            # Happens automatically by commands.py
            # Take it or leave it, doesn't work otherwise
            ("Zero out rotatePivot", lambda: True),

            ("Cancel", lambda: False)
        ]
    )


@_graphical
def validate_playbackspeed():
    play_every_frame = cmds.optionVar(query="timeSliderPlaySpeed") == 0.0

    if play_every_frame:
        return True

    return ui.warn(
        option="validatePlaybackSpeed",
        title="Play every frame",
        message=(
            "Ensure your playback speed is set to 'Play every frame' "
            "to avoid frame drops, these can break a simulation and "
            "generally causes odd things to happen."
        ),
        call_to_action="Go to Maya Preferences to change this.",
        actions=[
            ("Ok", lambda: True)
        ]
    )


def _replay(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):

        # Hint to the recorder that we're playing back this
        # command, and that it shouldn't be recorded
        kwargs["_replaying"] = True

        return func(*args, **kwargs)
    return wrapper


def _replayable(func):
    """Enable replay of this action

    The animator can save a series of commands as a script,
    and recall these later with optional customisations on e.g.
    options and selection.

    """

    def _append(root, action):
        with cmdx.DagModifier() as mod:
            if not root.has_attr("_ragdollHistory"):
                mod.add_attr(root, cmdx.String("_ragdollHistory"))
                mod.do_it()

            history = root["_ragdollHistory"].read() or "[]"

            try:
                history = json.loads(history)

            except Exception:
                log.warning(
                    "Malformatted Ragdoll history on %s, clearing"
                    % root
                )
                history = []

            history.append(action)
            mod.set_attr(root["_ragdollHistory"], json.dumps(history))

    def _record(selection):
        key = __.actiontokey[func.__name__]
        item = __.menuitems[key]
        opt = item.get("options", [])

        action = {
            "name": func.__name__,

            # For posterity and UI
            "time": time.time(),

            # Include explicitly passed selection, in case
            # the call is being made from a script.
            # Questionable whether we should record these at all (?)
            "selection": [
                node.shortest_path()
                for node in selection
            ],

            # Store options at the time of calling
            "options": {
                key: options.read(key)
                for key in opt
            }
        }

        _recorded_actions.append(action)
        _append(selection[0], action)

    @functools.wraps(func)
    def wrapper(selection=None, **kwargs):
        if not kwargs.get("_replaying") and not _is_standalone():
            selection = selection or cmdx.selection()
            if selection:
                try:
                    _record(selection)
                except Exception:
                    # This *cannot* cause function to not get called
                    import traceback
                    traceback.print_exc()

        return func(selection, **kwargs)

    return wrapper


def _filtered_selection(node_type):
    """Interpret user selection

    They should be able to..

    1. Select transforms, even though they meant the shape
    2. Select a transform with *multiple* shapes of a given type
    2. Select the shape
    3. Select multiple shapes
    4. Select multiple shapes *and* transforms

    """

    selection = list(cmdx.selection())

    if not selection:
        return []

    shapes = []
    for node in selection:
        shapes += node.shapes(node_type)

    shapes = filter(None, shapes)
    shapes = list(shapes) + selection
    shapes = filter(lambda shape: shape.type() == node_type, shapes)

    return shapes


@commands.with_undo_chunk
def create_scene(selection=None):

    if options.read("validateEvaluationMode"):
        mode = cmds.evaluationManager(query=True, mode=True)

        if mode[0] == 'off':
            if warn_about_dg() is Cancelled:
                return

    if options.read("validatePlaybackSpeed"):
        validate_playbackspeed()

    return commands.create_scene()


def has_valid_rotatepivot(transform):
    """Ragdoll currently does not support any custom pivot or axis"""

    if not options.read("validateRotatePivot"):
        return True

    tolerance = 0.01
    nonzero = []

    for attr in ("rotatePivot",
                 "rotatePivotTranslate",
                 "scalePivot",
                 "scalePivotTranslate",
                 "rotateAxis"):
        for axis in "XYZ":
            plug = transform[attr + axis]
            if abs(plug.read()) > tolerance:
                nonzero.append(plug)

    if nonzero:
        for plug in nonzero:
            log.warning("%s was not zero" % plug.path())

        return warn_about_pivot()

    else:
        return True


def _opt(key, override=None):
    override = override or {}
    return override.get(key, options.read(key))


@_replayable
@commands.with_undo_chunk
def create_active_rigid(selection=None, **opts):
    """Create a new rigid from selection"""

    created = []
    converted = []
    selection = selection or cmdx.selection()

    if not selection:
        return log.warning(
            "Select something to turn dynamic, "
            "e.g. a box or NURBS curve"
        )

    # Based on the first selection, determine
    # whether to convert or create something new.
    if selection[0].isA(cmdx.kShape):
        if selection[0].type() == "rdRigid":
            # The user meant to convert the selection
            return convert_rigid(selection, opts)

    elif selection[0].isA(cmdx.kTransform):
        if selection[0].shape("rdRigid"):
            # The user meant to convert the selection
            return convert_rigid(selection, opts)

    if not _validate_transforms(selection):
        return

    previous = None
    select = _opt("rigidSelect", opts)
    passive = _opt("createRigidType", opts) == "Passive"

    scene = _find_current_scene()

    for index, node in enumerate(selection):
        transform = node.parent() if node.isA(cmdx.kShape) else node

        if not has_valid_rotatepivot(transform):
            break

        # Rigid bodies must have translate and rotate channels
        if not transform.isA(cmdx.kTransform):
            log.warning("%s is not a transform node", transform.path())
            continue

        existing = {
            "Abort": commands.Abort,
            "Overwrite": commands.Overwrite,
            "Blend": commands.Blend,
        }.get(_opt("existingAnimation", opts), "Overwrite")

        kwargs = {
            "compute_mass": _opt("computeMass", opts),
            "passive": passive,
            "existing": existing,
        }

        try:
            rigid = commands.create_rigid(node, scene, **kwargs)
        except Exception as e:
            _print_exception()
            log.error(str(e))
            continue

        # There may have been an error
        if not rigid:
            continue

        # Apply optionvars
        initial_shape = _opt("initialShape", opts)
        auto_connect = _opt("autoConnect", opts)

        if initial_shape != "Auto":
            # Overtake the automatic mechanism from commands.py
            # with whatever the user selected in the UIr
            shapes = {
                "Box": commands.BoxShape,
                "Sphere": commands.SphereShape,
                "Capsule": commands.CapsuleShape,
                "Mesh": commands.ConvexHullShape,
            }

            with cmdx.DagModifier() as mod:
                mod.set_attr(rigid["shapeType"], shapes.get(
                    initial_shape,

                    # Fallback, this should never really happen
                    commands.BoxShape
                ))

        # Auto connect
        if auto_connect != "Nothing":
            can_connect = previous is not None and not passive

            if can_connect:
                is_joint = previous.parent().type() == "joint"
                connect_all = auto_connect == "All"
                connect_joints = auto_connect == "Joints Only"

                if connect_all or (connect_joints and is_joint):
                    con = commands.socket_constraint(
                        previous, rigid, scene
                    )

                    if _opt("autoOrient", opts):
                        commands.orient(con)

        if isinstance(rigid, list):
            created.extend(rigid)
        else:
            created.append(rigid)

        previous = rigid

    if created or converted:
        if select:
            all_rigids = [r.parent() for r in created + converted]
            cmds.select(map(str, all_rigids), replace=True)

        log.info("Created %d rigid bodies", len(created + converted))
        return kSuccess

    else:
        return log.warning("Nothing happened, that was unexpected")


@_replayable
@commands.with_undo_chunk
def create_passive_rigid(selection=None, **opts):
    # Special case of nothing selected, just make a default sphere
    if not selection and not cmdx.selection():
        with cmdx.DagModifier() as mod:
            name = commands._unique_name("rPassive1")
            transform = mod.create_node("transform", name=name)

        cmds.select(transform.path())

    opts["createRigidType"] = "Passive"
    return _replay(create_active_rigid)(selection, **opts)


@commands.with_undo_chunk
def create_link(*args):
    links = []

    scene = _find_current_scene()
    for node in cmdx.selection():
        if not node.isA(cmdx.kJoint):
            return log.error("%s must be a joint" % node)

        link = commands.create_link(node, scene)
        links += [link]

    cmds.select(map(str, links))
    return kSuccess


def _axis_to_vector(axis="x"):
    return {
        "x": cmdx.Vector(1, 0, 0),
        "y": cmdx.Vector(0, 1, 0),
        "z": cmdx.Vector(0, 0, 1),
    }[axis.lower()]


@commands.with_undo_chunk
def create_muscle(selection=None, **opts):
    try:
        a, b = selection or cmdx.selection()
    except ValueError:
        return log.warning("Select root and tip anchors of new muscle")

    if not all(node.isA(cmdx.kTransform) for node in (a, b)):
        return log.error(
            "Select two transforms for root and tip anchors of muscle"
        )

    if not all(node.parent() for node in (a, b)):
        return log.error(
            "Anchors must have a parent, see muscle documentation for details"
        )

    new_scene = not cmdx.ls(type="rdScene")
    scene = _find_current_scene()

    if new_scene:
        # Muscles work best with the PGS solver, fow now
        log.info("Swapping TGS for PGS for better muscle simulation results")
        with cmdx.DagModifier() as mod:
            mod.set_attr(scene["solverType"], commands.PGSSolverType)
            mod.set_attr(scene["gravity"], 0)

    kwargs = {
        "up_axis": _axis_to_vector(_opt("muscleUpAxis", opts)),
        "aim_axis": _axis_to_vector(_opt("muscleAimAxis", opts)),
        "flex": _opt("muscleFlex", opts),
        "radius": _opt("muscleRadius", opts),
    }

    muscle, root, tip = tools.make_muscle(a, b, scene, **kwargs)

    cmds.select(muscle.parent().path())
    return kSuccess


def _validate_transforms(nodes, tolerance=0.01):
    """Check for unsupported features in nodes of `root`"""
    negative_scaled = []
    positive_scaled = []
    axes = []
    issues = []

    for node in nodes:
        tm = node.transform(cmdx.sWorld)
        if any(value < 0 - tolerance for value in tm.scale()):
            negative_scaled += [node]

        if any(value > 1 + tolerance for value in tm.scale()):
            positive_scaled += [node]

        axis = node["rotateAxis"].read()
        if (any(abs(value) > tolerance for value in axis)):
            axes += [node]

    if negative_scaled:
        issues += [
            "%d node(s) has negative scale\n%s" % (
                len(negative_scaled),
                "\n".join(" - %s" % node for node in negative_scaled),
            )
        ]

    if positive_scaled:
        issues += [
            "%d node(s) were scaled\n%s" % (
                len(positive_scaled),
                "\n".join(" - %s" % node for node in positive_scaled),
            )
        ]

    if axes:
        issues += [
            "%d node(s) had a custom rotate axis\n%s" % (
                len(axes),
                "\n".join(" - %s" % node for node in axes),
            )
        ]

    if issues:
        for issue in issues:
            log.warning(issue)

        log.warning("%d %s" % (
            len(issues),
            "issue was found" if len(issues) == 1 else
            "issues were found"
        ))

    return False if issues else True


@_replayable
@commands.with_undo_chunk
def create_character(selection=None, **opts):
    scene = _find_current_scene()
    root = selection or cmdx.selection()

    if not root or root[0].type() != "joint":
        return log.warning("Select root joint from which to create character")

    if len(root) > 1:
        return log.warning(
            "Multiple roots selected, select the root of 1 hierarchy"
        )

    # Operate only on first selected joint, to avoid
    # the tragic fate of accidentally making 100 ragdolls
    # (Could still be done via scripting)
    root = root[0]

    hierarchy = [root]
    hierarchy += [
        joint for joint in root.descendents(type="joint")
        if joint.child(type="joint")
    ]

    if not _validate_transforms(hierarchy):
        return

    kwargs = {
        "copy": _opt("characterCopy", opts),
        "control": _opt("characterControl", opts),
        "normalise_shapes": _opt("characterNormalise", opts),
    }

    tools.create_character(root, scene, **kwargs)

    cmds.select(str(root))
    log.info("Successfully created character from %s", root)
    return kSuccess


def _find_rigid(node, autocreate=False):
    if node.type() == "rdRigid":
        pass

    elif node.type() in ("transform", "joint"):
        shape = node.shape(type="rdRigid")

        # Automatically convert selection to rigid for the constraints
        if not shape and not node.shape(type="rdLink"):
            if autocreate:
                scene = _find_current_scene(autocreate=autocreate)
                shape = commands.create_active_rigid(node, scene)
            else:
                return log.warning(
                    "%s did not have a rdRigid shape", node.path()
                )

        node = shape

    return node


@commands.with_undo_chunk
def create_constraint(selection=None, **opts):
    select = _opt("constraintSelect", opts)
    constraint_type = _opt("constraintType", opts)
    selection = selection or cmdx.selection()

    if selection and selection[0].type() == "rdConstraint":
        # The user meant to convert/restore a constraint
        return convert_constraint(selection, constraint_type, select)

    try:
        parent, child = selection
    except ValueError:
        return log.warning(
            "Select parent and child rigids, "
            "these will become constrained to each other"
        )

    scene = _find_current_scene(autocreate=True)
    parent = _find_rigid(parent)
    child = _find_rigid(child)

    if any(node is None for node in (parent, child)):
        return log.warning("Must select two rigids")

    kwargs = {
        "parent": parent,
        "child": child,
        "scene": scene,
        "maintain_offset": _opt("maintainOffset", opts),
    }

    if constraint_type == "Point":
        con = commands.point_constraint(**kwargs)

    elif constraint_type == "Orient":
        con = commands.orient_constraint(**kwargs)

    elif constraint_type == "Hinge":
        con = commands.hinge_constraint(**kwargs)

    elif constraint_type == "Socket":
        con = commands.socket_constraint(**kwargs)

    elif constraint_type == "Parent":
        con = commands.parent_constraint(**kwargs)

    else:
        return log.warning(
            "Unrecognised constraint type '%s'" % constraint_type
        )

    guide_strength = _opt("constraintGuideStrength", opts)
    if guide_strength > 0:
        with cmdx.DagModifier() as mod:
            mod.set_attr(con["driveStrength"], guide_strength)

    if select:
        print("I'm selecting, because opts: %s" % opts)
        cmds.select(con.path(), replace=True)

    log.info("Constrained %s to %s" % (child, parent))
    return kSuccess


@commands.with_undo_chunk
def convert_constraint(selection=None, constraint_type=None, select=False):
    converted = []

    if constraint_type is None:
        constraint_type = options.read("convertConstraintType")

    for node in selection or cmdx.selection():
        con = node

        if not node.type() == "rdConstraint":
            con = node.shape(type="rdConstraint")

        if not con:
            log.warning("No constraint found for %s", node)
            continue

        if constraint_type == "Point":
            converted += [commands.convert_to_point(con)]

        elif constraint_type == "Orient":
            converted += [commands.convert_to_orient(con)]

        elif constraint_type == "Parent":
            converted += [commands.convert_to_parent(con)]

        elif constraint_type == "Hinge":
            converted += [commands.convert_to_hinge(con)]

        elif constraint_type == "Socket":
            converted += [commands.convert_to_socket(con)]

        else:
            # Raise errors, instead of logging directly, such that the
            # error message references the calling function instead of this,
            # helper-level function
            log.warning("Unrecognised constraint type '%s'", constraint_type)
            break

    if not converted:
        return log.warning("Nothing converted")

    elif select:
        cmds.select(map(str, converted), replace=True)

    log.info("Converted %d constraints" % len(converted))
    return kSuccess


@commands.with_undo_chunk
def convert_rigid(selection=None, passive=None):
    converted = []

    for node in selection or cmdx.selection():
        rigid = node

        if node.isA(cmdx.kTransform):
            rigid = node.shape(type="rdRigid")

        if not rigid or rigid.type() != "rdRigid":
            log.warning("Couldn't convert %s" % node)

        if passive is None:
            # Unless specified, invert the current rigid type
            typ = options.read("convertRigidType")

            if typ == "Auto":
                # Toggle between kinematic and dynamic
                typ = "Dynamic" if rigid["kinematic"] else "Kinematic"

            passive = typ == "Kinematic"

        commands.convert_rigid(rigid, passive)
        converted.append(rigid)

    if not converted:
        return log.warning("Noting converted")

    log.info("%d rigids converted", len(converted))
    return kSuccess


@commands.with_undo_chunk
def convert_to_socket(node):
    con = node.shape(type="rdConstraint")

    if con is None:
        return log.warning(
            "Couldn't find an existing constraint to convert, "
            "did you mean to select parent and child?"
        )

    commands.convert_to_socket(con)
    log.info("Converted %s -> Socket", con.path())
    return kSuccess


@_replayable
@commands.with_undo_chunk
def create_point_constraint(selection=None, **opts):
    opts = dict(opts, **{"constraintType": "Point"})
    return create_constraint(selection, **opts)


@_replayable
@commands.with_undo_chunk
def create_orient_constraint(selection=None, **opts):
    opts = dict(opts, **{"constraintType": "Orient"})
    return create_constraint(selection, **opts)


@_replayable
@commands.with_undo_chunk
def create_parent_constraint(selection=None, **opts):
    opts = dict(opts, **{"constraintType": "Parent"})
    return create_constraint(selection, **opts)


@_replayable
@commands.with_undo_chunk
def create_hinge_constraint(selection=None, **opts):
    opts = dict(opts, **{"constraintType": "Hinge"})
    return create_constraint(selection, **opts)


@_replayable
@commands.with_undo_chunk
def create_socket_constraint(selection=None, **opts):
    opts = dict(opts, **{"constraintType": "Socket"})
    return create_constraint(selection, **opts)


@commands.with_undo_chunk
def set_initial_state(selection=None, **opts):
    rigids = []
    selection = selection or cmdx.selection()

    # Initialise everything
    if not selection:
        selection = cmdx.ls(type="rdRigid")

    for rigid in selection:
        if rigid.isA(cmdx.kTransform):
            rigid = rigid.shape(type="rdRigid")

        if rigid and rigid.type() == "rdRigid":
            rigids += [rigid]

    commands.set_initial_state(rigids)

    log.info(
        "Successfully set initial state for %d rigid bodies.", len(rigids)
    )
    return kSuccess


@_replayable
@commands.with_undo_chunk
def create_driven_control(selection=None, **opts):
    controls = []
    selection = selection or cmdx.selection()

    if len(selection) == 1:
        actor = selection[0]

        if actor.isA(cmdx.kTransform):
            actor = selection[0].shape(type="rdRigid")

        if not actor:
            return log.warning("%s was not a Ragdoll Rigid", selection[0])

        _, ctrl, _ = commands.create_absolute_control(actor)
        controls += [ctrl.parent().path()]

    elif len(selection) == 2:
        reference, actor = selection

        if actor.isA(cmdx.kTransform):
            actor = selection[1].shape(type="rdRigid")

        if not actor:
            return log.warning("%s was not a Ragdoll Rigid", selection[1])

        if actor.sibling(type="rdConstraint"):
            ctrl = commands.create_active_control(reference, actor)
        else:
            _, ctrl, _ = commands.create_absolute_control(actor, reference)

        controls += [reference.path()]

    else:
        return log.warning(
            "Select one rigid, or one rigid and a reference transform"
        )

    cmds.select(controls)
    return kSuccess


@_replayable
@commands.with_undo_chunk
def create_kinematic_control(selection=None, **opts):
    controls = []

    for node in selection or cmdx.selection():
        actor = node

        if actor.isA(cmdx.kTransform):
            actor = node.shape(type="rdRigid")

        if not actor:
            log.warning("%s was not an Ragdoll Rigid", node)
            continue

        con = commands.create_kinematic_control(actor)
        controls += [con.path()]

    if not controls:
        return log.warning("Nothing happened, did you select a rigid?")
    else:
        cmds.select(controls)
        return kSuccess


@commands.with_undo_chunk
def transfer_selected(selection=None):
    try:
        a, b = selection or cmdx.selection()
    except ValueError:
        return log.warning(
            "Select source and destination rigids, in that order"
        )

    commands.transfer_attributes(a, b, mirror=True)

    log.info("Transferred attributes from %s -> %s", a, b)
    return kSuccess


def edit_constraint_frames(selection=None):
    frames = []

    for node in selection or cmdx.selection():
        con = node

        if con.isA(cmdx.kTransform):
            con = node.shape(type="rdConstraint")

        if not con:
            log.warning("%s had no constraint", node)
            continue

        frames.extend(commands.edit_constraint_frames(con))

    log.info("Created %d frames", len(frames))
    cmds.select(map(str, frames))
    return kSuccess


def _create_force(selection=None, force_type=None):
    # To specific rigids, or all of them
    selection = cmdx.selection() or cmdx.ls(type="rdRigid")

    rigids = []
    for node in selection:
        rigid = node

        if rigid.isA(cmdx.kTransform):
            rigid = node.shape(type="rdRigid")

        if not rigid:
            log.warning("%s was not an Ragdoll Rigid", node)
            continue

        rigids += [rigid]

    if not rigids:
        return log.warning("No rigids found")

    scene = _find_current_scene(autocreate=False)
    force = commands.create_force(force_type, rigid, scene)

    for rigid in rigids:
        commands.assign_force(rigid, force)

    cmds.select(force.parent().path())
    return kSuccess


@commands.with_undo_chunk
def create_push_force(selection=None):
    return _create_force(selection, commands.PushForce)


@commands.with_undo_chunk
def create_pull_force(selection=None):
    return _create_force(selection, commands.PullForce)


@commands.with_undo_chunk
def create_uniform_force(selection=None):
    return _create_force(selection, commands.UniformForce)


@commands.with_undo_chunk
def create_turbulence_force(selection=None):
    return _create_force(selection, commands.TurbulenceForce)


@commands.with_undo_chunk
def create_slice(selection=None):
    scene = _find_current_scene(autocreate=False)
    slice = commands.create_slice(scene)
    cmds.select(slice.parent().path())
    log.info("Created %s", slice)
    return kSuccess


@commands.with_undo_chunk
def assign_force(selection=None):
    sel = selection or cmdx.selection()

    if len(sel) < 2:
        return log.warning(
            "Select rigid body followed by one or more forces to assign"
        )

    force, targets = sel[0], sel[1:]

    if force.isA(cmdx.kTransform):
        force = force.shape(type="rdForce")

    if not force or force.type() != "rdForce":
        return log.warning("%s was not a force", sel[0])

    assignments = []
    for node in targets:
        target = node

        if target.isA(cmdx.kTransform):
            target = node.shape(type="rdRigid")

        if not target or target.type() != "rdRigid":
            log.warning("%s was not a rigid body", node)
            continue

        if commands.assign_force(target, force):
            assignments += [force]

    if assignments:
        return log.info("Assigned %s to %d rigids", force, len(assignments))
    else:
        log.warning("No forces assigned")
        return kSuccess


@commands.with_undo_chunk
def duplicate_selected(selection=None, **opts):
    selection = cmdx.selection()
    cmds.select(deselect=True)

    duplicates = []
    for node in selection:
        rigid = node

        if rigid.isA(cmdx.kTransform):
            rigid = node.shape(type="rdRigid")

        if not rigid:
            log.warning("%s skipped, not a rigid", node)
            continue

        log.info("Duplicating %s", rigid)

        dup = commands.duplicate(rigid)
        duplicates += [dup]

        cmds.select(dup.parent().path(), add=True)

    if duplicates:
        log.info("Duplicated %d rigids", len(duplicates))
        return kSuccess
    else:
        return log.warning("Nothing duplicated")


@commands.with_undo_chunk
def delete_physics(selection=None, **opts):
    if _opt("deleteFromSelection", opts):
        selection = selection or cmdx.selection(type="dagNode")

        if not selection:
            count = commands.delete_all_physics()

        else:
            shapes = []
            for node in selection:
                shapes += node.shapes()

            shapes = filter(None, shapes)
            shapes = list(shapes) + selection
            shapes = filter(lambda shape: shape.isA(cmdx.kShape), shapes)

            count = commands.delete_physics(shapes)

    else:
        count = commands.delete_all_physics()

    if count:
        log.info("Deleted %d Ragdoll nodes", count)
        return kSuccess

    else:
        return log.warning("Nothing deleted")


@_replayable
@commands.with_undo_chunk
def create_dynamic_control(selection=None, **opts):
    chain = selection or cmdx.selection(type="transform")

    if not chain or len(chain) < 2:
        return log.warning(
            "Select two or more animation controls, "
            "in the order they should be connected. "
            "The first selection will be passive (i.e. animated)."
        )

    if not _validate_transforms(chain):
        return

    scene = _find_current_scene()

    kwargs = {
        "use_capsules": _opt("dynamicControlShapeType", opts) == "Capsule",
        "auto_blend": _opt("dynamicControlAutoBlend", opts),
        "auto_influence": _opt("dynamicControlAutoInfluence", opts),
        "auto_multiplier": _opt("dynamicControlAutoMultiplier", opts),
        "auto_initial_state": _opt("dynamicControlAutoInitialState", opts),
        "auto_world_constraint": _opt("dynamicControlAutoWorldspace", opts),
        "central_blend": (
            _opt("dynamicControlBlendMethod", opts) == "From Root"
        ),
    }

    for ctrl in chain:
        if not has_valid_rotatepivot(ctrl):
            return

    try:
        tools.create_dynamic_control(chain, scene, **kwargs)

    except Exception as e:
        # Turn this into a friendly warning
        _print_exception()
        log.warning(str(e))
        return kFailure

    else:
        root = chain[0]
        cmds.select(str(root))

    return kSuccess


@commands.with_undo_chunk
def convert_to_polygons(selection=None):
    meshes = []

    for node in cmdx.selection(type=("transform", "rdRigid", "rdControl")):
        actor = node

        if actor.isA(cmdx.kTransform):
            actor = node.shape(type=("rdRigid", "rdControl"))

        if not actor:
            log.warning("%s was not a rdRigid or rdControl" % node)
            continue

        mesh = tools.convert_to_polygons(actor)
        meshes += [mesh.parent().path()]

    if meshes:
        cmds.select(meshes)
        log.info("Converted %d rigids to polygons" % len(meshes))
        return kSuccess
    else:
        return log.warning("Nothing converted")


def normalise_shapes(selection=None):
    selection = selection or cmdx.selection()

    if not selection:
        return log.warning("Select root of hierarchy to normalise it")

    root = selection[0]

    if root.isA(cmdx.kShape):
        root = root.parent()

    commands.normalise_shapes(root)

    return True


def multiply_rigids(selection=None):
    rigids = _filtered_selection("rdRigid")

    if not rigids:
        return False

    root = rigids[0].parent()

    selected_channels = _selected_channels()
    mult = commands.multiply_rigids(
        rigids, parent=root, channels=selected_channels
    )

    cmds.select(str(mult))

    return True


def multiply_constraints(selection=None):
    constraints = _filtered_selection("rdConstraint")

    if not constraints:
        return False

    root = constraints[0].parent()
    selected_channels = _selected_channels()

    mult = commands.multiply_constraints(
        constraints, parent=root, channels=selected_channels
    )

    cmds.select(str(mult))

    return True


def select_rigids(selection=None):
    selection = cmds.ls(selection=True)

    if selection:
        cmds.select(cmds.ls(selection, type="rdRigid"))
    else:
        cmds.select(cmds.ls(type="rdRigid"))


def select_constraints(selection=None):
    selection = cmds.ls(selection=True)

    if selection:
        cmds.select(cmds.ls(selection, type="rdConstraint"))
    else:
        cmds.select(cmds.ls(type="rdConstraint"))


#
# User Interface
#


def _last_command():
    """Store repeatable command at module-level

    This assumes no threading happens.

    """

    _last_command._func()


def repeatable(func):
    """Make `func` repeatable in Maya

    See https://groups.google.com/g/python_inside_maya
               /c/2GO5PGD6Q6w/m/U-97zyB_DAAJ

    """

    @functools.wraps(func)
    def _wrapper(*args, **kwargs):
        _last_command._func = func

        command = 'python("import {0};{0}._last_command()")'.format(__name__)
        result = func()

        try:
            cmds.repeatLast(
                addCommand=command,
                addCommandLabel=func.__name__
            )
        except Exception:
            pass

        return result
    return _wrapper


def welcome_user(*args):
    parent = ui.MayaWindow()
    win = ui.SplashScreen(parent)
    win.show()
    win.activateWindow()

    # Maya automatically centers new windows,
    # sometimes. On some platforms. Trust no one.
    ui.center_window(win)

    return win


def _Arg(var, label=None, callback=None):
    var = __.optionvars[var]
    var = copy.deepcopy(var)  # Allow edits to internal lists etc.

    # Special case
    if var["name"] == "solver":
        scenes = [n.shortestPath() for n in cmdx.ls(type="rdScene")]
        var["items"] = scenes + var["items"]
        var["default"] = var["items"][0]
        var["initial"] = None  # Always prefer the latest created scene
        options.write(var)  # Update this whenever the window is shown

    if label is not None:
        var["label"] = label

    # Restore persistent values, from Maya preferences
    optionvar = options.read(var)
    if optionvar is not None:
        var["initial"] = optionvar

    depends = var.pop("depends", [])
    for dependency in depends:
        pass

    cls = getattr(qargparse, var.pop("type"))
    arg = cls(**var)

    if callback is not None:
        arg.changed.connect(callback)

    return arg


def _Window(key, command):
    parent = ui.MayaWindow()
    menuitem = __.menuitems[key]
    args = map(_Arg, menuitem.get("options", []))

    win = ui.Options(
        key,
        args,
        command=repeatable(command),
        icon=_resource("icons", menuitem["icon"]),
        description=menuitem["summary"],
        media=menuitem.get("media", []),
        parent=parent
    )

    # On Windows, windows typically spawn in the
    # center of the screen. On Linux? Flip a coin.
    ui.center_window(win)

    win.show()

    return win


def global_preferences(*args):
    def callback():
        time = cmds.currentTime(query=True)
        cmds.evalDeferred(lambda: cmds.currentTime(time, update=True))

    def global_preferences():
        pass

    window = _Window("globalPreferences", global_preferences)

    # Update viewport immediately whenever this changes
    scale = window.parser.find("scale")
    scale.changed.connect(callback)

    return window


def create_rigid_options(*args):
    window = _Window("activeRigid", create_active_rigid)
    return window


def create_passive_options(selection=None):
    window = _Window("passiveRigid", create_passive_rigid)
    return window


def convert_constraint_options(*args):
    window = _Window("convertConstraint", convert_constraint)
    return window


def convert_rigid_options(*args):
    window = _Window("convertRigid", convert_rigid)
    return window


def _constraint_options(typ):
    def _create_constraint_options(*args):
        # Preselect whatever the user picked
        options.write("constraintType", typ)
        window = _Window("constraint", create_constraint)
        return window

    return _create_constraint_options


def create_kinematic_control_options(*args):
    return _Window("kinematic", create_kinematic_control)


def create_driven_control_options(*args):
    return _Window("guide", create_driven_control)


def create_push_force_options(*args):
    return _Window("push", create_push_force)


def create_pull_force_options(*args):
    return _Window("pull", create_pull_force)


def create_uniform_force_options(*args):
    return _Window("directional", create_uniform_force)


def create_turbulence_force_options(*args):
    return _Window("wind", create_turbulence_force)


def multiply_rigids_options(*args):
    return _Window("multiplyRigids", multiply_rigids)


def multiply_constraints_options(*args):
    return _Window("multiplyConstraints", create_turbulence_force)


def create_character_options(*args):
    window = _Window("character", create_character)

    # Create dependencies between arguments
    control = window.parser.find("characterControl")
    copy = window.parser.find("characterCopy")
    control["condition"] = copy.read

    return window


def create_muscle_options(*args):
    return _Window("muscle", create_muscle)


def create_dynamic_control_options(*args):
    return _Window("createDynamicControl", create_dynamic_control)


def select_rigids_options(*args):
    return _Window("selectRigids", select_rigids)


def select_constraints_options(*args):
    return _Window("selectConstraints", select_constraints)


def delete_physics_options(*args):
    return _Window("deleteAllPhysics", delete_physics)


# Backwards compatibility
create_rigid = create_active_rigid
create_collider = create_passive_rigid
