"""Progress Tracker store — pure functions on a document dict, thread-safe persistence via PluginState facade."""

from __future__ import annotations

import itertools
import json
import logging
import re
import threading
import time
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Union

__all__ = [
    "PLUGIN_ID",
    "DEFAULTS",
    "bind",
    "configure",
    "get_document",
    "save_document",
    "as_facade",
    "new_track",
    "find_task_index",
    "active_task_index",
    "result_line",
    "apply_op",
    "stamp_source",
    "ROW_PRECEDENCE",
    "is_check_command",
    "normalize_command",
    "_prune_next_ids",
    "close_session_tracks",
]

PLUGIN_ID = "progress-tracker"

DEFAULTS = {
    "max_findings": 200,
    "max_turns_retained": 250,
    "enabled_tools": [],
    "enabled_findings": ["found", "failed"],
}

_logger = logging.getLogger("progress-tracker.store")

_lock = threading.Lock()
_facade: Optional[Any] = None
_config = dict(DEFAULTS)


class _DictFacade:
    """Wrap a plain dict to the PluginState-like get/set protocol."""

    __slots__ = ("_data",)

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


def bind(facade: Any) -> None:
    """Bind a PluginState facade (or any object with get/set) for persistence."""
    global _facade
    with _lock:
        _facade = facade


def as_facade(obj: Any) -> Any:
    """Return a get/set facade for a PluginState or a plain dict."""
    if hasattr(obj, "get") and hasattr(obj, "set"):
        return obj
    return _DictFacade(obj)


def configure(config: Dict[str, Any]) -> None:
    """Update runtime config (merged with DEFAULTS)."""
    global _config
    with _lock:
        _config.update({k: v for k, v in config.items() if k in DEFAULTS})


def _get_facade() -> Any:
    if _facade is None:
        raise RuntimeError("store not bound; call bind(facade) or get_document(facade)")
    return _facade


def get_document(facade: Optional[Any] = None) -> Dict[str, Any]:
    """Load the whole document from the bound facade or the given one."""
    target = as_facade(facade or _get_facade())
    with _lock:
        doc = target.get("progress", None)
    if doc is None:
        doc = _empty_document()
    # Ensure all expected keys exist (migration from older shapes)
    doc.setdefault("tracks", [])
    doc.setdefault("tab", "blocked")
    doc.setdefault("open", None)
    doc.setdefault("turn", None)
    doc.setdefault("log", {"blocks": [], "findings": []})
    doc.setdefault("turns", [])
    doc.setdefault("sessions", {})
    return doc


def save_document(doc: Dict[str, Any], facade: Optional[Any] = None) -> None:
    """Persist the document to the bound facade or the given one."""
    target = as_facade(facade or _get_facade())
    with _lock:
        target.set("progress", doc)


def _empty_document() -> Dict[str, Any]:
    return {
        "tracks": [],
        "tab": "blocked",
        "open": None,
        "turn": None,
        "log": {"blocks": [], "findings": []},
        "turns": [],
        "sessions": {},
    }


# ─── Core timestamps ───

def _now_ms() -> int:
    return int(time.time() * 1000)


# ─── Track & Task construction ───

_TRACK_SEQ = itertools.count(1)


def new_track(title: str, subjects: List[str], track_id: Optional[str] = None,
              session_id: Optional[str] = None) -> Dict[str, Any]:
    """Create a new track with tasks for each subject. session_id scopes the bar to one
    session; None keeps the legacy behaviour of a bar every session can see."""
    now = _now_ms()
    tasks = []
    for i, subj in enumerate(subjects, 1):
        tid = f"step{i}"
        task = {
            "id": tid,
            "label": subj,
            "state": "in_progress" if i == 1 else "pending",
            "note": None,
            "path": None,
            "attempt": 0,
            "maxAttempts": 2,
            "failures": 0,
            "blockReason": None,
            "openedAt": now if i == 1 else None,
            "closedAt": None,
            "ticket": None,
            "nextTaskIds": [],
            "skipped": False,
        }
        tasks.append(task)
    # Build nextTaskIds: each task lists all following ids
    for i, t in enumerate(tasks):
        t["nextTaskIds"] = [t2["id"] for t2 in tasks[i + 1 :]]
    track = {
        # Unique per track: the pane's ✕ and the band's ✕ dismiss by this id, and the desktop
        # keys row effects on it. A constant here silently dismissed the wrong row.
        "id": track_id or f"track{next(_TRACK_SEQ)}",
        "sessionId": session_id,
        "title": title,
        "caption": "",
        "tasks": tasks,
        "activeTaskId": tasks[0]["id"] if tasks else None,
        "createdAt": now,
        "updatedAt": now,
        # SPEC operational mirrors
        "failedCommand": None,
        "failedReason": None,
        "failedLines": None,
        "failedToolUseId": None,
        "failureToLog": False,
        "isDismissed": False,
        "isWaiting": False,
        "waitingSince": None,
        "waitMs": 0,
        "isExpanded": True,
        "isCounter": False,
        "isAutoSteps": False,
        "isWorkBar": False,
        "isPlanning": False,
        "isFromPlan": False,
        "turnMs": 0,
        "tokens": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "agentRuns": [],
        "statusKey": "running",
        "callsSinceMove": 0,
        "doneAt": None,
    }
    return track


