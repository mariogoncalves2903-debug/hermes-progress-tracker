"""Progress Tracker — port of SPEC.md to Hermes plugin API."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_DIR = str(Path(__file__).resolve().parent)
if _DIR in sys.path:
    sys.path.remove(_DIR)
sys.path.insert(0, _DIR)

import progress_store as store

_log = logging.getLogger("progress-tracker")

PLUGIN_ID = "progress-tracker"
TOOL_NAME = "progress_tracker"
TOOLSET = "progress"

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["title", "tasks", "next", "active", "add", "skip", "failed", "fixed", "found", "dismiss"],
            "description": "Operation: title, tasks, next, active, add, skip, failed, fixed, found, or dismiss"
        },
        "title": {"type": "string", "description": "New title for the bar (op=title)"},
        "tasks": {
            "type": "array",
            "items": {"anyOf": [{"type": "string"}, {"type": "object"}]},
            "description": "Plan steps for a new bar (op=tasks) or create payload [{op:'create', title, tasks}] (op=title). Use goal-level steps from the user's request, never tool names."
        },
        "active": {"type": "string", "description": "Task name to activate (op=active)"},
        "task": {"type": "string", "description": "Alias for active (desktop op=active arg shape)"},
        "add": {"type": "array", "items": {"type": "string"}, "description": "Subjects to append (op=add)"},
        "reason": {"type": "string", "description": "Failure reason (op=failed)"},
        "ticket": {"type": "string", "description": "Tool call id for the failure (op=failed)"},
        "lines": {"type": "array", "items": {"type": "string"}, "description": "Output lines for context (op=failed)"},
        "kind": {"type": "string", "description": "Finding kind (op=found)"},
        "found": {"type": "string", "description": "Finding text (op=found)"},
        "text": {"type": "string", "description": "Alias for found (desktop op=found arg shape)"},
    },
    "required": ["op"],
    "additionalProperties": False,
}


def _sid(kwargs: Dict[str, Any]) -> str:
    """Extract session id from hook kwargs."""
    return (
        kwargs.get("session_id")
        or kwargs.get("session_key")
        or kwargs.get("sid")
        or "default"
    )


# ─── Multi-step enforcement ───
# A prompt section alone does not guarantee the model calls the tool. These hooks close the
# gap: once a session shows project-shaped user intent, heavy execution tools are blocked
# until a real bar exists. Narrow on purpose — trivial one-shot actions stay untracked.

ENFORCED_TOOLS = frozenset({
    "terminal", "execute_code", "write_file", "patch", "browser_exec",
    "computer_use", "delegate_task", "tool_call",
})

_PROJECT_KEYWORDS = (
    "build", "create", "make", "implement", "harness", "integrate", "port",
    "refactor", "automate", "develop", "scaffold", "migrate", "plugin",
    "desktop", "backend", "script", "project", "app", "api", "install",
    "fix", "debug", "resolve", "deploy", "test", "configure", "setup",
)

_PROJECT_PHRASES = (
    "can you", "could you", "i need", "i want", "let's", "lets", "so it",
    "so that", "and then", "in order to", "step 1", "first,", "then,",
    "finally,", "background", "deliver", "run in the background",
)

_EXPLAIN_PREFIXES = (
    "explain", "what is", "what are", "why does", "why do", "how does",
    "how to", "teach me", "describe", "tutorial", "guide me", "learn about",
)

_MIN_PROJECT_CHARS = 100

_BLOCK_MESSAGE = (
    "progress-tracker: this looks like multi-step work and no active progress bar exists. "
    "Call progress_tracker now with {\"op\": \"title\", \"title\": <goal>, \"tasks\": [...]} "
    "before running more tools."
)


def _looks_like_multistep(text: Any) -> bool:
    """Conservative project detector: request signals plus execution shape.

    Two keyword hits lower the length bar; explanatory openers ("explain how to...")
    are questions, not work orders, unless the message also starts with a build verb.
    """
    if not isinstance(text, str):
        return False
    t = text.lower().strip()
    if len(t) < 50:
        return False
    keywords = sum(1 for k in _PROJECT_KEYWORDS if k in t)
    phrases = sum(1 for p in _PROJECT_PHRASES if p in t)
    if keywords < 1:
        return False
    head = t[:60]
    if any(p in head for p in _EXPLAIN_PREFIXES):
        return False
    min_len = 50 if keywords >= 2 else _MIN_PROJECT_CHARS
    if len(t) < min_len:
        return False
    return phrases >= 1 or keywords >= 2 or t.count("?") >= 2


def _has_active_track(doc: Dict[str, Any], session_id: str) -> bool:
    """Any exact-session, non-dismissed, non-completed bar. Legacy None-owner bars do not count."""
    return any(
        t.get("sessionId") == session_id
        and not t.get("isDismissed")
        and t.get("statusKey") != "completed"
        for t in doc.get("tracks", [])
    )


def _enforcement_directive(tool_name: str, session_id: str) -> Optional[Dict[str, str]]:
    """Return a pre_tool_call block directive when project intent is untracked."""
    if tool_name not in ENFORCED_TOOLS:
        return None
    doc = store.get_document()
    if _has_active_track(doc, session_id):
        return None
    session = doc.get("sessions", {}).get(session_id) or {}
    # Intent flag is authoritative: on_pre_llm_call recomputes it per user request and a
    # successful progress_tracker call clears it, so stale stored text cannot block forever.
    if not session.get("projectHint"):
        return None
    return {"action": "block", "message": _BLOCK_MESSAGE}


def _truncate_failed_output(result: Any) -> Optional[str]:
    """Bound stored failure output: first 8 lines, max 2000 chars."""
    if not isinstance(result, str):
        return None
    return "\n".join(result.splitlines()[:8])[:2000]


def _tool_handler(args: Any = None, **kwargs: Any) -> str:
    """Tool handler for progress_tracker. Returns JSON string per SPEC 4.5."""
    try:
        payload = dict(args) if isinstance(args, dict) else {}
        payload.update(kwargs)
        doc = store.get_document()
        sid = _sid(kwargs)
        result_json, _payload = store.apply_op(doc, payload["op"], payload, session_id=sid)
        store.save_document(doc)
        return result_json
    except Exception as exc:
        _log.exception("progress_tracker tool failed")
        return json.dumps({"error": f"progress_tracker: {exc}"})


def _hook(fn):
    """Wrap a hook to swallow all exceptions (logging only), but propagate return values."""
    def wrapper(**kwargs):
        try:
            return fn(**kwargs)
        except Exception as exc:
            _log.warning("hook %s failed: %s", fn.__name__, exc)
        return None
    return wrapper


class _MemoryState:
    """Fallback when the host ctx exposes no state facade (e.g. the 0.21.x validator)."""

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


# ─── Hook implementations ───

@_hook
def on_pre_tool_call(**kwargs):
    """Track one TOOL row per call per SPEC 2.3; negative tool names ignored; close-consecutive
    dedupe. No auto-bar: a bar is only created when the agent plans one — auto-building steps
    from tool names produced junk like "skill_view". Enforcement is narrow: once the user's
    request looks like project work, heavy tools are blocked until a real bar exists. The stall
    note rides on_pre_llm_call instead: a pre_tool_call return value is a tool directive
    (block/approve/modify), so a plain string there is dropped by the dispatcher."""
    tool_name = kwargs.get("tool_name") or kwargs.get("function_name") or ""
    if not tool_name or tool_name.startswith("-"):
        return
    sid = _sid(kwargs)
    doc = store.get_document()

    directive = _enforcement_directive(tool_name, sid)
    if directive is not None:
        return directive

    track = store.session_track(doc, sid)

    # Call counting / nudge logic (only for main-loop tools, not internal)
    is_main_loop = tool_name not in ("progress_tracker",)
    if is_main_loop:
        session = store.ensure_session(doc, sid)
        calls = session.get("callsSinceMove", 0) + 1
        session["callsSinceMove"] = calls

        # Track recent tool calls with args for stepNameOf (keep last 3)
        recent = session.get("recentTools", [])
        recent.append((tool_name, kwargs.get("args") or {}))
        if len(recent) > 3:
            recent = recent[-3:]
        session["recentTools"] = recent

        if not track:
            # No active track: just count calls. A bar is only created when the agent plans
            # one — auto-building steps from tool names produced junk like "skill_view".
            pass
        else:
            # Track exists: count calls toward the stall nudge (reset on movement in apply_op).
            track["callsSinceMove"] = track.get("callsSinceMove", 0) + 1

    store.add_tool_row(doc, tool_name, sid)
    store.save_document(doc)


@_hook
def on_post_tool_call(**kwargs):
    """Check failures per SPEC 2.4; permission completion on blocked; check clearing."""
    tool_name = kwargs.get("tool_name") or kwargs.get("function_name") or ""
    if not tool_name:
        return
    status = kwargs.get("status") or "ok"
    error_type = kwargs.get("error_type")
    error_message = kwargs.get("error_message") or ""
    session_id = _sid(kwargs)
    tool_call_id = kwargs.get("tool_call_id") or ""
    command = ""
    if tool_name == "Bash":
        args = kwargs.get("args") or {}
        command = args.get("command") or ""

    doc = store.get_document()
    track = store.session_track(doc, session_id)

    # A successful planning call satisfies enforcement for the request that raised it,
    # including a bar that just reached completed — otherwise a later quick check in the
    # same session stays blocked forever.
    if tool_name == "progress_tracker" and status == "ok":
        session = store.ensure_session(doc, session_id)
        session["projectHint"] = False

    # A failed step fails the list. Permission denials are blocked, not failed.
    # Bash stays upstream-strict (only isCheck commands fail): a nonzero grep/diff/test is
    # often a legitimate answer, and marking those red would cry wolf.
    denial = status == "error" and any(
        w in error_message.lower() for w in ("blocked", "approval", "denied", "cancelled")
    )
    counts_as_failure = tool_name != "Bash" or store.is_check_command(command)
    if status == "error" and not denial and counts_as_failure and track:
        idx = store.active_task_index(track)
        if idx >= 0:
            at = track["tasks"][idx]
            at["attempt"] = at.get("attempt", 0) + 1
            at["failures"] = at.get("failures", 0) + 1
            at["blockReason"] = command or tool_name
            at["ticket"] = tool_call_id
            track["failedCommand"] = command or tool_name
            track["failedReason"] = store.error_line_of(kwargs.get("result")) or error_message
            track["failedLines"] = store.error_lines_of(kwargs.get("result")) or _truncate_failed_output(kwargs.get("result"))
            track["failedToolUseId"] = tool_call_id
            track["failureToLog"] = True

    # Clearing: repeating the failed command (or tool) successfully clears the red state.
    # Upstream only knows Bash checks; a generalised failure needs the same door.
    if status == "ok" and track and track.get("failedCommand"):
        if store.normalize_command(track["failedCommand"]) == store.normalize_command(command or tool_name):
            store.clear_check(doc, command or tool_name, track)

    # Permission completion: status error with blocked/approval/denied in message
    if status == "error" and any(w in error_message.lower() for w in ("blocked", "approval", "denied", "cancelled")):
        # resolve newest open permission block
        for b in reversed(doc["log"]["blocks"]):
            if b["kind"] == "permission" and b.get("answeredAt") is None:
                b["answeredAt"] = store._now_ms()
                b["outcome"] = "Not approved"
                break

    # Close the tool row
    store.close_tool_row(doc, tool_name, status)
    store.save_document(doc)


@_hook
def on_pre_llm_call(**kwargs):
    """Begin turn — SPEC 2.1. The stall note (upstream :913/:990) rides this turn's injected
    context, which is the only plugin channel that reaches the model as text. Also records the
    user's request shape so multi-step enforcement survives later "continue" turns."""
    session_id = _sid(kwargs)
    turn_id = kwargs.get("turn_id")
    doc = store.get_document()
    store.begin_turn(doc, session_id, turn_id)
    user_message = kwargs.get("user_message")
    if not kwargs.get("parent_session_id") and isinstance(user_message, str) and user_message.strip():
        session = store.ensure_session(doc, session_id)
        session["lastUserMessage"] = user_message[:4000]
        # Intent is per-request: a later non-project question must not stay blocked forever.
        session["projectHint"] = _looks_like_multistep(user_message)
    store.save_document(doc)
    if kwargs.get("parent_session_id"):
        return ""  # upstream nudges the main loop only
    return store.nudge_note(doc, session_id)


