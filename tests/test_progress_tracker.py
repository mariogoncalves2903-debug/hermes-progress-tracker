"""Unit tests for progress-tracker plugin (unittest, no pytest)."""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, Dict

import progress_store as store
from __init__ import (
    _cmd_progress,
    _looks_like_multistep,
    on_pre_tool_call,
    on_post_tool_call,
    on_pre_llm_call,
    on_post_llm_call,
    on_session_open,
    on_session_end,
    on_session_reset,
    on_agent_loop_stopped,
    on_subagent_start,
    on_subagent_stop,
    on_pre_approval_request,
    on_post_approval_response,
    on_human_input_request,
    on_human_input_resolved,
    on_pre_verify,
    on_pre_command,
)


class _PlainDictFacade:
    """A plain dict wrapped to the PluginState get/set protocol."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.data: Dict[str, Any] = {}
        self.facade = _PlainDictFacade(self.data)
        store.bind(self.facade)
        store.configure(dict(store.DEFAULTS))
        store.configure({
            "max_findings": 10,
            "max_turns_retained": 5,
            "enabled_tools": [],
            "enabled_findings": ["found", "failed"],
        })

    def tearDown(self) -> None:
        store.bind(None)

    def _doc(self) -> Dict[str, Any]:
        return store.get_document(self.facade)

    def _save(self, doc: Dict[str, Any]) -> None:
        store.save_document(doc, self.facade)

    def _track(self, title: str = "T", subjects=("A", "B")) -> Dict[str, Any]:
        """Create a track and persist it as the only track."""
        t = store.new_track(title, list(subjects))
        doc = self._doc()
        doc["tracks"] = [t]
        self._save(doc)
        return t


class TestProgressTracker(_StoreCase):
    def test_init_empty_title(self) -> None:
        """Track created with empty title keeps the empty title (SPEC: label null/empty)."""
        t = store.new_track("", ["a", "b"])
        self.assertEqual(t["title"], "")
        self.assertEqual(len(t["tasks"]), 2)

    def test_tasks_creation_first_active_rest_pending(self) -> None:
        """SPEC 4.3/4.4: first task in_progress, rest pending.

        The request's wording ("first pending then rest active") is read as
        ambiguous; SPEC.md line 505 is authoritative.
        """
        t = store.new_track("Title", ["first", "second", "third"])
        self.assertEqual(t["activeTaskId"], "step1")
        self.assertEqual(t["tasks"][0]["state"], "in_progress")
        self.assertEqual(t["tasks"][1]["state"], "pending")
        self.assertEqual(t["tasks"][2]["state"], "pending")

    def test_add_happy_path_pending_stays_active_advances(self) -> None:
        """add appends pending; active on a later task completes earlier ones."""
        self._track("T", ["A", "B"])
        doc = self._doc()
        store.apply_op(doc, "add", {"add": ["C", "D"]})
        self._save(doc)
        track = self._doc()["tracks"][0]
        self.assertEqual([tk["state"] for tk in track["tasks"]],
                         ["in_progress", "pending", "pending", "pending"])
        self.assertEqual([tk["id"] for tk in track["tasks"]],
                         ["step1", "step2", "add3", "add4"])

        store.apply_op(doc, "active", {"active": "C"})
        self._save(doc)
        track = self._doc()["tracks"][0]
        self.assertEqual([tk["state"] for tk in track["tasks"]],
                         ["completed", "completed", "in_progress", "pending"])

    def test_add_promotes_first_pending_when_none_active(self) -> None:
        """SPEC: add with no in_progress promotes the first pending task."""
        self._track("T", ["A", "B"])
        doc = self._doc()
        store.apply_op(doc, "next", {})   # complete A, B becomes in_progress
        store.apply_op(doc, "next", {})   # complete B -> none in progress
        doc = self._doc()
        store.apply_op(doc, "add", {"add": ["C"]})
        self._save(doc)
        track = self._doc()["tracks"][0]
        self.assertEqual(track["tasks"][-1]["state"], "in_progress")
        self.assertEqual(track["activeTaskId"], "add3")

    def test_next_empty_triggers_skip_recovery(self) -> None:
        """SPEC upTo: next with no in_progress closes the first non-completed task."""
        self._track("T", ["A", "B"])
        doc = self._doc()
        doc["tracks"][0]["activeTaskId"] = None  # simulate no active task
        self._save(doc)
        res, _ = store.apply_op(doc, "next", {})
        self._save(doc)
        self.assertNotIn("error", json.loads(res))
        track = self._doc()["tracks"][0]
        self.assertEqual(track["tasks"][0]["state"], "completed")
        self.assertEqual(track["activeTaskId"], "step2")

        # all completed -> no error, stays done
        store.apply_op(doc, "next", {})
        self._save(doc)
        res2, _ = store.apply_op(doc, "next", {})
        self.assertNotIn("error", json.loads(res2))
        self.assertEqual(json.loads(res2)["result"], "2/2, done")

    def test_skip_failed_retry_add_chain(self) -> None:
        """skip -> failed -> next (retry) -> add chain."""
        self._track("T", ["A", "B", "C"])
        doc = self._doc()
        store.apply_op(doc, "skip", {})
        store.apply_op(doc, "failed", {"reason": "boom", "ticket": "call_1"})
        self._save(doc)
        doc = self._doc()
        track = doc["tracks"][0]
        self.assertTrue(track["tasks"][0]["skipped"])
        self.assertEqual(track["tasks"][1]["attempt"], 1)
        self.assertEqual(track["tasks"][1]["blockReason"], "boom")
        self.assertEqual(track["failedRuns"], 1)
        # retry: next completes B, starts C, clears failure
        store.apply_op(doc, "next", {})
        self._save(doc)
        doc = self._doc()
        track = doc["tracks"][0]
        self.assertEqual(track["activeTaskId"], "step3")
        self.assertEqual(track["tasks"][2]["state"], "in_progress")
        self.assertIsNone(track["failedCommand"])
        store.apply_op(doc, "add", {"add": ["D"]})
        self._save(doc)
        self.assertEqual(len(self._doc()["tracks"][0]["tasks"]), 4)

    def test_attempt_accounting_shows_attempt_1_2(self) -> None:
        """Result line includes 'attempt 1/2' after the first failure."""
        self._track("T", ["A"])
        doc = self._doc()
        store.apply_op(doc, "failed", {"reason": "err", "ticket": "c1"})
        self._save(doc)
        track = self._doc()["tracks"][0]
        self.assertIn("attempt 1/2", store.result_line(track))
        store.apply_op(doc, "failed", {"reason": "err", "ticket": "c2"})
        self._save(doc)
        track = self._doc()["tracks"][0]
        self.assertIn("attempt 2/2", store.result_line(track))

    def test_unknown_task_message(self) -> None:
        """Exact SPEC deny message for an unresolved task name."""
        self._track("T", ["A", "B"])
        doc = self._doc()
        res, _ = store.apply_op(doc, "active", {"active": "nope"})
        self.assertEqual(
            json.loads(res)["error"],
            'progress_tracker: no task named "nope"; tasks are "A", "B".',
        )

    def test_found_dedupe_identical_and_different(self) -> None:
        """Identical findings deduped; different kind/text stored."""
        self._track("T", ["A"])
        doc = self._doc()
        for args in (
            {"found": "same", "kind": "found"},
            {"found": "same", "kind": "found"},
            {"found": "diff", "kind": "found"},
            {"found": "diff", "kind": "failed"},
        ):
            store.apply_op(doc, "found", args)
        self._save(doc)
        findings = self._doc()["log"]["findings"]
        self.assertEqual(
            [(f["kind"], f["text"]) for f in findings],
            [("found", "same"), ("found", "diff"), ("failed", "diff")],
        )

    def test_block_cap_eviction_max_findings(self) -> None:
        """With max_findings=2, oldest findings evicted first."""
        store.configure({"max_findings": 2})
        self._track("T", ["A"])
        doc = self._doc()
        for i in range(3):
            store.apply_op(doc, "found", {"found": f"f{i}", "kind": "found"})
        self._save(doc)
        findings = self._doc()["log"]["findings"]
        self.assertEqual([f["text"] for f in findings], ["f1", "f2"])

    def test_turn_cap_eviction_endblock_trimtimeline(self) -> None:
        """max_turns_retained=2: 3rd beginTurn closes blocks and trims, then evicts oldest turn."""
        store.configure({"max_turns_retained": 2, "max_findings": 5})
        self._track("T", ["A"])
        doc = self._doc()
        store.apply_op(doc, "found", {"found": "f1"})
        store.apply_op(doc, "found", {"found": "f2"})
        store.open_permission_block(doc, "cmd", "sk")
        store.begin_turn(doc, "s1", "t1")
        store.begin_turn(doc, "s1", "t2")
        store.begin_turn(doc, "s1", "t3")
        self._save(doc)
        doc = self._doc()
        self.assertEqual([t["id"] for t in doc["turns"]], ["t2", "t3"])
        perm = next(b for b in doc["log"]["blocks"] if b["kind"] == "permission")
        self.assertIsNotNone(perm.get("answeredAt"))   # endBlock fired
        self.assertLessEqual(len(doc["log"]["findings"]) + len(doc["log"]["blocks"]), 5)

    def test_row_of_ranking(self) -> None:
        """row_rank ranks blocks/findings newest-to-oldest for dashboard ordering."""
        self.assertEqual(store.row_rank("blocked"), 0)
        self.assertEqual(store.row_rank("permission"), 1)
        self.assertEqual(store.row_rank("question"), 2)
        self.assertEqual(store.row_rank("subagent"), 3)
        self.assertEqual(store.row_rank("skill"), 3)
        self.assertEqual(store.row_rank("lifecycle"), 4)
        self.assertEqual(store.row_rank("tool"), 5)
        self.assertEqual(store.row_rank("unknown"), 99)

    def test_stamp_source_precedence(self) -> None:
        """Precedence: blocked > permission > question > newest(subagent|skill) > lifecycle > tool."""
        now = store._now_ms()
        candidates = {
            "tool": now, "lifecycle": now + 10, "skill": now + 20,
            "subagent": now + 30, "question": now + 40,
            "permission": now + 50, "blocked": now + 60,
        }
        self.assertEqual(store.stamp_source(candidates), "blocked")
        del candidates["blocked"]
        self.assertEqual(store.stamp_source(candidates), "permission")
        del candidates["permission"]
        self.assertEqual(store.stamp_source(candidates), "question")
        del candidates["question"]
        self.assertEqual(store.stamp_source(candidates), "subagent")
        del candidates["subagent"]
        self.assertEqual(store.stamp_source(candidates), "skill")
        del candidates["skill"]
        self.assertEqual(store.stamp_source(candidates), "lifecycle")
        del candidates["lifecycle"]
        self.assertEqual(store.stamp_source(candidates), "tool")

    def test_negative_tool_names_create_no_row(self) -> None:
        """pre_tool_call with '-' prefix or empty name creates no TOOL row."""
        self._track("T", ["A"])
        on_pre_tool_call(tool_name="-internal", session_id="s1")
        on_pre_tool_call(tool_name="", session_id="s1")
        on_pre_tool_call(tool_name="Bash", session_id="s1")
        doc = self._doc()
        rows = [b for b in doc["log"]["blocks"] if b.get("kind") == "tool"]
        self.assertEqual([r["tool"] for r in rows], ["Bash"])

    def test_close_consecutive_tool_dedupe(self) -> None:
        """Consecutive identical tool calls do not open a second row."""
        self._track("T", ["A"])
        on_pre_tool_call(tool_name="Bash", session_id="s1")
        on_pre_tool_call(tool_name="Bash", session_id="s1")
        doc = self._doc()
        rows = [b for b in doc["log"]["blocks"] if b.get("kind") == "tool"]
        self.assertEqual(len(rows), 1)
        on_post_tool_call(tool_name="Bash", status="ok")
        on_pre_tool_call(tool_name="Bash", session_id="s1")
        doc = self._doc()
        rows = [b for b in doc["log"]["blocks"] if b.get("kind") == "tool"]
        self.assertEqual(len(rows), 2)

    def test_subagent_direct_span_exclusion(self) -> None:
        """subagent_start/stop record under the active task only; no timeline row."""
        self._track("T", ["A"])
        on_subagent_start(child_subagent_id="run1", child_session_id="cs1", parent_session_id="s1")
        doc = self._doc()
        self.assertEqual(len(doc["tracks"][0]["tasks"][0]["agentRuns"]), 1)
        # subagent_stop payload has child_session_id, no child_subagent_id
        on_subagent_stop(child_session_id="cs1")
        doc = self._doc()
        run = doc["tracks"][0]["tasks"][0]["agentRuns"][0]
        self.assertIsNotNone(run["endedAt"])
        self.assertEqual([b for b in doc["log"]["blocks"] if b.get("kind") == "tool"], [])

    def test_permission_close_on_latest_approval_response(self) -> None:
        """post_approval_response closes the newest open permission block; first answer wins."""
        self._track("T", ["A"])
        on_pre_approval_request(command="cmd1", session_key="sk", pattern_keys=["p1"])
        on_pre_approval_request(command="cmd2", session_key="sk", pattern_keys=["p2"])
        doc = self._doc()
        perms = [b for b in doc["log"]["blocks"] if b["kind"] == "permission"]
        self.assertEqual(len(perms), 2)
        self.assertEqual(perms[1]["details"]["patterns"], 1)
        self.assertEqual(perms[1]["details"]["command"], "cmd2")

        on_post_approval_response(choice="approve")
        doc = self._doc()
        perms = [b for b in doc["log"]["blocks"] if b["kind"] == "permission"]
        self.assertEqual(perms[-1]["outcome"], "Approved")
        self.assertIsNotNone(perms[-1]["answeredAt"])
        on_post_approval_response(choice="deny")   # terminal rule: ignored
        doc = self._doc()
        perms = [b for b in doc["log"]["blocks"] if b["kind"] == "permission"]
        self.assertEqual(perms[-1]["outcome"], "Approved")

    def test_question_resolve_by_kind(self) -> None:
        """on_human_input_resolved normalizes outcome to answered|dismissed."""
        self._track("T", ["A"])
        on_human_input_request(kind="question")
        doc = self._doc()
        qs = [b for b in doc["log"]["blocks"] if b["kind"] == "question"]
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["text"], "question")
        self.assertEqual(qs[0]["details"]["kind"], "question")

        on_human_input_resolved(outcome="answered")
        doc = self._doc()
        qs = [b for b in doc["log"]["blocks"] if b["kind"] == "question"]
        self.assertEqual(qs[0]["outcome"], "answered")

        on_human_input_request(kind="clarify")
        on_human_input_resolved(outcome="dismissed")
        doc = self._doc()
        qs = [b for b in doc["log"]["blocks"] if b["kind"] == "question"]
        self.assertEqual(qs[-1]["details"]["kind"], "clarify")
        self.assertEqual(qs[-1]["outcome"], "dismissed")


class TestMultistepEnforcement(_StoreCase):
    PROJECT_MSG = (
        "can you build a harness for Gemini Desktop so it can run in the background and "
        "deliver an API? Let's say I have Gemini running in the background and I plug the "
        "API into Nine Router, then take the reply and convert it into something usable."
    )

    def _seed_project_intent(self, session_id: str = "s1") -> None:
        on_pre_llm_call(session_id=session_id, turn_id="t1", user_message=self.PROJECT_MSG)

    def _make_track(self, title: str, session_id: str = "s1", completed: bool = False) -> Dict[str, Any]:
        t = store.new_track(title, ["a", "b"], session_id=session_id)
        if completed:
            for task in t["tasks"]:
                task["state"] = "completed"
            t["statusKey"] = "completed"
            t["activeTaskId"] = None
        doc = self._doc()
        doc.setdefault("tracks", []).append(t)
        self._save(doc)
        return t

    def test_detector_boundaries(self) -> None:
        self.assertFalse(_looks_like_multistep("fix this bug"))
        self.assertFalse(_looks_like_multistep(None))
        self.assertTrue(_looks_like_multistep(self.PROJECT_MSG))
        self.assertFalse(_looks_like_multistep(
            "x" * 120 + " build build build build build build build build build build"
        ))

    def test_blocks_heavy_tool_until_bar_exists(self) -> None:
        self._seed_project_intent()
        result = on_pre_tool_call(tool_name="terminal", args={"command": "echo hi"}, session_id="s1")
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("action"), "block")
        self.assertIn('"op": "title"', result.get("message", ""))

    def test_read_only_tool_not_blocked(self) -> None:
        self._seed_project_intent()
        self.assertIsNone(on_pre_tool_call(tool_name="search_files", args={}, session_id="s1"))

    def test_progress_tracker_tool_not_blocked(self) -> None:
        self._seed_project_intent()
        self.assertIsNone(on_pre_tool_call(tool_name="progress_tracker", args={"op": "title"}, session_id="s1"))

    def test_active_bar_allows_heavy_tool(self) -> None:
        self._seed_project_intent()
        self._make_track("Build harness")
        self.assertIsNone(on_pre_tool_call(tool_name="terminal", args={"command": "echo hi"}, session_id="s1"))

    def test_completed_bar_does_not_cover_next_task(self) -> None:
        self._seed_project_intent()
        self._make_track("Old task", completed=True)
        result = on_pre_tool_call(tool_name="write_file", args={}, session_id="s1")
        self.assertEqual((result or {}).get("action"), "block")

    def test_short_request_not_blocked(self) -> None:
        on_pre_llm_call(session_id="s1", turn_id="t1", user_message="what time is it?")
        self.assertIsNone(on_pre_tool_call(tool_name="terminal", args={}, session_id="s1"))

    def test_subagent_intent_not_recorded(self) -> None:
        on_pre_llm_call(
            session_id="child1", turn_id="t1", user_message=self.PROJECT_MSG,
            parent_session_id="s1",
        )
        self.assertIsNone(on_pre_tool_call(tool_name="terminal", args={}, session_id="child1"))

    def test_block_does_not_count_as_tool_call(self) -> None:
        self._seed_project_intent()
        before = self._doc()["sessions"]["s1"].get("callsSinceMove", 0)
        on_pre_tool_call(tool_name="terminal", args={}, session_id="s1")
        after = self._doc()["sessions"]["s1"].get("callsSinceMove", 0)
        self.assertEqual(before, after)


class TestHookWrappers(_StoreCase):
    def test_hooks_swallow_exceptions(self) -> None:
        """Hook wrappers must catch every exception (a plugin bug cannot break a turn)."""
        store.bind(None)
        try:
            on_pre_tool_call(tool_name="Bash", session_id="s1")
            on_post_tool_call(tool_name="Bash", status="ok")
            on_pre_llm_call(session_id="s1", turn_id="t1")
            on_post_llm_call(session_id="s1")
            on_session_open(session_id="s1")
            on_session_end(session_id="s1")
            on_session_reset()
            on_agent_loop_stopped()
            on_subagent_start(child_subagent_id="r", parent_session_id="s1")
            on_subagent_stop(child_subagent_id="r")
            on_pre_approval_request(command="c", session_key="k")
            on_post_approval_response(choice="approve")
            on_human_input_request(kind="q")
            on_human_input_resolved(outcome="answered")
            on_pre_verify()
            on_pre_command()
        except Exception as exc:  # pragma: no cover
            self.fail(f"Hook raised exception: {exc}")
        finally:
            store.bind(self.facade)


class TestSessionCloseAndPrune(_StoreCase):
    def test_session_end_keeps_completed_and_dismisses_unfinished(self) -> None:
        """A completed bar stays visible until the next task; unfinished ones stop reading as running."""
        doc = self._doc()
        unfinished = store.new_track("A", ["a1", "a2"], session_id="s1")
        complete = store.new_track("A done", ["a1", "a2"], session_id="s1")
        for task in complete["tasks"]:
            task["state"] = "completed"
        other = store.new_track("B", ["b1", "b2"], session_id="s2")
        doc["tracks"] = [unfinished, complete, other]
        self._save(doc)

        on_session_end(session_id="s1")

        by_title = {t["title"]: t for t in self._doc()["tracks"]}
        self.assertTrue(by_title["A"]["isDismissed"])
        self.assertFalse(by_title["A done"]["isDismissed"])
        self.assertFalse(by_title["B"]["isDismissed"])

    def test_new_task_replaces_completed_bar(self) -> None:
        """A new title+tasks payload clears the previous completed bar from this session."""
        doc = self._doc()
        old = store.new_track("old done", ["a"], session_id="s1")
        for task in old["tasks"]:
            task["state"] = "completed"
        doc["tracks"] = [old]
        self._save(doc)

        store.apply_op(doc, "title", {"title": "next task", "tasks": ["one", "two"]}, session_id="s1")

        tracks = [t for t in doc["tracks"] if t.get("sessionId") == "s1"]
        self.assertEqual([t["title"] for t in tracks], ["next task"])

    def test_prune_keeps_complete_but_drops_stale_unfinished(self) -> None:
        """Completed bars persist past the stale window; stale unfinished/dismissed bars expire."""
        stale = store._now_ms() - store.STALE_MS - 1000
        old_dismissed = store.new_track("old", ["a"], session_id="s1")
        old_dismissed["isDismissed"] = True
        old_complete = store.new_track("done", ["a"], session_id="s2")
        for task in old_complete["tasks"]:
            task["state"] = "completed"
        for track in (old_dismissed, old_complete):
            track["updatedAt"] = stale
            track["createdAt"] = stale
        doc = self._doc()
        doc["tracks"] = [old_dismissed, old_complete, store.new_track("fresh", ["a"])]
        self._save(doc)

        store.prune_stale_tracks(self._doc())

        self.assertEqual([t["title"] for t in self._doc()["tracks"]], ["done", "fresh"])


    def test_wait_time_is_scoped_to_the_tracks_own_session(self) -> None:
        """Another session's open blocks must not inflate this bar's waited time."""
        other = store.new_track("other", ["a"], session_id="s2")
        mine = store.new_track("mine", ["b"], session_id="s1")
        doc = self._doc()
        doc["tracks"] = [other, mine]
        hour = 60 * 60 * 1000
        other["createdAt"] -= hour
        doc["log"]["blocks"] = [
            {"id": "b1", "kind": "question", "text": "q", "at": store._now_ms() - hour // 2,
             "answeredAt": None, "outcome": None, "details": {"kind": "question", "sessionId": "s2"}},
        ]
        self._save(doc)

        snap = store.snapshot(doc, "s1", include_all=True)

        by_sid = {t["sessionId"]: t for t in snap["tracks"]}
        self.assertEqual(by_sid["s1"]["waitMs"], 0, "s2's block leaked into s1's wait")
        self.assertGreater(by_sid["s2"]["waitMs"], 0)


class TestDesktopContract(_StoreCase):
    """Desktop pane <-> Python backend field and arg-shape contract."""

    def test_single_track_replacement(self) -> None:
        """SPEC 1.3: tasks op replaces the track list; only one active track."""
        doc = self._doc()
        store.apply_op(doc, "title", {"title": "T1", "tasks": ["a", "b"]})
        self._save(doc)
        doc = self._doc()
        store.apply_op(doc, "tasks", {"tasks": ["c", "d"]})
        self._save(doc)
        doc = self._doc()
        self.assertEqual(len(doc["tracks"]), 1, "tasks must replace, not insert")
        self.assertEqual([t["label"] for t in doc["tracks"][0]["tasks"]], ["c", "d"])

    def test_title_with_create_payload_creates_bar(self) -> None:
        """title op with tasks create payload creates bar when no tracks."""
        doc = self._doc()
        payload = {"tasks": [{"op": "create", "title": "NewBar", "tasks": ["x", "y"]}]}
        store.apply_op(doc, "title", payload)
        self._save(doc)
        doc = self._doc()
        self.assertEqual(len(doc["tracks"]), 1)
        self.assertEqual(doc["tracks"][0]["title"], "NewBar")
        self.assertEqual([t["label"] for t in doc["tracks"][0]["tasks"]], ["x", "y"])

    def test_title_with_string_list_creates_bar(self) -> None:
        """title op with tasks as string list creates bar when no tracks."""
        doc = self._doc()
        store.apply_op(doc, "title", {"title": "Bar2", "tasks": ["p", "q"]})
        self._save(doc)
        doc = self._doc()
        self.assertEqual(len(doc["tracks"]), 1)
        self.assertEqual(doc["tracks"][0]["title"], "Bar2")
        self.assertEqual([t["label"] for t in doc["tracks"][0]["tasks"]], ["p", "q"])

    def test_title_replace_existing_with_create_payload(self) -> None:
        """title op with create payload replaces existing track."""
        doc = self._doc()
        store.apply_op(doc, "title", {"title": "Old", "tasks": ["a"]})
        self._save(doc)
        payload = {"tasks": [{"op": "create", "title": "Replaced", "tasks": ["b"]}]}
        store.apply_op(doc, "title", payload)
        self._save(doc)
        doc = self._doc()
        self.assertEqual(len(doc["tracks"]), 1)
        self.assertEqual(doc["tracks"][0]["title"], "Replaced")
        self.assertEqual([t["label"] for t in doc["tracks"][0]["tasks"]], ["b"])

    def test_arg_alias_active_task(self) -> None:
        """active op accepts 'task' alias for desktop shape."""
        doc = self._doc()
        store.apply_op(doc, "title", {"title": "T", "tasks": ["a", "b"]})
        self._save(doc)
        doc = self._doc()
        store.apply_op(doc, "active", {"task": "b"})  # alias
        self._save(doc)
        doc = self._doc()
        self.assertEqual(doc["tracks"][0]["activeTaskId"], doc["tracks"][0]["tasks"][1]["id"])

    def test_arg_alias_add_tasks(self) -> None:
        """add op accepts 'tasks' list alias for desktop shape."""
        doc = self._doc()
        store.apply_op(doc, "title", {"title": "T", "tasks": ["a"]})
        self._save(doc)
        doc = self._doc()
        store.apply_op(doc, "add", {"tasks": ["b", "c"]})  # alias
        self._save(doc)
        doc = self._doc()
        self.assertEqual([t["label"] for t in doc["tracks"][0]["tasks"]], ["a", "b", "c"])

    def test_arg_alias_found_text(self) -> None:
        """found op accepts 'text' alias for desktop shape."""
        doc = self._doc()
        store.apply_op(doc, "title", {"title": "T", "tasks": ["a"]})
        self._save(doc)
        doc = self._doc()
        store.apply_op(doc, "found", {"text": "note via alias"})
        self._save(doc)
        doc = self._doc()
        findings = doc["log"]["findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["text"], "note via alias")

    def test_snapshot_union_fields_present(self) -> None:
        """snapshot includes both port and desktop field names for each task."""
        doc = self._doc()
        store.apply_op(doc, "title", {"title": "T", "tasks": ["a"]})
        self._save(doc)
        doc = self._doc()
        snap = store.snapshot(doc, "default")
        track = snap["tracks"][0]
        task = track["tasks"][0]
        # port names
        self.assertIn("id", task)
        self.assertIn("label", task)
        self.assertIn("state", task)
        self.assertIn("attempt", task)
        self.assertIn("maxAttempts", task)
        self.assertIn("failures", task)
        self.assertIn("blockReason", task)
        self.assertIn("skipped", task)
        # desktop names
        self.assertIn("subject", task)
        self.assertIn("status", task)
        self.assertIn("attempts", task)
        self.assertIn("isSkipped", task)
        self.assertIn("startedAt", task)
        self.assertIn("note", task)
        self.assertIn("ticket", task)
        # track desktop names
        self.assertIn("caption", track)
        self.assertIn("description", track)
        self.assertIn("label", track)
        self.assertIn("startedAt", track)

    def test_snapshot_scopes_bars_per_session_and_can_include_all(self) -> None:
        """Each chat sees its own bar; the sidebar pane can list every session's bars."""
        doc = self._doc()
        store.apply_op(doc, "title", {"title": "A", "tasks": ["a"]}, session_id="s1")
        store.apply_op(doc, "title", {"title": "B", "tasks": ["b"]}, session_id="s2")
        self._save(doc)
        doc = self._doc()

        mine = store.snapshot(doc, "s1")
        self.assertEqual([t["title"] for t in mine["tracks"]], ["A"])
        self.assertEqual(mine["tracks"][0]["sessionId"], "s1")

        every = store.snapshot(doc, "s1", include_all=True)
        self.assertEqual({t["sessionId"] for t in every["tracks"]}, {"s1", "s2"})
        # cross-session failure detail redacted
        for t in every["tracks"]:
            if t["sessionId"] != "s1":
                for k in ("failedCommand", "failedReason", "failedLines", "failedToolUseId"):
                    self.assertNotIn(k, t, f"{k} leaked for foreign session")

    def test_failed_lines_truncation(self) -> None:
        """failedLines truncated to first 8 lines, max 2000 chars; never stored on success."""
        from __init__ import _truncate_failed_output
        # truncation
        long = "line\n" * 20
        out = _truncate_failed_output(long)
        self.assertEqual(out.count("\n"), 7)  # 8 lines -> 7 newlines
        self.assertLessEqual(len(out), 2000)
        # non-string -> None
        self.assertIsNone(_truncate_failed_output(None))
        self.assertIsNone(_truncate_failed_output(123))


