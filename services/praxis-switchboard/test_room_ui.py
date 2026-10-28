
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import room_ui
import room_runtime
import switchboard
import project_rooms

def visible(text):
    return room_ui.SGR.sub("", room_ui.OSC_LINK.sub("", text))

class TestWrapping(unittest.TestCase):
    def test_visible_columns_and_unicode_clusters(self):
        body = "회의 요약 " * 8 + "e\u0301 " * 4 + "👩🏽‍💻 " * 4 + "🇰🇷 " * 4 + "x" * 100
        for width in (8, 12, 24, 40, 80, 120):
            lines = room_ui.wrap_text(body, width)
            self.assertTrue(all(room_ui.cell_width(visible(line)) <= width for line in lines))
            self.assertEqual("".join(lines).replace(" ", ""), body.replace(" ", ""))
            self.assertFalse(any(line.startswith(("\u0301", "\u200d", "🏽")) for line in lines))

    def test_osc8_wraps_label_not_uri_and_closes_before_each_newline(self):
        target = "https://example.org/" + "long-address" * 100
        label = "Open the full research materials and wireframe"
        linked = f"\x1b]8;;{target}\x1b\\{label}\x1b]8;;\x1b\\"
        lines = room_ui.wrap_text(linked, 18)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(visible(line) for line in lines), label)
        for line in lines:
            self.assertLessEqual(room_ui.cell_width(visible(line)), 18)
            self.assertEqual(room_ui.OSC_LINK.findall(line), [target, ""])

    def test_paragraphs_lists_code_and_explicit_newlines(self):
        lines = room_ui.wrap_text("Intro.\n\n- First bullet is deliberately long for this pane.\n"
                                 "  1. Nested item also needs wrapping.\n\n```python\n"
                                 "    text = 'abcdef0123456789'\n```", 24)
        self.assertEqual(lines[:2], ["Intro.", ""])
        self.assertTrue(lines[3].startswith("  "))
        self.assertIn("```python", lines)
        self.assertTrue(all(room_ui.cell_width(line) <= 24 for line in lines))