@_hook
def on_post_llm_call(**kwargs):
    """End turn when open — SPEC 2.2. Token usage absent from payload; never invent."""
    session_id = _sid(kwargs)
    # tokens not provided by Hermes hook payload — explicitly not available
    doc = store.get_document()
    store.end_turn(doc, session_id, tokens=None)
    store.save_document(doc)


@_hook
def on_post_api_request(**kwargs):
    """Record one model call's tokens and the session's context fill — upstream's ctx/tokens.

    ``post_api_request`` is the only hook that carries ``usage`` (normalized buckets) and
    ``context_length`` (the model's window), so the pane's token and context tiles read from
    here rather than being guessed.
    """
    doc = store.get_document()
    store.record_api_call(
        doc,
        _sid(kwargs),
        usage=kwargs.get("usage") or {},
        context_length=kwargs.get("context_length"),
    )
    store.save_document(doc)


@_hook
def on_session_open(**kwargs):
    """Prune stale tracks, init session entry — SPEC 2.10."""
    session_id = _sid(kwargs)
    doc = store.get_document()
    store.ensure_session(doc, session_id)
    store.prune_stale_tracks(doc)
    store.save_document(doc)


@_hook
def on_session_start(**kwargs):
    """Alias of on_session_open — SPEC 2.10."""
    on_session_open(**kwargs)