def session_track(doc: Dict[str, Any], session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Newest bar owned by this session. Legacy bars with no owner count for everyone."""
    ts = [t for t in doc.get("tracks", []) if t.get("sessionId") in (None, session_id)]
    return ts[-1] if ts else None


def _replace_session_tracks(doc: Dict[str, Any], session_id: Optional[str],
                            new_track_obj: Dict[str, Any]) -> None:
    """Swap this session's bar only — other sessions keep theirs (a None owner is legacy
    global space, so it is replaced too)."""
    doc["tracks"] = [t for t in doc["tracks"]
                     if t.get("sessionId") not in (None, session_id)] + [new_track_obj]


def find_task_index(track: Dict[str, Any], needle: str) -> int:
    """SPEC 4.2 findTask: exact lowercase trim, then prefix, then substring either direction."""
    needle = needle.strip().lower()
    if not needle:
        return -1
    for i, t in enumerate(track["tasks"]):
        label = (t.get("label") or "").strip().lower()
        if label == needle:
            return i
    for i, t in enumerate(track["tasks"]):
        label = (t.get("label") or "").strip().lower()
        if label.startswith(needle):
            return i
    for i, t in enumerate(track["tasks"]):
        label = (t.get("label") or "").strip().lower()
        if needle in label:
            return i
    return -1


def active_task_index(track: Dict[str, Any]) -> int:
    """Return index of the in_progress task, or -1."""
    aid = track.get("activeTaskId")
    if not aid:
        return -1
    for i, t in enumerate(track["tasks"]):
        if t["id"] == aid:
            return i
    return -1


# ─── Timeline row precedence (SPEC 1.4/1.7) ───

ROW_PRECEDENCE = {
    "blocked": 0,      # check | error
    "permission": 1,
    "question": 2,
    "subagent": 3,
    "skill": 3,
    "lifecycle": 4,
    "tool": 5,
}


def stamp_source(candidates: Dict[str, int]) -> Optional[str]:
    """
    Choose the winning source kind from candidates {kind: timestamp_ms}.
    Precedence per SPEC 1.4/1.7: blocked > permission > question > newest of subagent/skill > lifecycle > tool.
    Returns the winning kind or None if empty.
    """
    if not candidates:
        return None
    # Filter to known kinds
    known = {k: ts for k, ts in candidates.items() if k in ROW_PRECEDENCE}
    if not known:
        return max(candidates, key=candidates.get)  # fallback: newest
    # Group by precedence
    best_kind = None
    best_prec = 99
    best_ts = -1
    for kind, ts in known.items():
        prec = ROW_PRECEDENCE[kind]
        if prec < best_prec or (prec == best_prec and ts > best_ts):
            best_prec = prec
            best_ts = ts
            best_kind = kind
    return best_kind


# ─── Bash check detection (SPEC 2.3/2.4) ───

def is_check_command(command: str) -> bool:
    """Upstream isCheck: any `&&`/`||`/`;`/`|` segment that is a test, build, lint or typecheck.

    The command's head decides, not any word in it — `grep`, `diff` and `test -f` exit 1
    as an answer, not a failure.
    """
    if not command:
        return False
    for part in re.split(r"&&|\|\||;|\|", command):
        words = [w for w in part.strip().split() if w and not w.startswith("-")]
        while words and (re.match(r"^\w+=", words[0]) or words[0] in ("cd", "time")):
            del words[: 2 if words[0] == "cd" else 1]
        if _is_check_words(words):
            return True
    return False


_RUNNERS = re.compile(
    r"^(pytest|py\.test|tox|nox|jest|vitest|mocha|tsc|eslint|ruff|mypy|flake8|pylint|"
    r"black|isort|pre-commit|rspec|phpunit|xcodebuild|gradle|gradlew|mvn|make|bazel|ctest)$"
)


def _is_check_words(words: List[str]) -> bool:
    """Upstream isCheckWords: strip launchers (npx/uv/poetry/pipx, pythonX) and match the head."""
    head = (words[0] if words else "").split("/")[-1]
    a = words[1] if len(words) > 1 else ""
    b = words[2] if len(words) > 2 else ""
    if _RUNNERS.match(head):
        return True
    if re.match(r"^(npx|bunx|pnpx|uv|poetry|pipx)$", head):
        return _is_check_words(words[2:] if a in ("run", "exec") else words[1:])
    if re.match(r"^python[\d.]*$", head):
        return _is_check_words(words[1:])
    if re.match(r"^(npm|pnpm|yarn|bun)$", head):
        return bool(re.match(r"^(test|t|build|lint|typecheck|check)$", a)) or bool(
            a == "run" and re.match(r"^(test|build|lint|typecheck|type-check|check)(:|$)", b)
        )
    if head == "cargo":
        return bool(re.match(r"^(test|build|check|clippy|nextest)$", a))
    if head == "go":
        return bool(re.match(r"^(test|build|vet)$", a))
    if head in ("dotnet", "swift"):
        return bool(re.match(r"^(test|build)$", a))
    if head == "claude":
        return a == "plugin" and bool(re.match(r"^(test|validate)$", b))
    return False


_ERROR_LINE = re.compile(r"\b(error|errors|failed|failure|FAIL|assert\w*)\b|✕", re.IGNORECASE)


def _shorten(text: str, max_len: int) -> str:
    """Upstream shorten: keep the first max-1 chars and mark the cut."""
    return text if len(text) <= max_len else f"{text[: max_len - 1]}…"


def _result_text(result: Any) -> str:
    """Upstream reads result.stdout/stderr; Hermes may hand us a plain string."""
    if isinstance(result, dict):
        return f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    return result if isinstance(result, str) else ""


def error_lines_of(result: Any) -> List[str]:
    """Upstream errorLinesOf: the lines naming a problem, else the last lines; 8 max, 200 chars each."""
    lines = [l.strip() for l in _result_text(result).split("\n") if l.strip()]
    hits = [l for l in lines if _ERROR_LINE.search(l)]
    picked = hits[:8] if hits else lines[-8:]
    return [_shorten(l, 200) for l in picked]


def error_line_of(result: Any) -> str:
    """Upstream errorLineOf: the first output line that names the problem."""
    for line in _result_text(result).split("\n"):
        if _ERROR_LINE.search(line):
            return _shorten(line.strip(), 200)
    return ""



# ─── Command normalization (SPEC 5.4) ───

def normalize_command(command: str) -> str:
    """Normalize a command for display/dedupe (SPEC 5.4 normalizeCommand)."""
    if not command:
        return ""
    return command.strip().splitlines()[0].strip()


# ─── NextTaskIds pruning ───

def _prune_next_ids(track: Dict[str, Any]) -> None:
    """Remove references to non-existent task ids; clear nextTaskIds for non-pending tasks."""
    existing = {t["id"] for t in track["tasks"]}
    for t in track["tasks"]:
        nxt = t.get("nextTaskIds") or []
        t["nextTaskIds"] = [tid for tid in nxt if tid in existing]
        if t.get("state") != "pending":
            t["nextTaskIds"] = []


# ─── Result line rendering ───

def result_line(track: Dict[str, Any]) -> str:
    """SPEC result string: `done/total, state, active "X", next "Y" [, attempt a/max]`."""
    tasks = track["tasks"]
    total = len(tasks)
    done = sum(1 for t in tasks if t["state"] == "completed")
    active_idx = active_task_index(track)
    active_label = ""
    next_label = ""
    attempt_part = ""
    if active_idx >= 0:
        at = tasks[active_idx]
        active_label = f'active "{at["label"]}"'
        if at.get("attempt", 0) > 0 and at.get("maxAttempts", 1) > 1:
            attempt_part = f', attempt {at["attempt"]}/{at["maxAttempts"]}'
        # next pending after active
        for j in range(active_idx + 1, total):
            if tasks[j]["state"] == "pending":
                next_label = f'next "{tasks[j]["label"]}"'
                break
    state = "done" if done == total and total > 0 else ("failed" if track.get("failedCommand") else "running")
    parts = [f"{done}/{total}", state]
    if active_label:
        parts.append(active_label)
    if next_label:
        parts.append(next_label)
    if attempt_part:
        parts.append(attempt_part.lstrip(", "))
    return ", ".join(parts)


# ─── Op application (SPEC 4.4 semantics, port field names) ───

def _subjects(arg: Any) -> List[str]:
    """Accept a task list as strings or as {subject|label|task|title} dicts."""
    if not isinstance(arg, list):
        return []
    out: List[str] = []
    for item in arg:
        if isinstance(item, str):
            if item.strip():
                out.append(item.strip())
        elif isinstance(item, dict):
            label = (item.get("subject") or item.get("label") or item.get("task")
                     or item.get("title") or "").strip()
            if label:
                out.append(label)
    return out


def _is_create_payload(item: Any) -> bool:
    """A dict in a tasks list is a nested create payload, not a bare subject."""
    return isinstance(item, dict) and any(k in item for k in ("op", "title", "tasks"))


def apply_op(doc: Dict[str, Any], op: str, args: Dict[str, Any],
             session_id: Optional[str] = None) -> tuple[str, Dict[str, Any]]:
    """
    Apply a tool op to this session's bar. Returns (result_json, payload_dict).
    Payload includes track snapshot for the tool response.
    """
    # Normalize desktop arg shapes to SPEC names (aliases)
    a = dict(args)
    if op == "active" and "active" not in a and "task" in a:
        a["active"] = a["task"]
    if op == "add" and "add" not in a and "tasks" in a:
        t = a["tasks"]
        if isinstance(t, list) and t and isinstance(t[0], str):
            a["add"] = t
    if op == "found" and "found" not in a and "text" in a:
        a["found"] = a["text"]

    tracks = doc["tracks"]
    track = session_track(doc, session_id) if session_id else (tracks[0] if tracks else None)
    # op=title with tasks (create payload or string list) creates/replaces the bar (SPEC 5.2)
    if op == "title" and not track:
        tasks_arg = a.get("tasks")
        create_payload = None
        subjects = []
        title = (a.get("title") or "").strip()
        if isinstance(tasks_arg, list) and tasks_arg:
            if _is_create_payload(tasks_arg[0]):
                create_payload = tasks_arg[0]
                title = (create_payload.get("title") or title).strip()
                subjects = _subjects(create_payload.get("tasks"))
            else:
                subjects = _subjects(tasks_arg)
        if subjects:
            new_track_obj = new_track(title or "Untitled", subjects, session_id=session_id)
            _replace_session_tracks(doc, session_id, new_track_obj)
            track = new_track_obj
            now = _now_ms()
            return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}
    if not track:
        return json.dumps({"error": "progress_tracker: no bar yet; create one with title and tasks."}), {}
    now = _now_ms()
    tasks = track["tasks"]
    active = active_task_index(track)

    def _save():
        track["updatedAt"] = now
        track["callsSinceMove"] = 0
        _prune_next_ids(track)

    if op == "dismiss":
        # Upstream dismiss(id): hide one track; prune_stale_tracks keeps dismissed ones.
        # No id in args (older callers) -> newest active track, matching the pane's ✕.
        target = next((t for t in tracks if t.get("id") == (args or {}).get("trackId")), None)
        if target is None:
            live = [t for t in tracks if not t.get("isDismissed")]
            target = (live[-1] if live else None) or track
        target["isDismissed"] = True
        target["updatedAt"] = now
        target["callsSinceMove"] = 0
        return json.dumps({"result": f"dismissed {target.get('title') or target.get('label') or target.get('id')}"}), {
            "track": _track_snapshot(target)
        }
    if op == "title":
        # Handle title with tasks create payload (replace existing track)
        create_payload = None
        tasks_arg = a.get("tasks")
        if isinstance(tasks_arg, list) and tasks_arg and _is_create_payload(tasks_arg[0]):
            create_payload = tasks_arg[0]
        if create_payload:
            title = (create_payload.get("title") or "").strip()
            subjects = _subjects(create_payload.get("tasks"))
            if subjects:
                new_track_obj = new_track(title or "Untitled", subjects, session_id=session_id)
                _replace_session_tracks(doc, session_id, new_track_obj)
                track = new_track_obj
                return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}
        new_title = (a.get("title") or "").strip()
        if new_title:
            track["title"] = new_title
            track["updatedAt"] = now
            track["callsSinceMove"] = 0
        subjects = _subjects(tasks_arg)
        if subjects:
            # SPEC 5.2: title + tasks replaces the task list, keeping the track id.
            keep_id = track.get("id")
            new_track_obj = new_track(track["title"] or "", subjects, track_id=keep_id,
                                      session_id=track.get("sessionId"))
            _replace_session_tracks(doc, session_id, new_track_obj)
            track = new_track_obj
        return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}

    if op == "tasks":
        subjects = a.get("tasks") or []
        if isinstance(subjects, str):
            subjects = [s.strip() for s in subjects.splitlines() if s.strip()]
        subjects = _subjects(subjects)
        if subjects:
            new_track_obj = new_track(track["title"] or "", subjects,
                                      session_id=track.get("sessionId"))
            _replace_session_tracks(doc, session_id, new_track_obj)
            track = new_track_obj
        return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}

    if op == "next":
        if active < 0:
            # SPEC: upTo = first non-completed task index
            up_to = -1
            for i, t in enumerate(tasks):
                if t["state"] != "completed":
                    up_to = i
                    break
            if up_to < 0:
                # all completed
                return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}
            at = tasks[up_to]
            at["state"] = "completed"
            at["closedAt"] = now
            at["attempt"] = 0
            at["blockReason"] = None
            at["ticket"] = None
            # advance to next pending
            for j in range(up_to + 1, len(tasks)):
                if tasks[j]["state"] == "pending":
                    tasks[j]["state"] = "in_progress"
                    tasks[j]["openedAt"] = now
                    track["activeTaskId"] = tasks[j]["id"]
                    break
            else:
                track["activeTaskId"] = None
                track["statusKey"] = "completed"
                track["doneAt"] = now
            track["failedCommand"] = None
            track["failedReason"] = None
            track["failureToLog"] = False
            _save()
            return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}

        at = tasks[active]
        at["state"] = "completed"
        at["closedAt"] = now
        at["attempt"] = 0
        at["blockReason"] = None
        at["ticket"] = None
        # advance to next pending
        for j in range(active + 1, len(tasks)):
            if tasks[j]["state"] == "pending":
                tasks[j]["state"] = "in_progress"
                tasks[j]["openedAt"] = now
                track["activeTaskId"] = tasks[j]["id"]
                break
        else:
            track["activeTaskId"] = None
            track["statusKey"] = "completed"
            track["doneAt"] = now
        track["failedCommand"] = None
        track["failedReason"] = None
        track["failureToLog"] = False
        _save()
        return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}

    if op == "active":
        needle = (a.get("active") or "").strip()
        idx = find_task_index(track, needle)
        if idx < 0:
            names = [f'"{t["label"]}"' for t in tasks]
            return json.dumps({"error": f'progress_tracker: no task named "{needle}"; tasks are {", ".join(names)}.'}), {}
        # SPEC: earlier -> completed, selected -> in_progress, other in_progress -> pending
        for i, t in enumerate(tasks):
            if i < idx:
                t["state"] = "completed"
                t["closedAt"] = t.get("closedAt") or now
            elif i == idx:
                if t["state"] != "in_progress":
                    t["state"] = "in_progress"
                    t["openedAt"] = now
                    t["attempt"] = 0
                    t["blockReason"] = None
                    t["ticket"] = None
            elif t["state"] == "in_progress":
                t["state"] = "pending"
        track["activeTaskId"] = tasks[idx]["id"]
        track["statusKey"] = "running"
        _save()
        return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}

    if op == "add":
        subjects = a.get("add") or []
        if isinstance(subjects, str):
            subjects = [s.strip() for s in subjects.splitlines() if s.strip()]
        subjects = _subjects(subjects)
        base = len(tasks)
        for i, subj in enumerate(subjects):
            tid = f"add{base + i + 1}"
            new_task = {
                "id": tid,
                "label": subj,
                "state": "pending",
                "note": None,
                "path": None,
                "attempt": 0,
                "maxAttempts": 2,
                "failures": 0,
                "blockReason": None,
                "openedAt": None,
                "closedAt": None,
                "ticket": None,
                "nextTaskIds": [],
                "skipped": False,
            }
            # update previous tail's nextTaskIds
            if tasks:
                tasks[-1]["nextTaskIds"].append(tid)
            tasks.append(new_task)
        # SPEC: if no task in progress, first pending becomes in_progress
        if not any(t["state"] == "in_progress" for t in tasks):
            for t in tasks:
                if t["state"] == "pending":
                    t["state"] = "in_progress"
                    t["openedAt"] = now
                    track["activeTaskId"] = t["id"]
                    break
        _save()
        return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}

    if op == "skip":
        if active < 0:
            up_to = -1
            for i, t in enumerate(tasks):
                if t["state"] != "completed":
                    up_to = i
                    break
            if up_to < 0:
                return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}
        else:
            up_to = active
        at = tasks[up_to]
        if at["state"] != "completed":
            at["state"] = "completed"
            at["closedAt"] = now
            at["skipped"] = True
            at["attempt"] = 0
            at["blockReason"] = None
            at["ticket"] = None
            # advance
            for j in range(up_to + 1, len(tasks)):
                if tasks[j]["state"] == "pending":
                    tasks[j]["state"] = "in_progress"
                    tasks[j]["openedAt"] = now
                    track["activeTaskId"] = tasks[j]["id"]
                    break
            else:
                track["activeTaskId"] = None
                track["statusKey"] = "completed"
                track["doneAt"] = now
        track["failedCommand"] = None
        track["failedReason"] = None
        track["failureToLog"] = False
        _save()
        return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}

    if op == "failed":
        if active < 0:
            return json.dumps({"error": "progress_tracker: no active task to mark failed"}), {}
        at = tasks[active]
        at["attempt"] = at.get("attempt", 0) + 1
        at["failures"] = at.get("failures", 0) + 1
        reason = args.get("reason") or "failed"
        at["blockReason"] = reason
        at["ticket"] = args.get("ticket")
        same = track.get("failedCommand") == reason
        track["failedCommand"] = reason
        track["failedRuns"] = track.get("failedRuns", 0) + 1
        # SPEC: repeating the same failure keeps reason/lines; a new failure clears them
        if not same:
            track["failedReason"] = None
            track["failedLines"] = None
        track["failedLines"] = args.get("lines") or track.get("failedLines")
        track["failedToolUseId"] = at["ticket"]
        track["failureToLog"] = True
        _save()
        return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}

    if op == "fixed":
        if active < 0:
            return json.dumps({"error": "progress_tracker: no active task to mark fixed"}), {}
        at = tasks[active]
        at["blockReason"] = None
        at["ticket"] = None
        track["failedCommand"] = None
        track["failedReason"] = None
        track["failureToLog"] = False
        _save()
        return json.dumps({"result": result_line(track)}), {"track": _track_snapshot(track)}

    if op == "found":
        finding = (a.get("found") or "").strip()
        if not finding:
            return json.dumps({"result": "noted"}), {}
        kind = (a.get("kind") or "found").strip()
        enabled = _config.get("enabled_findings", DEFAULTS["enabled_findings"])
        if enabled and kind not in enabled:
            return json.dumps({"result": "noted"}), {}
        # dedupe: identical finding in last N findings
        findings = doc["log"]["findings"]
        for f in reversed(findings[-50:]):
            if f.get("kind") == kind and f.get("text") == finding:
                return json.dumps({"result": "noted"}), {}
        findings.append({
            "id": f"finding{len(findings)+1}",
            "kind": kind,
            "text": finding,
            "at": now,
            "sessionId": a.get("session_id") or a.get("session"),
        })
        _cap_findings(doc)
        return json.dumps({"result": "noted"}), {"track": _track_snapshot(track)}

    valid_ops = "title, tasks, next, active, add, skip, failed, fixed, found, dismiss"
    return json.dumps({"error": f'progress_tracker: unknown op "{op}" (valid ops: {valid_ops})'}), {}


def _track_snapshot(track: Dict[str, Any], blocks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Minimal serializable snapshot for tool responses.

    Emits both the port field names and the desktop field names so
    desktop/plugin.js reads never see undefined. Mapping:
      label->subject, state->status, skipped->isSkipped,
      attempt->attempts, openedAt->startedAt, closedAt->doneAt, caption->description.
    """
    timing = track_timing(track, blocks)
    tokens = track.get("tokens") or {}
    return {
        "id": track["id"],
        "sessionId": track.get("sessionId"),
        "title": track["title"],
        "label": track["title"],
        "caption": track.get("caption", ""),
        "description": track.get("caption", ""),
        "activeTaskId": track.get("activeTaskId"),
        "startedAt": track.get("createdAt"),
        # Pane tiles (upstream headerOf): the measured numbers, not a second clock.
        "tokens": {
            "input": tokens.get("input", 0),
            "output": tokens.get("output", 0),
            "cached": tokens.get("cacheRead", 0),
            "calls": tokens.get("calls", 0),
        },
        "elapsedMs": timing["elapsedMs"],
        "workMs": timing["workMs"],
        "waitMs": timing["waitMs"],
        "taskMs": timing["taskMs"],
        "tasks": [
            {
                "id": t["id"],
                "label": t["label"],
                "subject": t["label"],
                "state": t["state"],
                "status": t["state"],
                "attempt": t.get("attempt", 0),
                "maxAttempts": t.get("maxAttempts", 2),
                "attempts": t.get("attempt", 0),
                "failures": t.get("failures", 0),
                "blockReason": t.get("blockReason"),
                "skipped": t.get("skipped", False),
                "isSkipped": t.get("skipped", False),
                "note": t.get("note"),
                "ticket": t.get("ticket"),
                "startedAt": t.get("openedAt"),
                "doneAt": t.get("closedAt"),
                "agentRuns": t.get("agentRuns", []),
            }
            for t in track["tasks"]
        ],
        "result": result_line(track),
        "statusKey": track.get("statusKey", "running"),
        # The band's pill state and the chip colour read these; `statusKey` alone left a
        # failed run looking green.
        "status": "failed" if track.get("failedCommand") else track.get("statusKey", "running"),
        "failedCommand": track.get("failedCommand"),
        "failedReason": track.get("failedReason"),
        "failedLines": track.get("failedLines") or [],
        "failedToolUseId": track.get("failedToolUseId"),
        "isDismissed": track.get("isDismissed", False),
    }


