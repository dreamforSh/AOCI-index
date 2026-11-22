#!/usr/bin/env python3
"""Multi-host lifecycle hook sessions and the convergent `Stop` hook."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    RepoTestCase,
    aoci_index,
    git_ops,
    hook,
)


class HookSessionTests(RepoTestCase):
    def test_stop_ignores_preexisting_drift_but_flags_new_edits(self):
        self.write("a.py", "a = 1\n")
        self.write("b.py", "b = 1\n")
        self.init()
        self.upsert(
            "a.py[CX2T]: F:defines constant a value | R:- | A:- | S:-",
            "b.py[CX2T]: F:defines constant b value | R:- | A:- | S:-",
        )
        self.write("a.py", "a = 2\n")  # user's own pending edit before the session
        base = {"cwd": str(self.root), "session_id": "sess-1"}
        start = hook("session-start", dict(base, hook_event_name="SessionStart", source="startup"))
        self.assertIn("additionalContext", start["hookSpecificOutput"])
        self.assertTrue((self.root / ".aoci" / ".sessions" / "sess-1.json").exists())

        stop = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertEqual(stop, {})

        self.write("b.py", "b = 2\n")  # agent edit during the session
        stop = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertEqual(stop.get("decision"), "block")
        self.assertIn("b.py", stop["reason"])
        self.assertNotIn("modified(2)", stop["reason"])

        self.write("a.py", "a = 3\n")  # agent now also touched the pre-existing file
        stop = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertIn("modified(2)", stop["reason"])

    def test_antigravity_contract_and_fully_idle(self):
        self.write("a.py", "a = 1\n")
        self.init()
        payload = {"cwd": str(self.root), "conversationId": "c1", "hookEventName": "Stop",
                   "terminationReason": "NO_TOOL_CALL", "continuationActive": False}
        out = hook("stop", payload, host="antigravity")
        self.assertEqual(out.get("decision"), "continue")
        self.assertEqual(hook("stop", dict(payload, fullyIdle=False), host="antigravity"), {})
        start = hook("session-start", {"cwd": str(self.root), "conversationId": "c2",
                                       "hookEventName": "SessionStart"}, host="antigravity")
        self.assertIn("systemMessage", start["injectSteps"][0])

    def test_stop_detects_replaced_malformed_line_and_retires_resolved_drift(self):
        self.write("a.py", "a = 1\n")
        self.init()
        self.upsert("a.py[CX2T]: F:defines constant a value | R:- | A:- | S:-")
        code_file = self.root / ".aoci" / "aoci.code.txt"
        code_file.write_text(code_file.read_text(encoding="utf-8") + "old broken line\n", encoding="utf-8")

        base = {"cwd": str(self.root), "session_id": "sess-mal"}
        hook("session-start", dict(base, hook_event_name="SessionStart"))
        self.assertEqual(hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False)), {})

        # Replacing the old malformed line with a DIFFERENT malformed line keeps count=1, but must be caught
        code_file.write_text(
            code_file.read_text(encoding="utf-8").replace("old broken line", "new broken line"),
            encoding="utf-8",
        )
        stop = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertEqual(stop.get("decision"), "block")
        self.assertIn("malformed_lines(1)", stop["reason"])

    def test_turn_start_exempts_mid_session_user_edits_without_hiding_agent_edits(self):
        self.write("user_edit.py", "u = 1\n")
        self.write("agent_edit.py", "g = 1\n")
        self.init()
        self.upsert(
            "user_edit.py[CX2T]: F:holds user edited constant | R:- | A:- | S:-",
            "agent_edit.py[CX2T]: F:holds agent edited constant | R:- | A:- | S:-",
        )
        base = {"cwd": str(self.root), "conversationId": "conv-turn"}
        hook("session-start", dict(base, hookEventName="SessionStart"), host="antigravity")
        self.assertEqual(
            hook("stop", dict(base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False), host="antigravity"),
            {},
        )

        # User edits `user_edit.py` in IDE between Turn 1 and Turn 2
        self.write("user_edit.py", "u = 2\n")
        # Turn 2 starts and is CANCELED mid-turn (no Stop runs!) — must not leave any stuck flag
        self.assertEqual(
            hook("auto", dict(base, hookEventName="PreInvocation", invocationNum=1), host="antigravity"),
            {},
        )
        # User edits `user_edit.py` again before Turn 3; `invocationNum=1` still refreshes statelessly
        self.write("user_edit.py", "u = 3\n")
        self.assertEqual(
            hook("auto", dict(base, hookEventName="PreInvocation", invocationNum=1), host="antigravity"),
            {},
        )
        self.assertEqual(
            hook("stop", dict(base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False), host="antigravity"),
            {},
        )

        # Turn 4: `invocationNum=1` starts turn, agent edits `agent_edit.py`, `invocationNum=2` must NOT absorb it
        hook("auto", dict(base, hookEventName="PreInvocation", invocationNum=1), host="antigravity")
        self.write("agent_edit.py", "g = 2\n")
        hook("auto", dict(base, hookEventName="PreInvocation", invocationNum=2), host="antigravity")
        stop = hook(
            "stop",
            dict(base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False),
            host="antigravity",
        )
        self.assertEqual(stop.get("decision"), "continue")
        self.assertIn("agent_edit.py", stop["reason"])
        self.assertNotIn("modified(2)", stop["reason"])

        # Claude Code `UserPromptSubmit` refreshes unconditionally without any invocationNum
        cc_base = {"cwd": str(self.root), "session_id": "cc-1"}
        self.write("user_edit.py", "u = 4\n")
        self.assertEqual(hook("auto", dict(cc_base, hook_event_name="UserPromptSubmit"), host="claude-code"), {})
        self.upsert("agent_edit.py[CX2T]: F:holds updated agent edited constant | R:- | A:- | S:-")
        self.assertEqual(hook("stop", dict(cc_base, hook_event_name="Stop", stop_hook_active=False), host="claude-code"), {})

    def test_hook_skips_git_subprocess_when_no_aoci_dir(self):
        import aoci_hook

        with tempfile.TemporaryDirectory() as plain_dir:
            with mock.patch.object(git_ops, "detect_git_info", side_effect=AssertionError("git must not be called")):
                with mock.patch.object(aoci_index, "detect_git_info", side_effect=AssertionError("git must not be called")):
                    self.assertEqual(aoci_hook.discover_indexed_roots({"cwd": plain_dir}), [])

    def test_codex_hook_contract_auto_detect_session_turn_and_stop(self):
        self.write("a.py", "a = 1\n")
        self.write("b.py", "b = 1\n")
        self.init()
        self.upsert(
            "a.py[CX2T]: F:defines constant a value | R:- | A:- | S:-",
            "b.py[CX2T]: F:defines constant b value | R:- | A:- | S:-",
        )
        self.write("a.py", "a = 2\n")  # pre-existing user edit before session start

        base = {
            "cwd": str(self.root),
            "session_id": "codex-1",
            "transcript_path": None,
            "model": "gpt-5",
            "permission_mode": "default",
        }
        start = hook(
            "session-start",
            dict(base, hook_event_name="SessionStart", source="startup"),
            host="codex",
        )
        self.assertEqual(set(start.keys()), {"hookSpecificOutput"})
        self.assertEqual(set(start["hookSpecificOutput"].keys()), {"hookEventName", "additionalContext"})
        self.assertEqual(start["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("aoci_index.py", start["hookSpecificOutput"]["additionalContext"])

        compact = hook(
            "session-start",
            dict(base, hook_event_name="SessionStart", source="compact"),
            host="codex",
        )
        self.assertIn("post-compaction context refresh", compact["hookSpecificOutput"]["additionalContext"])

        # Auto-detect Codex via `turn_id` / `last_assistant_message`
        stop_clean = hook(
            "auto",
            dict(base, turn_id="t1", hook_event_name="Stop", stop_hook_active=False, last_assistant_message=None),
            host="auto",
        )
        self.assertEqual(stop_clean, {})

        # Turn-start (`UserPromptSubmit`) refreshes user edit baseline
        self.write("a.py", "a = 3\n")
        self.assertEqual(
            hook(
                "auto",
                dict(base, turn_id="t2", hook_event_name="UserPromptSubmit", prompt="update b"),
                host="auto",
            ),
            {},
        )

        # Agent modifies b.py -> Stop blocks with strict StopCommandOutputWire keys (never hookSpecificOutput)
        self.write("b.py", "b = 2\n")
        stop_block = hook(
            "auto",
            dict(base, turn_id="t2", hook_event_name="Stop", stop_hook_active=False, last_assistant_message="done"),
            host="auto",
        )
        self.assertEqual(set(stop_block.keys()), {"decision", "reason"})
        self.assertEqual(stop_block["decision"], "block")
        self.assertNotIn("hookSpecificOutput", stop_block)
        self.assertIn("b.py", stop_block["reason"])
        self.assertNotIn("modified(2)", stop_block["reason"])

        # Loop protection: stop_hook_active=True without progress stops retrying
        stop_cap = hook(
            "auto",
            dict(base, turn_id="t2", hook_event_name="Stop", stop_hook_active=True, last_assistant_message="retry"),
            host="auto",
        )
        self.assertEqual(stop_cap, {})

        # Non-indexed workspace returns {} across all Codex events
        with tempfile.TemporaryDirectory() as plain_dir:
            for ev, extra in (
                ("session-start", {"hook_event_name": "SessionStart", "source": "startup"}),
                ("turn-start", {"turn_id": "t0", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}),
                ("stop", {"turn_id": "t0", "hook_event_name": "Stop", "stop_hook_active": False}),
            ):
                self.assertEqual(hook(ev, {"cwd": plain_dir, "session_id": "s0", **extra}, host="codex"), {})


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):

    def test_convergent_stop_hook_retry_on_progress_and_exit_on_stall_or_cap(self):
        self.init()
        base = {"cwd": str(self.root), "session_id": "conv-1"}
        hook("session-start", dict(base, hook_event_name="SessionStart"))
        for name in ("a.py", "b.py", "c.py"):
            self.write(name, f"{name[0]} = 1\n")

        # Stop #1 with 3 unsynced files -> blocks and persists stop_retries=1, last_blocked_count=3
        s1 = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertEqual(s1.get("decision"), "block")
        snap1 = json.loads((self.root / ".aoci" / ".sessions" / "conv-1.json").read_text(encoding="utf-8"))
        self.assertEqual(snap1["stop_retries"], 1)
        self.assertEqual(snap1["last_blocked_count"], 3)

        # Agent syncs `a.py` (3 -> 2 unsynced): strict convergence allows ONE extra continuation block
        self.assertEqual(self.upsert("a.py[CX2T]: F:defines module constant a | R:- | A:- | S:-")[0], 0)
        s2 = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=True))
        self.assertEqual(s2.get("decision"), "block")
        snap2 = json.loads((self.root / ".aoci" / ".sessions" / "conv-1.json").read_text(encoding="utf-8"))
        self.assertEqual(snap2["stop_retries"], 2)
        self.assertEqual(snap2["last_blocked_count"], 2)

        # Even if agent makes progress again (2 -> 1), stop_retries >= 2 exits immediately
        self.assertEqual(self.upsert("b.py[CX2T]: F:defines module constant b | R:- | A:- | S:-")[0], 0)
        s3 = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=True))
        self.assertEqual(s3, {})

        # New turn-start resets retry counters; finishing cleanly keeps counters at 0
        hook("turn-start", dict(base, hook_event_name="UserPromptSubmit"))
        snap_reset = json.loads((self.root / ".aoci" / ".sessions" / "conv-1.json").read_text(encoding="utf-8"))
        self.assertEqual(snap_reset["stop_retries"], 0)
        self.assertEqual(snap_reset["last_blocked_count"], 0)

        # Missing session_id with continuationActive=True returns {} immediately
        self.assertEqual(
            hook("stop", {"cwd": str(self.root), "hookEventName": "Stop", "continuationActive": True}, host="antigravity"),
            {},
        )

        # Stalled continuation (`cur_unsynced >= last_blocked_count`) returns {} immediately
        stall_base = {"cwd": str(self.root), "conversationId": "conv-stall"}
        hook("session-start", dict(stall_base, hookEventName="SessionStart"), host="antigravity")
        self.write("d.py", "d = 1\n")
        st1 = hook(
            "stop",
            dict(stall_base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False),
            host="antigravity",
        )
        self.assertEqual(st1.get("decision"), "continue")
        st2 = hook(
            "stop",
            dict(stall_base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=True),
            host="antigravity",
        )
        self.assertEqual(st2, {})

        # Clean finish resets `stop_retries` and `last_blocked_count` to 0
        self.assertEqual(
            self.upsert(
                "c.py[CX2T]: F:defines module constant c | R:- | A:- | S:-",
                "d.py[CX2T]: F:defines module constant d | R:- | A:- | S:-",
            )[0],
            0,
        )
        hook("turn-start", dict(base, hook_event_name="UserPromptSubmit"))
        self.assertEqual(hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False)), {})
        snap_clean = json.loads((self.root / ".aoci" / ".sessions" / "conv-1.json").read_text(encoding="utf-8"))
        self.assertEqual(snap_clean["stop_retries"], 0)
        self.assertEqual(snap_clean["last_blocked_count"], 0)

        # Clean finish on `conv-stall` also resets `stop_retries` and `last_blocked_count` without `turn-start`
        self.assertEqual(
            hook(
                "stop",
                dict(stall_base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False),
                host="antigravity",
            ),
            {},
        )
        stall_clean = json.loads((self.root / ".aoci" / ".sessions" / "conv-stall.json").read_text(encoding="utf-8"))
        self.assertEqual(stall_clean["stop_retries"], 0)
        self.assertEqual(stall_clean["last_blocked_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