class TestRoomPresentation(unittest.TestCase):
    def test_user_and_agent_share_turn_without_mutating_events(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        event = {"type": "result", "seat": "Kip", "state": "succeeded",
                 "summary": "Read [the sample](https://example.org/demo).", "artifacts": []}
        before = copy.deepcopy(event)
        with contextlib.redirect_stdout(stream):
            ui = room_ui.RoomUI(plain=True, width=30)
            turn = ui.user("@Kip Please show me the result of this long question.")
            ui.printer(turn)(event)
        output = stream.getvalue()
        self.assertIn("01 · YOU", output)
        self.assertIn("01 · @Kip · RESPONSE", output)
        self.assertIn("┃ @Kip", output)
        self.assertIn("│ Read", output)
        self.assertIn("\x1b]8;;https://example.org/demo\x1b\\the sample", output)
        self.assertNotIn("\x1b[", output)                                              
        self.assertEqual(event, before)
        self.assertTrue(all(room_ui.cell_width(visible(line)) <= 30 for line in output.splitlines()))

    def test_pipe_keeps_markdown_target_and_never_emits_ansi(self):
        body = "[A report](</a long path/" + "x" * 120 + ".html>)"
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            room_ui.RoomUI(width=24).block("@Ada · RESPONSE", body, turn=3)
        self.assertIn(body, stream.getvalue())
        self.assertNotIn("\x1b", stream.getvalue())

    def test_failure_is_labeled_and_never_success_coloured(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        with contextlib.redirect_stdout(stream):
            room_ui.RoomUI().printer(1)({"type": "result", "seat": "Ada", "state": "failed",
                                        "summary": "Stopped.", "contract_reasons": ["Missing evidence"]})
        self.assertIn("RESPONSE · failed", stream.getvalue())
        self.assertIn("Missing evidence", stream.getvalue())
        self.assertIn(room_ui.PALETTE["error"], stream.getvalue())
        self.assertNotIn(room_ui.PALETTE["agent"], stream.getvalue())

    def test_width_is_refreshed_between_blocks(self):
        ui = room_ui.RoomUI()
        with mock.patch.object(room_ui.shutil, "get_terminal_size",
                               side_effect=[os.terminal_size((80, 24)), os.terminal_size((24, 24))]):
            self.assertEqual(ui.width(), 78)
            self.assertEqual(ui.width(), 22)

    def test_non_tty_input_and_context_are_unchanged(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
             mock.patch("builtins.input", return_value="  @all hello  ") as read:
            with room_ui.RoomUI() as ui:
                self.assertEqual(ui.read(), "@all hello")
                self.assertIsNone(ui.editor)
            read.assert_called_once_with("\nYOU › ")

    def test_async_callbacks_capture_their_own_turn_not_latest_prompt(self):
        stream = io.StringIO()
        reached, release = threading.Event(), threading.Event()

        class Board:
            def handle(self, prompt, sender, post, **kwargs):
                reached.set()
                release.wait(3)
                post({"type": "result", "seat": "Leo", "state": "succeeded", "summary": prompt})
                return {"lifecycle": "completed"}

        with contextlib.redirect_stdout(stream), tempfile.TemporaryDirectory() as tmp:
            for kind in ("leo", "project"):
                ui = room_ui.RoomUI(plain=True)
                if kind == "leo":
                    session = switchboard.LeoRoomSession(Board, switchboard._sender(), lambda: None)
                else:
                    session = project_rooms.ProjectRoomSession(
                        mock.Mock(), mock.Mock(), Board, switchboard._sender(), lambda: None,
                        output_dir=Path(tmp))
                with mock.patch.object(project_rooms, "contextual_request", side_effect=lambda r, p, text: text), \
                     mock.patch.object(project_rooms.ProjectRoomSession, "_record_prompt", return_value=Path(tmp)/"ROOM.md"):
                    reached.clear()
                    release.clear()
                    session.submit("First", post=ui.printer(ui.user("First")))
                    self.assertTrue(reached.wait(1))
                    session.submit("Second", post=ui.printer(ui.user("Second")))
                    release.set()
                    session.close()
        output = stream.getvalue()
        self.assertEqual(output.count("01 · @Leo · RESPONSE"), 2)
        self.assertEqual(output.count("02 · @Leo · RESPONSE"), 2)

    def test_all_interactive_rooms_use_the_same_surface(self):
        for run in (switchboard._run_leo_room, switchboard._run_project_room,
                    switchboard._run_live_project_room):
            self.assertTrue(hasattr(run, "__wrapped__"))

    def test_all_room_loops_label_questions_and_results_and_close_once(self):
        for kind in ("leo", "project", "live"):
            stream = io.StringIO()
            session = mock.Mock()
            session.active.return_value = 0
            session.close.return_value = []
            session.sessions = {}
            session.sources = []
            prompt = "@Ada A question\nwith a second line"
            result = mock.Mock(seat="Ada", body="A response", outcome="succeeded")
            session.submit.side_effect = lambda text, post: post({
                "type": "result", "seat": "Ada", "state": "succeeded", "summary": "A response"})
            session.run_turn.side_effect = lambda text, on_result, question_handler: on_result(result)
            factory = {"leo": "LeoRoomSession", "project": "project_rooms.ProjectRoomSession",
                       "live": "live_room.LiveRoomSession"}[kind]
            run = {"leo": switchboard._run_leo_room, "project": switchboard._run_project_room,
                   "live": switchboard._run_live_project_room}[kind]
            args = () if kind == "leo" else (mock.Mock(), mock.Mock())
            with self.subTest(kind=kind), contextlib.redirect_stdout(stream), \
                 mock.patch("switchboard." + factory, return_value=session), \
                 mock.patch.object(project_rooms, "output_root", return_value=Path("/outputs")), \
                 mock.patch.object(sys.stdin, "isatty", return_value=False), \
                 mock.patch("builtins.input", side_effect=["", "/help", "/status", prompt, "/exit"]):
                self.assertEqual(run(*args, plain=True), 0)
            self.assertIn("01 · YOU", stream.getvalue())
            self.assertIn("01 · @Ada · RESPONSE", stream.getvalue())
            if kind == "live":
                self.assertEqual(session.run_turn.call_args.args[0], prompt)
                session.prewarm.assert_called_once()
            else:
                self.assertEqual(session.submit.call_args.args[0], prompt)
            session.close.assert_called_once()

    def test_live_question_reply_uses_same_turn_and_eof_closes_cleanly(self):
        for answer in ("My answer", EOFError()):
            session = mock.Mock()
            session.close.return_value = []
            replies = []
            def ask(text, on_result, question_handler):
                replies.append(question_handler("Ada", "Which option?"))
            session.run_turn.side_effect = ask
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), \
                 mock.patch.object(switchboard.live_room, "LiveRoomSession", return_value=session), \
                 mock.patch.object(sys.stdin, "isatty", return_value=False), \
                 mock.patch("builtins.input", side_effect=["@Ada hello", answer, "/exit"]):
                self.assertEqual(switchboard._run_live_project_room(mock.Mock(), mock.Mock(), plain=True), 0)
            self.assertIn("01 · @Ada · QUESTION FOR YOU", stream.getvalue())
            if isinstance(answer, str):
                self.assertIn("01 · YOU · REPLY TO @Ada", stream.getvalue())
                self.assertEqual(replies, [answer])
            session.close.assert_called_once()

class TestRoomRuntime(unittest.TestCase):
    def test_missing_runtime_does_not_launch_anything(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(room_runtime, "RUNTIME", Path(tmp) / "missing"), \
             mock.patch.object(room_runtime.os, "execv") as execute:
            with self.assertRaisesRegex(RuntimeError, "install-switchboard-runtime.sh"):
                room_runtime.ensure_interactive_runtime()
            execute.assert_not_called()

    def test_existing_alias_reexecs_with_exact_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "bin").mkdir()
            (runtime / "bin/python").touch()
            with mock.patch.object(room_runtime, "RUNTIME", runtime), \
                 mock.patch.object(room_runtime.os, "execv") as execute, \
                 mock.patch.object(sys, "argv", ["sb", "--plain", "room", "A project", "--foreground"]):
                room_runtime.ensure_interactive_runtime()
            python, args = execute.call_args.args
            self.assertEqual(python, str(runtime / "bin/python"))
            self.assertEqual(args[2:], ["--plain", "room", "A project", "--foreground"])

try:
    import prompt_toolkit
except ImportError:
    prompt_toolkit = None

@unittest.skipUnless(prompt_toolkit, "run with the managed room runtime for editor tests")
class TestEditor(unittest.TestCase):
    def _input(self, keys):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from prompt_toolkit.data_structures import Size

        class NarrowOutput(DummyOutput):
            def get_size(self):
                return Size(rows=12, columns=24)

        with create_pipe_input() as pipe:
            editor = room_ui.create_editor(input=pipe, output=NarrowOutput())
            pipe.send_text(keys)
            return editor.prompt("YOU › ")

    def test_multiline_paste_is_one_message_not_commands(self):
        text = "@Kip A pasted question\n/exit\n@Ada continuation"
        self.assertEqual(self._input("\x1b[200~" + text + "\x1b[201~\r"), text)

    def test_backspace_and_arrows_across_wraps(self):
        text = "@Kip " + "a" * 70
        self.assertEqual(self._input(text + "\x7f\x7f\x1b[DZ\r"), text[:-3] + "Z" + "a")

    def test_escape_enter_inserts_line_and_enter_submits(self):
        self.assertEqual(self._input("@Ada first\x1b\rsecond\r"), "@Ada first\nsecond")

    def test_ctrl_c_and_eof_propagate(self):
        with self.assertRaises(KeyboardInterrupt):
            self._input("\x03")
        with self.assertRaises(EOFError):
            self._input("\x04")

    @unittest.skipUnless(os.name == "posix", "requires a POSIX PTY")
    def test_real_narrow_pty_background_output_preserves_draft(self):
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 15, 24, 0, 0))
        code = """
import json, threading, time
from pathlib import Path
from room_ui import RoomUI
with RoomUI(plain=True) as ui:
    def answer():
        time.sleep(.35)
        ui.printer(1)({'type':'result','seat':'Ada','state':'succeeded',
                      'summary':'[Web](https://example.org/room) Report: ' +
                                str(Path('ROOM-UI.md').absolute()) + '\\nBackground answer.'})
    worker = threading.Thread(target=answer)
    worker.start()
    result = ui.read()
    worker.join()
print('RESULT=' + json.dumps(result))
"""
        process = subprocess.Popen([sys.executable, "-c", code], stdin=slave, stdout=slave,
                                   stderr=slave, cwd=Path(__file__).parent,
                                   env={**os.environ, "TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"})
        os.close(slave)
        output = b""

        def collect(until, timeout=4):
            nonlocal output
            deadline = time.monotonic() + timeout
            while until not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    try:
                        chunk = os.read(master, 65536)
                        if not chunk:
                            break
                        output += chunk
                    except OSError:
                        break
            self.assertIn(until, output)

        try:
            collect(b"\x1b[?2004h")
            draft = "@Kip " + "wrapped input " * 5
            os.write(master, draft.encode())
            collect(b"Background answer.")
            os.write(master, b"\x7fZ\r")
            collect(b"RESULT=")
            process.wait(timeout=4)
            while select.select([master], [], [], .05)[0]:
                try:
                    chunk = os.read(master, 65536)
                    if not chunk:
                        break
                    output += chunk
                except OSError:
                    break
            match = re.search(rb'RESULT=("[^\r\n]*")', output)
            self.assertIsNotNone(match, output)
            self.assertEqual(json.loads(match.group(1)), draft[:-1] + "Z")

            self.assertIn(b"\x1b]8;;https://example.org/room\x1b\\Web\x1b]8;;\x1b\\", output)
            target = (Path(__file__).parent / "ROOM-UI.md").as_uri().encode()
            self.assertIn(b"\x1b]8;;" + target + b"\x1b\\", output)
            for line in output.split(b"\n"):
                opens = re.findall(rb"\x1b\]8;;[^\x1b]+\x1b\\", line)
                self.assertEqual(len(opens), line.count(b"\x1b]8;;\x1b\\"), line)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
            os.close(master)

if __name__ == "__main__":
    unittest.main()