def _cap_findings(doc: Dict[str, Any]) -> None:
    """SPEC LOG_CAP: keep blocks+findings within max_findings, evicting oldest-first across both lists."""
    max_f = _config.get("max_findings", DEFAULTS["max_findings"])
    log = doc["log"]
    total = len(log["blocks"]) + len(log["findings"])
    excess = total - max_f
    while excess > 0:
        b = log["blocks"][0] if log["blocks"] else None
        f = log["findings"][0] if log["findings"] else None
        b_at = b.get("at") if b else None
        f_at = f.get("at") if f else None
        if b is not None and (f is None or b_at <= f_at):
            log["blocks"].pop(0)
        elif f is not None:
            log["findings"].pop(0)
        else:
            break
        excess -= 1


# ─── Turn lifecycle (beginTurn / endTurn) ───

def begin_turn(doc: Dict[str, Any], session_id: str, turn_id: Optional[str] = None) -> None:
    """Start a turn for a session."""
    now = _now_ms()
    sid = session_id or "default"
    sess = doc["sessions"].setdefault(sid, {"open": None, "tab": "blocked", "activeTaskId": None, "turnId": None})
    sess["turnId"] = turn_id or f"turn{len(doc['turns'])+1}"
    sess["turnStartedAt"] = now
    doc["turns"].append({
        "id": sess["turnId"],
        "sessionId": sid,
        "startedAt": now,
        "endedAt": None,
        "turnMs": 0,
        "tokens": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    })
    _enforce_turn_cap(doc)