@_hook
def on_session_end(**kwargs):
    """End turn, close blocks, dismiss this session's unfinished bar — SPEC 2.11.
    Completed bars stay visible until the next task replaces them."""
    session_id = _sid(kwargs)
    doc = store.get_document()
    store.end_turn(doc, session_id)
    store.end_block(doc)
    store.close_session_tracks(doc, session_id)
    store.save_document(doc)


@_hook
def on_session_reset(**kwargs):
    """Close blocks — SPEC 2.12."""
    doc = store.get_document()
    store.end_block(doc)
    store.save_document(doc)


@_hook
def on_session_finalize(**kwargs):
    """Close blocks and dismiss the session's unfinished bar — SPEC 2.12.
    Completed bars stay visible until the next task replaces them."""
    session_id = _sid(kwargs)
    doc = store.get_document()
    store.end_block(doc)
    store.close_session_tracks(doc, session_id)
    store.save_document(doc)


@_hook
def on_agent_loop_stopped(**kwargs):
    """Mark interrupted — SPEC 2.11."""
    doc = store.get_document()
    for track in doc["tracks"]:
        if track.get("statusKey") == "running":
            track["statusKey"] = "interrupted"
    store.save_document(doc)


@_hook
def on_subagent_start(**kwargs):
    """Start agent run — SPEC 4."""
    run_id = kwargs.get("child_subagent_id") or kwargs.get("run_id") or ""
    alt_id = kwargs.get("child_session_id") or ""
    parent_session = kwargs.get("parent_session_id") or ""
    doc = store.get_document()
    store.start_agent_run(doc, run_id, alt_id)
    store.save_document(doc)


