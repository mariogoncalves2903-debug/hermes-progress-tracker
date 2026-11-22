"""Dashboard/Desktop backend — mounted at /api/plugins/progress-tracker/."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

_ROOT = Path(__file__).resolve().parent.parent
_root_str = str(_ROOT)
if _root_str in sys.path:
    sys.path.remove(_root_str)
sys.path.insert(0, _root_str)

import progress_store as store
from fastapi import APIRouter, Request

router = APIRouter()

# Override for tests: set_facade(facade) — a PluginState or a plain dict
_FACADE = None


def set_facade(facade: Any) -> None:
    """Set a custom facade (PluginState or dict) for the dashboard. For tests."""
    global _FACADE
    _FACADE = facade


def _state() -> Any:
    """Get the state facade: test override, or real PluginState, or a plain dict."""
    if _FACADE is not None:
        return _FACADE
    try:
        from hermes_cli.plugins_state import PluginState
        return PluginState("progress-tracker", "progress-tracker")
    except Exception:
        # Fallback for environments without PluginState (e.g., tests)
        return {"progress": store._empty_document()}


def _doc() -> Dict[str, Any]:
    """Load the document using the current facade."""
    return store.get_document(_state())


def _save(doc: Dict[str, Any]) -> None:
    """Save the document using the current facade."""
    store.save_document(doc, _state())


@router.get("/snapshot")
async def snapshot(session: str = "default", all: bool = False) -> Dict[str, Any]:
    """Compact snapshot for the dashboard. `all` = every session's bars (sidebar pane)."""
    doc = _doc()
    return store.snapshot(doc, session, include_all=all)


@router.get("/timeline")
async def timeline(session: str = "default") -> list[Dict[str, Any]]:
    """Merged timeline (blocks + findings + turns) for the dashboard."""
    doc = _doc()
    return store.timeline(doc, session)


@router.post("/op")
async def op(request: Request) -> Dict[str, Any]:
    """Apply an op and return the updated snapshot."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    session = body.get("session", "default")
    op_name = body.get("op")
    args = body.get("args", {})
    if not op_name:
        return {"error": "op is required"}
    doc = _doc()
    result_json, _payload = store.apply_op(doc, op_name, args, session_id=session)
    _save(doc)
    return {"result": json.loads(result_json), "snapshot": store.snapshot(doc, session)}


@router.get("/demo")
async def demo() -> Dict[str, Any]:
    """Return the SPEC 6.6 demo script as structured data."""
    return store.demo_script()