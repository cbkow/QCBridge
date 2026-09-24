"""Cross-platform path translation (decision #15).

Python port of ufb's mapping engine (ufb/core/src/utils.rs: translate_path_to,
strip_for_win, expand_mapping_prefix, to_native_path), with two upgrades folded
in from ufb's newer identity model: component-boundary matching (a mapping for
/Volumes/share never captures /Volumes/share-2) and longest-prefix-first
ordering (row order never decides between overlapping roots).

The wire form is Windows-canonical: the Host canonicalizes outbound paths, the
Replica localizes on apply. Unmapped paths fall through with separators
converted — callers surface those in the status overlay, never silently.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

WIRE_OS = "win"


@dataclass(frozen=True)
class PathMapping:
    """One prefix-pair row, as configured in the addon preferences."""

    win: str
    mac: str
    enabled: bool = True
    label: str = ""


def rows_to_wire(mappings) -> list[dict]:
    """The table as plain dicts: what the agent's config holds, what
    set_config sends, and what the host's hello carries to the replica."""
    return [
        {"win": m.win, "mac": m.mac, "enabled": bool(m.enabled), "label": m.label}
        for m in mappings
    ]


def rows_from_wire(rows) -> list[PathMapping]:
    """Dicts back into rows; a malformed entry is skipped, not guessed at."""
    out: list[PathMapping] = []
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        win, mac = r.get("win"), r.get("mac")
        if not isinstance(win, str) or not isinstance(mac, str):
            continue
        out.append(PathMapping(
            win=win, mac=mac, enabled=bool(r.get("enabled", True)), label=str(r.get("label", "")),
        ))
    return out


def merge_tables(own, received) -> list[PathMapping]:
    """This machine's rows first, then the peer's rows it does not already
    have (same roots, case-insensitive on the Windows side). Since
    2026-09-24 the host's rows ride in the hello, so a replica with an empty
    table still localizes; a row entered on both ends is not doubled."""
    def key(m):
        return (m.win.lower().replace("/", "\\").rstrip("\\"), m.mac.rstrip("/"))
    out = list(own)
    seen = {key(m) for m in out}
    for m in received:
        if key(m) not in seen:
            out.append(m)
            seen.add(key(m))
    return out


def current_os_tag() -> str:
    if sys.platform == "win32":
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "lin"


def to_native(path: str, os_tag: str) -> str:
    if os_tag == "win":
        return path.replace("/", "\\")
    return path.replace("\\", "/")


def _expand_home(prefix: str) -> str:
    if prefix == "~" or prefix.startswith(("~/", "~\\")):
        home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
        rest = prefix[1:].lstrip("/\\")
        return f"{home}/{rest}" if rest else home
    return prefix


def _expand_mapping_prefix(prefix: str, os_tag: str) -> str:
    # Expand ~ only when the mapping side refers to the current machine —
    # otherwise we'd be expanding against the wrong home dir.
    return _expand_home(prefix) if os_tag == current_os_tag() else prefix


def _strip_for_win(s: str) -> str:
    # Drive letters in mappings are conventional — the share suffix is what
    # identifies the location. Tolerates legacy/driveless forms on the win side.
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        s = s[2:]
    return s.lstrip("/")


def _mapping_prefix_for(mapping: PathMapping, os_tag: str) -> str | None:
    if os_tag == "win":
        return mapping.win
    if os_tag == "mac":
        return mapping.mac
    return None


def translate(
    source_os: str,
    target_os: str,
    path: str,
    mappings: list[PathMapping] | tuple[PathMapping, ...],
) -> str:
    """Translate `path` from source_os form to target_os form.

    Deliberately no source_os == target_os short-circuit: a same-OS call still
    repairs foreign-form strings (a drive-less or forward-slash Windows path)
    to proper native form. Paths matching no mapping fall through with
    separators converted.
    """
    candidates = []
    for mapping in mappings:
        if not mapping.enabled:
            continue
        source_raw = _mapping_prefix_for(mapping, source_os)
        target_raw = _mapping_prefix_for(mapping, target_os)
        if not source_raw or not target_raw:
            continue
        candidates.append((mapping, source_raw, target_raw))

    # Longest source prefix first: overlapping roots resolve to the most
    # specific rule regardless of row order.
    candidates.sort(key=lambda c: -len(c[1].replace("\\", "/").rstrip("/")))

    norm_path = path.replace("\\", "/")

    for _mapping, source_raw, target_raw in candidates:
        source_prefix = _expand_mapping_prefix(source_raw, source_os)
        target_prefix = _expand_mapping_prefix(target_raw, target_os)
        norm_source = source_prefix.replace("\\", "/")

        if source_os == "win":
            stripped_source = _strip_for_win(norm_source)
            if not stripped_source.strip("/"):
                # Bare drive root (e.g. "U:\"): the drive letter is its only
                # discriminating information — stripping it would leave an
                # empty prefix that matches every path. Compare drive-intact.
                case_path = norm_path
                cmp_path = norm_path.lower()
                cmp_source = norm_source.rstrip("/").lower()
            else:
                case_path = _strip_for_win(norm_path)
                cmp_path = case_path.lower()
                cmp_source = stripped_source.rstrip("/").lower()
        else:
            case_path = norm_path
            cmp_path = norm_path
            cmp_source = norm_source.rstrip("/")

        # Component boundary: the prefix matches whole path components only.
        if cmp_path == cmp_source or cmp_path.startswith(cmp_source + "/"):
            remainder = case_path[len(cmp_source):].lstrip("/\\")
            target_norm = target_prefix.rstrip("/\\")
            translated = f"{target_norm}/{remainder}" if remainder else target_norm
            return to_native(translated, target_os)

    return to_native(path, target_os)


def to_canonical(native_path: str, mappings) -> str:
    """Local native form → Windows-canonical wire form."""
    return translate(current_os_tag(), WIRE_OS, native_path, mappings)


def from_canonical(wire_path: str, mappings) -> str:
    """Windows-canonical wire form → local native form."""
    return translate(WIRE_OS, current_os_tag(), wire_path, mappings)


def localize_any(path: str, mappings) -> str:
    """Host-native path of unknown OS → local native form. A bootstrap or a
    blob carries the host's paths exactly as Blender stored them, in the
    host's own form — nobody canonicalizes them. Try every source form the
    table knows and take the first that matches a mapping; fall through
    unchanged (the caller decides, by existence, whether that is a miss)."""
    local = current_os_tag()
    for source in (WIRE_OS, "mac", "lin"):
        translated = translate(source, local, path, mappings)
        if translated != to_native(path, local):
            return translated
    return path


def is_mapped(source_os: str, path: str, mappings) -> bool:
    """True if some enabled mapping covers `path` — the honesty check:
    unmapped paths crossing the wire get surfaced in the status overlay."""
    target_os = "mac" if source_os == "win" else "win"
    translated = translate(source_os, target_os, path, mappings)
    return translated != to_native(path, target_os)