@_hook
def on_subagent_stop(**kwargs):
    """Finish agent run — SPEC 4."""
    run_id = kwargs.get("child_subagent_id") or kwargs.get("child_session_id") or ""
    doc = store.get_document()
    store.finish_agent_run(doc, run_id)
    store.save_document(doc)


@_hook
def on_pre_approval_request(**kwargs):
    """Open permission block — SPEC 5.4 normalizeCommand, patterns.length ? 1 : 0."""
    command = kwargs.get("command") or ""
    session_key = kwargs.get("session_key") or ""
    pattern_keys = kwargs.get("pattern_keys") or []
    doc = store.get_document()
    store.open_permission_block(doc, command, session_key, pattern_keys)
    store.save_document(doc)


@_hook
def on_post_approval_response(**kwargs):
    """Close permission block — SPEC 5.4 resolvePermissionBlock terminal rule."""
    choice = kwargs.get("choice") or ""
    doc = store.get_document()
    store.resolve_permission_block(doc, choice)
    store.save_document(doc)


@_hook
def on_human_input_request(**kwargs):
    """Open question block — SPEC 5.5 never capture prompt text."""
    kind = kwargs.get("kind") or "question"
    doc = store.get_document()
    store.open_question_block(doc, kind, _sid(kwargs))
    store.save_document(doc)


@_hook
def on_human_input_resolved(**kwargs):
    """Close question block — SPEC 5.5 outcome normalized answered|dismissed."""
    outcome = kwargs.get("outcome") or "error"
    doc = store.get_document()
    store.resolve_question_block(doc, outcome)
    store.save_document(doc)


@_hook
def on_pre_verify(**kwargs):
    """Parse and ignore — SPEC 2.8."""
    pass


@_hook
def on_pre_command(**kwargs):
    """Parse and ignore — SPEC 2.8."""
    pass


# ─── Slash command ───