def end_turn(doc: Dict[str, Any], session_id: str, tokens: Optional[Dict[str, int]] = None) -> None:
    """End the current turn for a session."""
    now = _now_ms()
    sid = session_id or "default"
    sess = doc["sessions"].get(sid)
    if not sess or not sess.get("turnId"):
        return
    for turn in reversed(doc["turns"]):
        if turn["sessionId"] == sid and turn["endedAt"] is None:
            turn["endedAt"] = now
            turn["turnMs"] = now - turn["startedAt"]
            if tokens:
                turn["tokens"] = {
                    "input": tokens.get("input", 0),
                    "output": tokens.get("output", 0),
                    "cacheRead": tokens.get("cacheRead", 0),
                    "cacheWrite": tokens.get("cacheWrite", 0),
                }
            break
    sess["turnId"] = None
    sess["turnStartedAt"] = None


def record_api_call(
    doc: Dict[str, Any],
    session_id: str,
    usage: Optional[Dict[str, Any]] = None,
    context_length: Optional[int] = None,
) -> None:
    """Fold one model call's tokens into the open turn and every running track.

    Upstream reads these from its engine (``track.tokens`` / ``ctx.contextPercent``). Hermes
    hands them to ``post_api_request``, so this is where they land: tokens in/out/cached and
    the call count on the turn and on each track that is still running, plus the session's
    context fill for the next snapshot.
    """
    sid = session_id or "default"
    u = usage or {}

    def num(key: str) -> int:
        try:
            return int(u.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    # Anthropic-style buckets: input_tokens excludes the cached prompt; the prompt is all three.
    prompt = num("input_tokens") + num("cache_read_tokens") + num("cache_write_tokens")
    out = num("output_tokens")
    cached = num("cache_read_tokens")

    for turn in reversed(doc.get("turns", [])):
        if turn.get("sessionId") == sid and turn.get("endedAt") is None:
            t = turn.setdefault("tokens", {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0})
            t["input"] += prompt
            t["output"] += out
            t["cacheRead"] += cached
            t["cacheWrite"] += num("cache_write_tokens")
            turn["modelCalls"] = turn.get("modelCalls", 0) + 1
            break

    # One list owns the call's tokens — upstream accrues to the running list, not every
    # open row. Fall back to the newest open list when the session has no active task.
    active_id = doc.get("sessions", {}).get(sid, {}).get("activeTaskId")
    open_tracks = [t for t in doc.get("tracks", [])
                   if not t.get("isDismissed") and not _is_complete(t)]
    if active_id:
        open_tracks = [t for t in open_tracks if t.get("id") == active_id]
    elif open_tracks:
        open_tracks = open_tracks[-1:]
    for track in open_tracks:
        tk = track.setdefault("tokens", {})
        tk["input"] = tk.get("input", 0) + prompt
        tk["output"] = tk.get("output", 0) + out
        tk["cacheRead"] = tk.get("cacheRead", 0) + cached
        tk["cacheWrite"] = tk.get("cacheWrite", 0) + num("cache_write_tokens")
        tk["calls"] = tk.get("calls", 0) + 1

    if context_length and context_length > 0 and prompt:
        doc["sessions"].setdefault(sid, {})["context"] = {
            # Math.round, not Python round: 10.5% must come out 11%, as upstream's does.
            "percent": min(100, int(prompt * 100 / context_length + 0.5)),
            "promptTokens": prompt,
            "window": context_length,
        }


def _block_wait_ms(block: Dict[str, Any], now: int) -> int:
    """How long one log block held the agent waiting on a human."""
    end = block.get("answeredAt") or block.get("resolvedAt") or now
    start = block.get("at") or end
    return max(0, end - start)


def track_timing(track: Dict[str, Any], blocks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:
    """Elapsed / worked / waited for one track, measured from the session's log blocks.

    A track is "waiting on you" for exactly the span of a permission, question or plan block,
    so the sums are derived from those blocks (an unresolved one keeps counting) rather than
    kept as a second clock that can drift.
    """
    now = _now_ms()
    created = track.get("createdAt") or now
    elapsed = max(0, now - created)
    wait = 0
    for b in blocks or []:
        start = b.get("at") or 0
        if start < created:
            continue  # opened before this track existed
        # Only permission/question blocks are human-wait; TOOL rows are work, not wait.
        if b.get("kind") not in ("permission", "question"):
            continue
        end = b.get("answeredAt") or b.get("resolvedAt") or now
        wait += max(0, end - start)
    active = track.get("activeTaskId")
    task_ms = 0
    for t in track.get("tasks", []):
        if t.get("id") == active and t.get("state") == "in_progress" and t.get("openedAt"):
            task_ms = max(0, now - t["openedAt"])
    return {"elapsedMs": elapsed, "workMs": max(0, elapsed - wait), "waitMs": wait, "taskMs": task_ms}


def _enforce_turn_cap(doc: Dict[str, Any]) -> None:
    max_t = _config.get("max_turns_retained", DEFAULTS["max_turns_retained"])
    turns = doc["turns"]
    if len(turns) > max_t:
        # endBlock: close any unanswered blocks
        end_block(doc)
        # trimTimeline: trim blocks+findings to max_findings
        _cap_findings(doc)
        # evict oldest turns
        excess = len(turns) - max_t
        del turns[:excess]


def end_block(doc: Dict[str, Any]) -> None:
    """Close all unanswered blocks (answeredAt = now)."""
    now = _now_ms()
    for b in doc["log"]["blocks"]:
        if b.get("answeredAt") is None:
            b["answeredAt"] = now
            b["outcome"] = b.get("outcome") or "Not answered"


# ─── Session helpers ───

def ensure_session(doc: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """Get or create session entry."""
    return doc["sessions"].setdefault(session_id, {"open": None, "tab": "blocked", "activeTaskId": None, "turnId": None})


def close_session_tracks(doc: Dict[str, Any], session_id: Optional[str]) -> int:
    """Session ended: dismiss its unfinished bars; keep completed ones as history.

    A finished session stops reading as "running", but a completed bar stays visible
    until the user starts another task (a new title/tasks payload replaces it).
    """
    now = _now_ms()
    closed = 0
    for t in doc["tracks"]:
        if t.get("sessionId") == session_id and not t.get("isDismissed") and not _is_complete(t):
            t["isDismissed"] = True
            t["updatedAt"] = now
            closed += 1
    return closed


# ─── Permission block helpers (SPEC 5.4) ───

def open_permission_block(doc: Dict[str, Any], command: str, session_key: str, pattern_keys: Optional[List[str]] = None) -> None:
    """Open a permission block for a tool approval request."""
    now = _now_ms()
    norm = normalize_command(command)
    patterns = pattern_keys or []
    doc["log"]["blocks"].append({
        "id": f"block{len(doc['log']['blocks'])+1}",
        "kind": "permission",
        "text": f"Allow {norm}",
        "details": {
            "command": norm,
            "patterns": len(patterns),
            "sessionKey": session_key,
            "sessionId": session_key,
        },
        "at": now,
        "answeredAt": None,
        "outcome": None,
    })
    _cap_findings(doc)


def resolve_permission_block(doc: Dict[str, Any], choice: str) -> None:
    """Resolve the newest unanswered permission block. Terminal rule: first answer wins."""
    now = _now_ms()
    for b in reversed(doc["log"]["blocks"]):
        if b["kind"] == "permission" and b.get("answeredAt") is None:
            # choice mapping: approve/once/yes -> Approved; deny/no/reject -> Not approved
            c = (choice or "").lower()
            if c in ("approve", "once", "yes", "allow"):
                b["outcome"] = "Approved"
            elif c in ("deny", "no", "reject", "refuse"):
                b["outcome"] = "Not approved"
            else:
                b["outcome"] = "Dismissed"
            b["answeredAt"] = now
            break


# ─── Question block helpers (SPEC 5.5) ───

def open_question_block(doc: Dict[str, Any], kind: str, session_id: Optional[str] = None) -> None:
    """Open a question block for human input (never captures prompt text)."""
    now = _now_ms()
    doc["log"]["blocks"].append({
        "id": f"block{len(doc['log']['blocks'])+1}",
        "kind": "question",
        "text": kind,  # never the prompt
        "details": {"kind": kind, "sessionId": session_id},
        "at": now,
        "answeredAt": None,
        "outcome": None,
    })
    _cap_findings(doc)


def resolve_question_block(doc: Dict[str, Any], outcome: str) -> None:
    """Resolve the newest unanswered question block. Outcome normalized to answered|dismissed."""
    now = _now_ms()
    for b in reversed(doc["log"]["blocks"]):
        if b["kind"] == "question" and b.get("answeredAt") is None:
            normalized = "answered" if outcome.lower() in ("answered", "answer", "responded", "submitted", "ok", "yes") else "dismissed"
            b["outcome"] = normalized
            b["answeredAt"] = now
            b["details"]["outcome"] = normalized
            break


# ─── Subagent run tracking (SPEC 4) ───

def start_agent_run(doc: Dict[str, Any], run_id: str, alt_id: str = "", task_id: Optional[str] = None) -> None:
    """Record subagent start under current active task."""
    tracks = doc["tracks"]
    if not tracks:
        return
    track = tracks[0]
    active = active_task_index(track)
    if active < 0:
        return
    at = track["tasks"][active]
    at.setdefault("agentRuns", []).append({
        "runId": run_id,
        "altId": alt_id or None,
        "startedAt": _now_ms(),
        "endedAt": None,
    })


def finish_agent_run(doc: Dict[str, Any], run_id: str) -> None:
    """Record subagent finish."""
    now = _now_ms()
    tracks = doc["tracks"]
    if not tracks:
        return
    for track in tracks:
        for at in track["tasks"]:
            for run in at.get("agentRuns", []):
                if (run.get("runId") == run_id or run.get("altId") == run_id) and run.get("endedAt") is None:
                    run["endedAt"] = now
                    break


# ─── Check clearing (SPEC 2.4) ───

def clear_check(doc: Dict[str, Any], command: str,
                track: Optional[Dict[str, Any]] = None) -> None:
    """Clear a failed check when the same check passes (latestFirst / clearOpen / findFirstOpen / checkCompleted)."""
    norm = normalize_command(command)
    tracks = doc["tracks"]
    if not tracks:
        return
    # With per-session bars, tracks[0] is another session's bar — clear the caller's.
    track = track or tracks[0]
    if track.get("failedCommand") and normalize_command(track["failedCommand"]) == norm:
        track["failedCommand"] = None
        track["failedReason"] = None
        track["failureToLog"] = False
        active = active_task_index(track)
        if active >= 0:
            at = track["tasks"][active]
            at["blockReason"] = None
            at["ticket"] = None
            at["attempt"] = 0


# ─── Tool timeline row helpers ───

def add_tool_row(doc: Dict[str, Any], tool_name: str, session_id: str) -> None:
    """Add a TOOL timeline row (pre_tool_call). Ignore negative names; dedupe consecutive."""
    if not tool_name or tool_name.startswith("-"):
        return
    enabled = _config.get("enabled_tools", DEFAULTS["enabled_tools"])
    if enabled and tool_name not in enabled:
        return
    now = _now_ms()
    blocks = doc["log"]["blocks"]
    if blocks and blocks[-1].get("kind") == "tool" and blocks[-1].get("tool") == tool_name and blocks[-1].get("answeredAt") is None:
        return
    blocks.append({
        "id": f"block{len(blocks)+1}",
        "kind": "tool",
        "tool": tool_name,
        "text": tool_name,
        "details": {"sessionId": session_id},
        "at": now,
        "answeredAt": None,
        "outcome": None,
    })
    _cap_findings(doc)


def close_tool_row(doc: Dict[str, Any], tool_name: str, status: str) -> None:
    """Close the latest open TOOL row for this tool (post_tool_call)."""
    if status == "blocked":
        # also handled by permission block resolution
        pass
    now = _now_ms()
    for b in reversed(doc["log"]["blocks"]):
        if b.get("kind") == "tool" and b.get("tool") == tool_name and b.get("answeredAt") is None:
            b["answeredAt"] = now
            b["outcome"] = "Completed" if status == "ok" else "Failed"
            b["details"]["status"] = status
            break


# ─── Demo script (SPEC 6.6) ───

DEMO_TASKS = [
    "Set up project scaffold",
    "Write failing tests",
    "Implement core logic",
    "Add edge-case handling",
    "Document public API",
    "Run full test suite",
]

# Each step: {delayMs, op, args} — matches upstream DEMO_STEPS sequence:
# 1. next (Plan -> Read config.ts)
# 2-4. next x3 (parallel reads: config.ts, api.ts, db.ts -> Build)
# 5. found (sub-agent "Map the routes" starts)
# 6. next (Build -> Test)
# 7. failed ("2 tests failing")
# 8. failed ("1 test failing") + found (retry loop finding)
# 9. fixed
# 10. found (waiting: "Allow npm publish")
# 11. found (waiting resolved: "It ran")
# 12. next (Test -> Ship)
# 13. next (Ship -> done)
DEMO_STEPS = [
    {"delayMs": 1400, "op": "next", "args": {}},
    {"delayMs": 400, "op": "next", "args": {}},
    {"delayMs": 400, "op": "next", "args": {}},
    {"delayMs": 800, "op": "next", "args": {}},
    {"delayMs": 700, "op": "found", "args": {"text": "Map the routes", "kind": "found"}},
    {"delayMs": 1600, "op": "next", "args": {}},
    {"delayMs": 1200, "op": "failed", "args": {"reason": "2 tests failing"}},
    {"delayMs": 1300, "op": "failed", "args": {"reason": "1 test failing"}},
    {"delayMs": 0, "op": "found", "args": {"text": "Retry loop in api.ts drops the auth header", "kind": "found"}},
    {"delayMs": 1600, "op": "fixed", "args": {}},
    {"delayMs": 500, "op": "found", "args": {"text": "Allow npm publish", "kind": "found"}},
    {"delayMs": 1800, "op": "found", "args": {"text": "It ran", "kind": "found"}},
    {"delayMs": 900, "op": "next", "args": {}},
    {"delayMs": 1400, "op": "next", "args": {}},
]

DEMO_LINGER_MS = 12000

# ─── Stale track pruning (SPEC 1.6) ───

STALE_MS = 12 * 60 * 60 * 1000  # 12 hours

# ─── Auto-bar constants (upstream lines 63, 993-1022) ───

AUTO_BAR_CALLS = 3
NUDGE_CALLS = 12
# Upstream progress-tracker.mjs:990 (no list at all) and :913 (list exists but stalled).
NUDGE_NO_LIST = ("This work has no task list yet, so the progress bar can only count steps. "
                 "If more steps are coming, call progress_tracker now with "
                 "{title, tasks:[every step you plan]} so the bar shows a real total. "
                 "Don't mention this note.")
NUDGE_STALLED = ('The progress bar still shows "{subject}" in progress ({done}/{total}). '
                 'If that task is done, update it now (progress_tracker next/active, or TaskUpdate). '
                 "Don't mention this note.")


def nudge_note(doc: Dict[str, Any], session_id: str) -> str:
    """Upstream :913/:990 note text, delivered as turn context once the bar stalls."""
    tracks = [t for t in doc.get("tracks", []) if not t.get("isDismissed")]
    track = tracks[-1] if tracks else None
    if track is None:
        sess = doc.get("sessions", {}).get(session_id, {})
        return NUDGE_NO_LIST if sess.get("callsSinceMove", 0) >= NUDGE_CALLS else ""
    if track.get("callsSinceMove", 0) < NUDGE_CALLS:
        return ""
    if not track.get("tasks"):
        return NUDGE_NO_LIST
    tasks = track["tasks"]
    idx = active_task_index(track)
    current = tasks[idx] if 0 <= idx < len(tasks) else None
    # Internal task fields are `label`/`state` (the snapshot exposes them as subject/status).
    done = sum(1 for t in tasks if t.get("state") == "completed" and not t.get("skipped"))
    return NUDGE_STALLED.format(subject=(current or {}).get("label") or "nothing",
                                done=done, total=len(tasks))


def stepNameOf(tool_name: str, args: Dict[str, Any]) -> str:
    """Derive a short subject from tool name + target (upstream stepNameOf, lines 1011-1022)."""
    if tool_name == "Read":
        path = args.get("path") or args.get("file_path") or ""
        return f"Read {path.split('/')[-1] if path else 'file'}"
    if tool_name == "Write":
        path = args.get("path") or args.get("file_path") or ""
        return f"Write {path.split('/')[-1] if path else 'file'}"
    if tool_name == "Edit":
        path = args.get("path") or args.get("file_path") or ""
        return f"Edit {path.split('/')[-1] if path else 'file'}"
    if tool_name == "Bash":
        cmd = args.get("command") or ""
        parts = cmd.strip().split()
        return f"Run {parts[0]}" if parts else "Run command"
    if tool_name == "Glob":
        pattern = args.get("pattern") or ""
        return f"Glob {pattern.split('/')[-1] if pattern else 'pattern'}"
    if tool_name == "Grep":
        pattern = args.get("pattern") or ""
        return f"Grep {pattern[:30]}"
    if tool_name == "Task":
        desc = args.get("description") or ""
        return f"Task: {desc[:40]}" if desc else "Task"
    if tool_name in ("WebFetch", "WebSearch"):
        return tool_name
    return tool_name


def prune_stale_tracks(doc: Dict[str, Any]) -> None:
    """Drop stale tracks, but keep completed ones so the last result stays on screen.

    Completed bars are the user's "done" state and remain until a new task replaces
    them; only stale unfinished/dismissed bars are pruned.
    """
    cutoff = _now_ms() - STALE_MS
    doc["tracks"] = [
        t for t in doc["tracks"]
        if _is_complete(t) or (t.get("updatedAt") or t.get("createdAt") or 0) > cutoff
    ]


def _is_complete(track: Dict[str, Any]) -> bool:
    tasks = track.get("tasks", [])
    return len(tasks) > 0 and all(t.get("state") == "completed" for t in tasks)


# ─── Row ranking (for dashboard rowOf) ───

def row_rank(kind: str) -> int:
    """Return precedence rank for a timeline row kind (lower = higher priority)."""
    return ROW_PRECEDENCE.get(kind, 99)


def demo_script() -> Dict[str, Any]:
    """Return the SPEC 6.6 demo script as a structured dict."""
    return {
        "title": "Feature: Progress Tracker",
        "tasks": DEMO_TASKS,
        "steps": DEMO_STEPS,
        "lingerMs": DEMO_LINGER_MS,
    }


# ─── Snapshot for dashboard ───

def _session_blocks(track: Dict[str, Any], blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Restrict a track's timing blocks to its own session.

    Unscoped, every bar counted every other session's open blocks, so "waited on you"
    absorbed the whole elapsed span and "worked" collapsed to zero.
    """
    sid = track.get("sessionId")
    if sid is None:
        return blocks
    out = []
    for b in blocks:
        bsid = (b.get("details") or {}).get("sessionId")
        if bsid is None or bsid == sid:
            out.append(b)
    return out


def snapshot(doc: Dict[str, Any], session_id: str, include_all: bool = False) -> Dict[str, Any]:
    """Compact snapshot for /snapshot endpoint. Bars are scoped to the requesting session;
    legacy bars with no owner are visible to all. `include_all` (sidebar pane) lists every
    session's bars so several chats' trackers can be followed side by side."""
    if include_all:
        tracks = list(doc.get("tracks", []))
    else:
        tracks = [t for t in doc.get("tracks", []) if t.get("sessionId") in (None, session_id)]
    sess = doc["sessions"].get(session_id, {})
    blocks = doc["log"]["blocks"]
    tracks = [_track_snapshot(t, _session_blocks(t, blocks)) for t in tracks]
    if include_all:
        for s in tracks:
            if s.get("sessionId") not in (None, session_id):
                for k in ("failedCommand", "failedReason", "failedLines", "failedToolUseId"):
                    s.pop(k, None)
    return {
        "tracks": tracks,
        "open": doc.get("open"),
        "turn": doc.get("turn"),
        "session": {
            "open": sess.get("open"),
            "tab": sess.get("tab", "blocked"),
            "activeTaskId": sess.get("activeTaskId"),
            "turnId": sess.get("turnId"),
        },
        # Context fill the engine last reported for this session (post_api_request); absent
        # until a call lands, which is why the CONTEXT tile is conditional upstream.
        "context": sess.get("context"),
        "log": {
            "blocks": doc["log"]["blocks"][-50:],
            "findings": doc["log"]["findings"][-50:],
        },
        "turns": doc["turns"][-10:],
        "config": {
            "maxFindings": _config.get("max_findings"),
            "maxTurnsRetained": _config.get("max_turns_retained"),
            "enabledTools": _config.get("enabled_tools"),
            "enabledFindings": _config.get("enabled_findings"),
        },
    }


def timeline(doc: Dict[str, Any], session_id: str) -> List[Dict[str, Any]]:
    """Timeline entries for /timeline endpoint (merged blocks+findings+turns)."""
    items = []
    for b in doc["log"]["blocks"]:
        items.append({"type": "block", **b, "sessionId": b.get("details", {}).get("sessionId")})
    for f in doc["log"]["findings"]:
        items.append({"type": "finding", **f, "sessionId": f.get("sessionId")})
    for t in doc["turns"]:
        items.append({"type": "turn", **t, "sessionId": t.get("sessionId")})
    items.sort(key=lambda x: x.get("at") or x.get("startedAt") or 0)
    return items