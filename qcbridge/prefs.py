"""Addon preferences, path-mapping table, and the session panel.

Role is configuration, never OS detection. All sync/stream machinery starts
only on explicit session start (operators below); enabling the addon is inert.
"""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup, UIList

from .ring0 import kiosk, session

ENCODER_RUNGS = [
    # (identifier, label, description) — judged on a production scene,
    # spikes/stage0-capture-harness/results/windows-v0-matrix/notes.md
    (
        "hevc_10_420_100",
        "HEVC 10-bit 4:2:0 100M (default)",
        "Fully GPU-resident capture+encode; headroom while Cycles saturates the machine",
    ),
    (
        "hevc_10_420_50",
        "HEVC 10-bit 4:2:0 50M",
        "Step-down rung; same zero-copy path at half the bitrate",
    ),
    (
        "hevc_10_444_50",
        "HEVC 10-bit 4:4:4 50M (chroma QC)",
        "True 4:4:4 requires the CPU conversion path; verify receiver-side pix_fmt",
    ),
]


class QCB_PathMapping(PropertyGroup):
    """One prefix-pair row (decision #15). Wire form is Windows-canonical."""

    win: StringProperty(name="Windows root", default="")
    mac: StringProperty(name="macOS root", default="")
    enabled: BoolProperty(name="Enabled", default=True)
    label: StringProperty(name="Label", default="")