def _cmd_progress(raw_args: str = "", **kwargs) -> str:
    """Compact text summary of tracks/tokens/log for the current session."""
    session_id = kwargs.get("session_id") or kwargs.get("session_key") or "default"
    if raw_args.strip():
        # raw_args may carry a session id
        session_id = raw_args.strip().split()[0]
    doc = store.get_document()
    snap = store.snapshot(doc, session_id)
    tracks = snap.get("tracks", [])
    if not tracks:
        return "progress: no bar yet; create one with title and tasks."
    t = tracks[0]
    parts = []
    parts.append(f"📊 {t['title']} — {t['result']}")
    if t.get("tasks"):
        for tk in t["tasks"]:
            mark = "●" if tk["state"] == "completed" else ("▶" if tk["state"] == "in_progress" else "○")
            flag = " ⚡" if tk.get("attempt", 0) > 0 else ""
            parts.append(f"  {mark} {tk['label']}{flag}")
    cfg = snap.get("config", {})
    parts.append(f"🔧 max_findings={cfg.get('maxFindings')} max_turns={cfg.get('maxTurnsRetained')}")
    return "\n".join(parts)


# ─── Plugin entry point ───

PROGRESS_RULE = (
    "Decomposable work is tracked, not optional. Before starting, decompose the user's request "
    "into goal-level steps and call progress_tracker with {\"op\": \"title\", \"title\": <goal>, "
    "\"tasks\": [...]}; then move through them with {\"op\": \"next\"} as each completes. Skip only trivial one-shot actions "
    "that cannot be decomposed. Heavy execution tools are blocked until a real bar exists — "
    "a fabricated or tool-name-derived step list is forbidden."
)


def register(ctx) -> None:
    """Hermes plugin entry point."""
    # Load config (SPEC 6.6 inputs)
    cfg = {
        "enabled_tools": ctx.get_config("enabled_tools", []) or [],
        "enabled_findings": ctx.get_config("enabled_findings", ["found", "failed"]) or ["found", "failed"],
        "max_findings": int(ctx.get_config("max_findings", 200) or 200),
        "max_turns_retained": int(ctx.get_config("max_turns_retained", 250) or 250),
    }
    store.configure(cfg)
    state = getattr(ctx, "state", None)
    if state is None:
        _log.warning("ctx.state unavailable; progress-tracker persistence is in-memory only")
        state = _MemoryState()
    store.bind(state)

    # Tool
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=TOOL_SCHEMA,
        handler=_tool_handler,
        description="Track the user's plan as goal-level tasks (not tool names), blocks, and findings",
        emoji="📈",
    )

    # Standing rule for every session: decomposable work is decomposed and tracked.
    register_section = getattr(ctx, "register_system_prompt_section", None)
    if callable(register_section):
        try:
            register_section("progress-tracker", PROGRESS_RULE, max_chars=900)
        except (TypeError, ValueError) as exc:
            _log.warning("progress-tracker prompt section unavailable: %s", exc)
    else:
        _log.warning("progress-tracker prompt section API unavailable on this Hermes host")

    # Hooks — must match provides_hooks in plugin.yaml exactly
    hooks = [
        ("pre_tool_call", on_pre_tool_call),
        ("post_tool_call", on_post_tool_call),
        ("pre_llm_call", on_pre_llm_call),
        ("post_llm_call", on_post_llm_call),
        ("post_api_request", on_post_api_request),
        ("on_session_start", on_session_open),
        ("on_session_end", on_session_end),
        ("on_session_reset", on_session_reset),
        ("on_session_finalize", on_session_finalize),
        ("agent_loop_stopped", on_agent_loop_stopped),
        ("subagent_start", on_subagent_start),
        ("subagent_stop", on_subagent_stop),
        ("pre_approval_request", on_pre_approval_request),
        ("post_approval_response", on_post_approval_response),
        ("on_human_input_request", on_human_input_request),
        ("on_human_input_resolved", on_human_input_resolved),
        ("pre_verify", on_pre_verify),
        ("pre_command", on_pre_command),
    ]
    for name, fn in hooks:
        ctx.register_hook(name, fn)

    # Slash command (wrap TypeError for older signature as done-bell does)
    try:
        ctx.register_command(
            "progress",
            handler=_cmd_progress,
            description="Show progress bar summary for the current session",
            args_hint="[session]",
        )
    except TypeError:
        ctx.register_command(
            "progress",
            handler=_cmd_progress,
            description="Show progress bar summary for the current session",
            args_hint="[session]",
        )


def get_document(state_facade: Any) -> Dict[str, Any]:
    """Module-level accessor for the dashboard: whole-document JSON under the facade."""
    return store.get_document(state_facade)


def save_document(doc: Dict[str, Any], state_facade: Any) -> None:
    """Module-level saver for the dashboard."""
    store.save_document(doc, state_facade)