class TestSlashCommand(_StoreCase):
    def test_no_bar(self) -> None:
        res = _cmd_progress("")
        self.assertIn("no bar yet", res)

    def test_shows_summary(self) -> None:
        self._track("MyBar", ["A", "B", "C"])
        doc = self._doc()
        store.apply_op(doc, "next", {})   # complete A, B active
        self._save(doc)
        res = _cmd_progress("")
        self.assertIn("MyBar", res)
        self.assertIn("●", res)   # completed
        self.assertIn("▶", res)   # in_progress
        self.assertIn("○", res)   # pending


class TestConfigSchema(_StoreCase):
    def test_defaults(self) -> None:
        store.configure(dict(store.DEFAULTS))
        self.assertEqual(store._config["max_findings"], 200)
        self.assertEqual(store._config["max_turns_retained"], 250)
        self.assertEqual(store._config["enabled_tools"], [])
        self.assertEqual(store._config["enabled_findings"], ["found", "failed"])

    def test_override(self) -> None:
        store.configure({
            "max_findings": 5, "max_turns_retained": 2,
            "enabled_tools": ["Bash"], "enabled_findings": ["failed"],
        })
        self.assertEqual(store._config["max_findings"], 5)
        self.assertEqual(store._config["max_turns_retained"], 2)
        self.assertEqual(store._config["enabled_tools"], ["Bash"])
        self.assertEqual(store._config["enabled_findings"], ["failed"])