class QCB_UL_path_mappings(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        row.prop(item, "label", text="", emboss=False)
        row.prop(item, "win", text="")
        row.prop(item, "mac", text="")


class QCB_OT_mapping_add(Operator):
    bl_idname = "qcbridge.mapping_add"
    bl_label = "Add Path Mapping"

    def execute(self, context):
        prefs = get_prefs(context)
        prefs.path_mappings.add()
        prefs.active_mapping_index = len(prefs.path_mappings) - 1
        return {"FINISHED"}


class QCB_OT_mapping_remove(Operator):
    bl_idname = "qcbridge.mapping_remove"
    bl_label = "Remove Path Mapping"

    @classmethod
    def poll(cls, context):
        return len(get_prefs(context).path_mappings) > 0

    def execute(self, context):
        prefs = get_prefs(context)
        prefs.path_mappings.remove(prefs.active_mapping_index)
        prefs.active_mapping_index = min(
            prefs.active_mapping_index, len(prefs.path_mappings) - 1
        )
        return {"FINISHED"}


class QCB_OT_session_start(Operator):
    bl_idname = "qcbridge.session_start"
    bl_label = "Start Session"
    bl_description = "Start the sync session in the configured role"

    @classmethod
    def poll(cls, context):
        return not session.running()

    def execute(self, context):
        result = session.start(get_prefs(context))
        if result != "ok":
            self.report({"WARNING"}, result)
            return {"CANCELLED"}
        return {"FINISHED"}


class QCB_OT_session_stop(Operator):
    bl_idname = "qcbridge.session_stop"
    bl_label = "Stop Session"

    @classmethod
    def poll(cls, context):
        return session.running()

    def execute(self, context):
        session.stop()
        return {"FINISHED"}


class QCB_OT_session_pause(Operator):
    bl_idname = "qcbridge.session_pause"
    bl_label = "Pause Sync"
    bl_description = (
        "Hold outgoing changes in the dirty set; the replica keeps its last state, "
        "labeled stale in the stream"
    )

    @classmethod
    def poll(cls, context):
        return session.running() and get_prefs(context).role == "HOST"

    def execute(self, context):
        session.state["paused"] = not session.state["paused"]
        return {"FINISHED"}


class QCB_OT_force_resync(Operator):
    bl_idname = "qcbridge.force_resync"
    bl_label = "Force Resync"
    bl_description = "Reship the whole file to the replica (tier 3)"

    @classmethod
    def poll(cls, context):
        return session.running() and get_prefs(context).role == "HOST"

    def execute(self, context):
        result = session.force_resync()
        if result != "ok":
            self.report({"WARNING"}, result)
            return {"CANCELLED"}
        return {"FINISHED"}


class QCB_OT_kiosk_toggle(Operator):
    bl_idname = "qcbridge.kiosk_toggle"
    bl_label = "Toggle Minimal Mode"
    bl_description = (
        "Enter/exit kiosk (minimal) mode. Shortcut: Ctrl+Alt+K — the way "
        "back out when the UI is hidden. Ctrl+Q still quits Blender either way"
    )

    def execute(self, context):
        if kiosk.active():
            kiosk.exit_now()
        else:
            kiosk.enter_now()
        return {"FINISHED"}


# ── settings persistence ─────────────────────────────────────────────────────
# Prefs live in userpref.blend and are DELETED when the extension is removed
# (a Remove-then-install "update" wipes them; crashes can lose them too).
# Mirror them to a JSON in Blender's config dir — outside the extension — and
# auto-restore onto a fresh install.

_SETTINGS_FIELDS = (
    "role", "replica_address", "bind_address", "port_control", "port_hot",
    "port_cold", "token", "enable_stream", "srt_port", "srt_url",
    "encoder_rung", "ffmpeg_path", "replica_kiosk",
)


def _settings_path() -> str:
    import os

    return os.path.join(
        bpy.utils.user_resource("CONFIG", create=True), "qcbridge_settings.json"
    )


def save_settings(prefs) -> str:
    import json

    data = {name: getattr(prefs, name) for name in _SETTINGS_FIELDS}
    data["path_mappings"] = [
        {"win": m.win, "mac": m.mac, "enabled": m.enabled, "label": m.label}
        for m in prefs.path_mappings
    ]
    path = _settings_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


def load_settings(prefs) -> bool:
    import json

    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    for name in _SETTINGS_FIELDS:
        if name in data:
            try:
                setattr(prefs, name, data[name])
            except (TypeError, ValueError):
                pass  # field changed shape across versions; keep default
    if "path_mappings" in data:
        while prefs.path_mappings:
            prefs.path_mappings.remove(0)
        for row in data["path_mappings"]:
            m = prefs.path_mappings.add()
            m.win = row.get("win", "")
            m.mac = row.get("mac", "")
            m.enabled = row.get("enabled", True)
            m.label = row.get("label", "")
    return True


def _maybe_restore():
    """Runs once shortly after register: a fresh install (empty token AND
    empty address AND no mappings) gets last-saved settings back."""
    addon = bpy.context.preferences.addons.get(__package__)
    if addon is None:
        return None
    prefs = addon.preferences
    fresh = (
        not prefs.token and not prefs.replica_address
        and len(prefs.path_mappings) == 0
    )
    if fresh and load_settings(prefs):
        print("qcbridge: settings restored from", _settings_path(), flush=True)
    return None


class QCB_OT_settings_save(Operator):
    bl_idname = "qcbridge.settings_save"
    bl_label = "Save Settings"
    bl_description = "Save all qcbridge settings to a file that survives addon removal/updates"

    def execute(self, context):
        path = save_settings(get_prefs(context))
        self.report({"INFO"}, f"saved: {path}")
        return {"FINISHED"}


class QCB_OT_settings_load(Operator):
    bl_idname = "qcbridge.settings_load"
    bl_label = "Load Settings"
    bl_description = "Restore qcbridge settings from the saved file"

    def execute(self, context):
        if load_settings(get_prefs(context)):
            self.report({"INFO"}, "settings loaded")
            return {"FINISHED"}
        self.report({"WARNING"}, "no saved settings found")
        return {"CANCELLED"}


class QCB_OT_shot_mode(Operator):
    bl_idname = "qcbridge.shot_mode"
    bl_label = "Shot Mode"
    bl_description = (
        "Replica holds the fitted camera frame (passepartout opaque, your "
        "navigation ignored, timeline still follows) — the A/B-alignable "
        "QC framing. Toggle off to return to follow mode"
    )

    @classmethod
    def poll(cls, context):
        return session.running() and get_prefs(context).role == "HOST"

    def execute(self, context):
        session.shot_mode_toggle()
        return {"FINISHED"}


class QCB_OT_replica_zoom(Operator):
    bl_idname = "qcbridge.replica_zoom"
    bl_label = "Replica Zoom"
    bl_description = (
        "Nudge the replica's camera-view zoom without changing your own "
        "view (an offset on top of the followed zoom)"
    )

    delta: FloatProperty(default=0.0)
    reset: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return session.running() and get_prefs(context).role == "HOST"

    def execute(self, context):
        session.replica_zoom(self.delta, self.reset)
        return {"FINISHED"}


class QCB_OT_open_qcview(Operator):
    bl_idname = "qcbridge.open_qcview"
    bl_label = "Open in QCView"
    bl_description = "Launch QCView on the replica's live stream (deep link)"

    @classmethod
    def poll(cls, context):
        return bool(session.qcview_deep_link())

    def execute(self, context):
        import subprocess
        import sys

        link = session.qcview_deep_link()
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", link])
            elif sys.platform == "win32":
                import os

                os.startfile(link)  # MSIX protocol registration routes it
            else:
                subprocess.Popen(["xdg-open", link])
        except OSError as exc:
            self.report({"ERROR"}, f"could not launch QCView: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class QCB_OT_copy_stream_url(Operator):
    bl_idname = "qcbridge.copy_stream_url"
    bl_label = "Copy Stream URL"
    bl_description = "Copy the viewer SRT URL (for any player, e.g. ffplay)"

    @classmethod
    def poll(cls, context):
        return bool(session.viewer_url())

    def execute(self, context):
        context.window_manager.clipboard = session.viewer_url()
        self.report({"INFO"}, "stream URL copied")
        return {"FINISHED"}


class QCBridgePreferences(AddonPreferences):
    bl_idname = __package__

    role: EnumProperty(
        name="Role",
        items=[
            ("HOST", "Host", "The workstation you edit on — the single writer"),
            ("REPLICA", "Replica", "The unattended GPU box that renders and streams"),
        ],
        default="HOST",
    )

    # Host: address of the replica to connect to (stable VPN IP).
    # Replica listens; host connects (decision #16).
    replica_address: StringProperty(
        name="Replica Address",
        description="VPN IP of the replica (host role connects to it)",
        default="",
    )
    bind_address: StringProperty(
        name="Bind Address",
        description="Interface the replica listens on (tunnel IP, or 0.0.0.0)",
        default="0.0.0.0",
    )
    port_control: IntProperty(name="Control Port", default=9990, min=1024, max=65535)
    port_hot: IntProperty(name="Hot Port", default=9991, min=1024, max=65535)
    port_cold: IntProperty(name="Cold Port", default=9992, min=1024, max=65535)

    token: StringProperty(
        name="Session Token",
        description="Pre-shared token; both ends must match (decision #7)",
        default="",
        subtype="PASSWORD",
    )

    # Replica pixel path (stage-0 proven values; parametrized — no hardcoded hosts).
    enable_stream: BoolProperty(
        name="Pixel Stream",
        description=(
            "Stream the replica viewport over SRT (full mode). Disable for "
            "sync-only mode: view the replica through your own remote-desktop "
            "tool instead (decision #17)"
        ),
        default=True,
    )
    srt_port: IntProperty(
        name="Stream Port",
        description="SRT port the replica listens on (URL is auto-generated)",
        default=9998, min=1024, max=65535,
    )
    srt_latency_ms: IntProperty(
        name="Stream Latency (ms)",
        description=(
            "SRT retransmission buffer — a fixed add to glass-to-glass "
            "latency. 300 is very safe; ~120 is fine on a clean low-RTT VPN"
        ),
        default=300, min=20, max=2000,
    )
    srt_url: StringProperty(
        name="SRT URL Override",
        description=(
            "Leave empty to auto-generate from the bind address and stream "
            "port. Set only for unusual topologies (e.g. caller-mode sender)"
        ),
        default="",
    )
    # (SRT passphrase is derived from the session token on both ends —
    # deliberately not user-configurable; decision #16 amendment.)
    encoder_rung: EnumProperty(
        name="Encoder Rung", items=ENCODER_RUNGS, default="hevc_10_420_100"
    )
    ffmpeg_path: StringProperty(
        name="FFmpeg Path",
        description=(
            "Leave empty to auto-detect: QCView's bundled ffmpeg "
            "(toolbox.json), then PATH. Set only to override"
        ),
        default="",
        subtype="FILE_PATH",
    )

    replica_kiosk: BoolProperty(
        name="Kiosk Mode",
        description="Strip all chrome on session start: display = viewport",
        default=True,
    )

    path_mappings: CollectionProperty(type=QCB_PathMapping)
    active_mapping_index: IntProperty(default=0)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "role")

        box = layout.box()
        box.label(text="Connection")
        if self.role == "HOST":
            box.prop(self, "replica_address")
        else:
            box.prop(self, "bind_address")
        row = box.row(align=True)
        row.prop(self, "port_control")
        row.prop(self, "port_hot")
        row.prop(self, "port_cold")
        box.prop(self, "token")

        if self.role == "REPLICA":
            box = layout.box()
            box.label(text="Pixel Path")
            box.prop(self, "enable_stream")
            sub = box.column()
            sub.enabled = self.enable_stream
            sub.prop(self, "srt_port")
            sub.prop(self, "srt_latency_ms")
            sub.prop(self, "srt_url")
            sub.prop(self, "encoder_rung")
            sub.prop(self, "ffmpeg_path")
            box.prop(self, "replica_kiosk")

        row = layout.row(align=True)
        row.operator("qcbridge.settings_save", icon="EXPORT")
        row.operator("qcbridge.settings_load", icon="IMPORT")

        box = layout.box()
        box.label(text="Path Mappings (Windows root ↔ macOS root)")
        row = box.row()
        row.template_list(
            "QCB_UL_path_mappings", "", self, "path_mappings",
            self, "active_mapping_index",
        )
        col = row.column(align=True)
        col.operator("qcbridge.mapping_add", icon="ADD", text="")
        col.operator("qcbridge.mapping_remove", icon="REMOVE", text="")


class QCB_PT_main(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "QC Bridge"
    bl_label = "QC Bridge"

    def draw(self, context):
        layout = self.layout
        prefs = get_prefs(context)
        if session.running():
            layout.label(text=f"Role: {prefs.role.title()}")
        else:
            layout.prop(prefs, "role", text="Role")
        layout.label(text=f"Status: {session.status_text()}")
        if session.running():
            layout.operator("qcbridge.session_stop", icon="PAUSE")
            if prefs.role == "HOST":
                label = "Resume Sync" if session.state["paused"] else "Pause Sync"
                layout.operator("qcbridge.session_pause", text=label)
                layout.operator("qcbridge.force_resync", icon="FILE_REFRESH")
                row = layout.row(align=True)
                row.operator("qcbridge.open_qcview", icon="PLAY")
                row.operator("qcbridge.copy_stream_url", text="", icon="COPYDOWN")
                layout.operator(
                    "qcbridge.shot_mode",
                    icon="CAMERA_DATA",
                    depress=session.state["shot_mode"],
                )
                row = layout.row(align=True)
                row.label(text="Replica Zoom")
                op = row.operator("qcbridge.replica_zoom", text="", icon="REMOVE")
                op.delta = -6.0
                op = row.operator("qcbridge.replica_zoom", text="", icon="ADD")
                op.delta = 6.0
                op = row.operator("qcbridge.replica_zoom", text="", icon="LOOP_BACK")
                op.reset = True
        else:
            layout.operator("qcbridge.session_start", icon="PLAY")
        if prefs.role == "REPLICA":
            label = "Exit Minimal Mode" if kiosk.active() else "Minimal Mode"
            layout.operator("qcbridge.kiosk_toggle", text=label, icon="FULLSCREEN_ENTER")


def get_prefs(context) -> QCBridgePreferences:
    return context.preferences.addons[__package__].preferences


_classes = (
    QCB_PathMapping,
    QCB_UL_path_mappings,
    QCB_OT_mapping_add,
    QCB_OT_mapping_remove,
    QCB_OT_session_start,
    QCB_OT_session_stop,
    QCB_OT_session_pause,
    QCB_OT_force_resync,
    QCB_OT_kiosk_toggle,
    QCB_OT_settings_save,
    QCB_OT_settings_load,
    QCB_OT_shot_mode,
    QCB_OT_replica_zoom,
    QCB_OT_open_qcview,
    QCB_OT_copy_stream_url,
    QCBridgePreferences,
    QCB_PT_main,
)


_keymaps: list = []


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    # Fresh-install auto-restore (deferred: prefs aren't queryable mid-register).
    bpy.app.timers.register(_maybe_restore, first_interval=1.0, persistent=True)
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:  # Ctrl+Alt+K: the escape hatch when kiosk has hidden all UI
        km = kc.keymaps.new(name="Window", space_type="EMPTY")
        kmi = km.keymap_items.new(
            "qcbridge.kiosk_toggle", "K", "PRESS", ctrl=True, alt=True
        )
        _keymaps.append((km, kmi))


def unregister() -> None:
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
