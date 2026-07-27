"""qcbridge — remote Blender beauty monitor.

One addon, two roles (Host / Replica), selected in preferences — never by OS.
Host is the single writer; sync is strictly one-way (docs/architecture.md).

Ring model (docs/decisions.md #11):
  ring0/  bpy adapter — thin, dumb: handlers, applies, UI, process supervision.
  ring1/  sync logic — NO bpy imports, type-hinted, pytest-able outside Blender.
  ring 2  native heavy lifting lives in subprocesses (ffmpeg), not in this package.
"""

from . import prefs


def register() -> None:
    prefs.register()


def unregister() -> None:
    prefs.unregister()