class TestDashboardAPI(_StoreCase):
    def test_routes(self) -> None:
        """FastAPI routes: snapshot, timeline, demo (plain-dict facade driver)."""
        from dashboard import plugin_api as api
        api.set_facade(self.facade)
        self._track("Dash", ["A", "B"])
        doc = self._doc()
        store.apply_op(doc, "next", {})
        self._save(doc)

        snap = asyncio.run(api.snapshot("default"))
        self.assertEqual(snap["tracks"][0]["title"], "Dash")
        self.assertEqual(snap["tracks"][0]["tasks"][0]["state"], "completed")
        self.assertEqual(snap["config"]["maxFindings"], 10)

        self.assertEqual(asyncio.run(api.timeline("default")), [])

        demo = asyncio.run(api.demo())
        self.assertEqual(demo["title"], "Feature: Progress Tracker")
        self.assertEqual(len(demo["tasks"]), 6)
        # The palette driver replays demo.steps — the endpoint must serve them, and every
        # step needs the {delayMs, op, args} shape the driver reads.
        self.assertEqual(len(demo["steps"]), 14)
        for step in demo["steps"]:
            self.assertIn("op", step)
            self.assertIn("delayMs", step)
            self.assertIn("args", step)


if __name__ == "__main__":
    unittest.main(verbosity=2)