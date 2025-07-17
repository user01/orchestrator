"""
Textual-based UI for the orchestrator backend.
"""
from __future__ import annotations
import sys
import os
import signal
import select
import termios
import tty
import asyncio
from pathlib import Path
from typing import Optional
from rich.text import Text
from textual.app import App
from textual.widgets import DataTable, Footer, Input, Button
from textual.widgets import RichLog as _RichLog
from textual.containers import Horizontal
from textual.reactive import reactive
import re
import time
from datetime import datetime
from .backend import OrchestratorBackend, load_config, format_duration, State, Kind

_ANSI_ESCAPE = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')

# Custom RichLog subclass that toggles auto_scroll based on user scrolls
class LogView(_RichLog):
    """RichLog that disables auto-scroll on user scroll-up, re-enables at bottom."""
    async def on_key(self, event) -> None:
        # Only when this widget has focus
        if self.has_focus:
            if event.key in ("up", "pageup", "home"):
                self.auto_scroll = False
            elif event.key in ("down", "pagedown", "end"):
                if self.scroll_offset.y >= self.max_scroll_y:
                    self.auto_scroll = True
        # Delegate to default key handler for scrolling and other key events
        await super().handle_key(event)


class OrchApp(App):
    """
    A Textual application for orchestrating and displaying tasks.

    This application provides a user interface for managing and monitoring a set of
    tasks defined in a configuration file. It displays the status of each task,
    their dependencies, and their logs in real-time.

    Attributes:
        BINDINGS (list[tuple[str, str, str]]): Key bindings for the application.
        DEFAULT_CSS (str): The default CSS for the application.
        selected_row (int): The currently selected row in the task table.
        filter_task (Optional[str]): The name of the task to filter logs by.
    """

    # Key bindings: quit
    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    #task_table {
        height: auto;
        max-height: 50%;
    }
    #log_view {
        height: 1fr;
    }
    #stdin_container {
        height: auto;
    }
    """
    # Reactive state: selected row index and current log filter
    selected_row: int = reactive(0)
    filter_task: Optional[str] = reactive(None)

    def __init__(self, config: Path) -> None:
        """
        Initializes the OrchApp.

        Args:
            config: The path to the configuration file.
        """
        super().__init__()
        self.config = config
        self.backend: OrchestratorBackend
        self.tasks: list[str] = []
        # Store all logs for filtering
        self.all_logs: list[str] = []
        # (No initial suppression; allow selecting tasks immediately)
        # Suppress row highlight events during programmatic updates
        self._suppress_row_highlight = False
        # (unused) flag to suppress first highlight after show-all
        self._ignore_next_row_highlight = False
        # Background task references for clean shutdown
        self._backend_task: asyncio.Task | None = None
        self._log_watcher_task: asyncio.Task | None = None
        # Timestamping: track last log time for periodic timestamps
        self._last_log_time: float | None = None
        # Seconds between timestamp inserts (interval for time-only stamps)
        self._timestamp_interval: float = 60.0

    async def on_mount(self) -> None:
        """
        Called when the app is mounted.

        Initializes the backend, sets up the UI components, and starts the
        background tasks for running the orchestrator and watching for logs.
        """
        # Load configuration and start backend
        cfgs = load_config(self.config)
        self.backend = OrchestratorBackend(cfgs)
        # Add meta task at index 0 for showing all logs
        self.tasks = ["All"] + list(self.backend.rt.keys())
        # Precompute max label width for log alignment
        self.max_label_len = max(len(name) for name in self.tasks)

        # Set up task table
        self.table = DataTable(zebra_stripes=True, name="task_table")
        # Highlight entire rows
        self.table.cursor_type = "row"
        self.table.add_columns("#", "Task", "State", "Deps", "Time")
        for idx, name in enumerate(self.tasks):
            if idx == 0:
                # Meta task row: no state, deps, or time
                self.table.add_row(str(idx), name, "", "", "")
            else:
                deps = ", ".join(self.backend.rt[name].cfg.depends_on)
                self.table.add_row(str(idx), name, "", deps, "")

        # Set up log view
        # Give a custom name to avoid clashing with App.log property
        # Set up log view with auto-scroll and scroll-tracking
        self.log_view = LogView(highlight=False, markup=False, name="log_view")
        self.log_view.auto_scroll = True

        # Add input and button for stdin
        self.stdin_input = Input(placeholder="Send to stdin...")
        self.stdin_button = Button("Send")
        self.stdin_container = Horizontal(
            self.stdin_input,
            self.stdin_button,
            id="stdin_container"
        )
        self.stdin_container.display = False

        # Layout: table on top, logs below, footer at bottom
        await self.mount(self.table)
        await self.mount(self.log_view)
        await self.mount(self.stdin_container)
        await self.mount(Footer())

        # Start backend run and log watcher, store tasks for shutdown
        self._backend_task = asyncio.create_task(self.backend.run())
        self._log_watcher_task = asyncio.create_task(self._watch_logs())
        # Refresh table periodically
        self.set_interval(0.5, self.refresh_tasks)
        # Focus the table for navigation
        self.set_focus(self.table)

    async def _watch_logs(self) -> None:
        """
        Watches for new log entries and updates the log view.

        Continuously pulls log entries from the backend's log queue, stores them,
        and writes them to the log view. Inserts a full date/time stamp at the
        first log and time-only stamps periodically. In all-tasks mode, logs are
        prefixed with a colored task label; in single-task mode, labels are
        omitted and only content is shown.
        """
        while True:
            # Pull raw log line from backend, strip ANSI and control codes
            raw_line = await self.backend.log_queue.get()
            # Remove ANSI escape sequences
            clean = _ANSI_ESCAPE.sub('', raw_line)
            # Remove any remaining non-printable/control characters
            line = ''.join(ch for ch in clean if ch.isprintable() or ch.isspace())
            # record every log line
            self.all_logs.append(line)
            # Only process if matches current filter (or show all)
            if self.filter_task is None or line.startswith(f"[{self.filter_task}]"):
                # Timestamp insertion: initial full date/time or time-only after interval
                now_ts = time.time()
                if (self._last_log_time is None) or (now_ts - self._last_log_time >= self._timestamp_interval):
                    # Full timestamp on first log, then time-only
                    if self._last_log_time is None:
                        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        stamp = datetime.now().strftime("%H:%M:%S")
                    ts_txt = Text(stamp, style="bold dim")
                    self.log_view.write(ts_txt)
                    self._last_log_time = now_ts
                # Preserve scroll position
                offset_y = self.log_view.scroll_offset.y
                max_y = self.log_view.max_scroll_y
                at_bottom = (offset_y >= max_y)
                # Determine label and content
                end = line.find("]")
                if end != -1:
                    label = line[1:end]
                    rest = line[end+1:]
                else:
                    label = None
                    rest = line
                # Render line depending on filter mode
                if self.filter_task is None:
                    # All tasks: show label padded and colored
                    padded = (label or "").ljust(self.max_label_len)
                    full = f"[{padded}]{rest}"
                    txt = Text(full)
                    if label and label in self.backend.rt:
                        txt.stylize(self.backend.rt[label].colour, 0, len(padded) + 2)
                else:
                    # Single-task mode: only show content, no label prefix
                    txt = Text(rest)
                self.log_view.write(txt)
                # auto-scroll is managed by RichLog.auto_scroll

    def refresh_tasks(self) -> None:
        """
        Refreshes the task table with the latest status.

        This method is called periodically to update the task table with the
        latest state, duration, and other information from the backend.
        """
        # Preserve current selection and suppress highlights during update
        prev_row = self.selected_row
        self._suppress_row_highlight = True
        # Clear existing rows (headers remain)
        self.table.clear()
        # Build style map for state coloring
        style_map = {
            State.PENDING: "grey50",
            State.RUNNING: "yellow",
            State.READY: "green",
            State.FAILED: "red",
        }
        for idx, name in enumerate(self.tasks):
            if idx == 0:
                # Meta task row
                self.table.add_row(str(idx), name, "", "", "")
            else:
                tr = self.backend.rt[name]
                # Color the task name cell
                task_cell = Text(name, style=tr.colour)
                # Determine icon: checkmark if completed tasks, else state symbol
                if tr.state is State.READY and tr.end_time > 0:
                    icon = "✓"
                else:
                    icon = tr.state.value
                state_cell = Text(icon, style=style_map.get(tr.state, ""))
                deps = ", ".join(tr.cfg.depends_on)
                if tr.start_time and tr.end_time:
                    duration = format_duration(tr.end_time - tr.start_time)
                else:
                    duration = ""
                self.table.add_row(str(idx), task_cell, state_cell, deps, duration)
        # Restore cursor to previous row, if valid
        if 0 <= prev_row < len(self.tasks):
            try:
                self.table.cursor_coordinate = (prev_row, 0)
            except Exception:
                pass
        # Re-enable highlight handling
        self._suppress_row_highlight = False

    def action_quit(self) -> None:
        """
        Quits the application.

        This method is called when the user presses the quit key binding. It
        gracefully shuts down the application by terminating all running
        subprocesses and cancelling the background tasks.
        """
        # Kill all subprocess groups
        for tr in self.backend.rt.values():
            if tr.proc and tr.proc.pid:
                try:
                    os.killpg(tr.proc.pid, signal.SIGTERM)
                except Exception:
                    pass
        # Force kill remaining subprocess groups
        for tr in self.backend.rt.values():
            if tr.proc and tr.proc.pid:
                try:
                    os.killpg(tr.proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        # Cancel background tasks
        if self._log_watcher_task:
            self._log_watcher_task.cancel()
        if self._backend_task:
            self._backend_task.cancel()
        # Close any open PTY masters
        for tr in self.backend.rt.values():
            fd = getattr(tr, "pty_primary", -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except Exception:
                    pass
                tr.pty_primary = -1
        # Exit the app immediately
        self.exit()


    def watch_filter_task(self, old: Optional[str], new: Optional[str]) -> None:
        """
        Called when the `filter_task` reactive attribute changes.

        This method re-renders the log view to show only the logs for the newly
        selected task. In single-task mode, it displays a colored header with the
        task name. It also shows or hides the stdin input container depending
        on whether the selected task is running.

        Args:
            old: The previous value of `filter_task`.
            new: The new value of `filter_task`.
        """
        # Show/hide stdin container
        should_show = False
        if new is not None and new != "All":
            task_rt = self.backend.rt.get(new)
            # Task must be running (process exists and has not exited) to receive stdin
            if task_rt and task_rt.proc and task_rt.proc.returncode is None:
                should_show = True

        self.stdin_container.display = should_show

        # Clear view and render header if in single-task mode
        self.log_view.clear()
        if new is not None and new != "All":
            # Show task title as header in its color
            colour = self.backend.rt.get(new).colour if self.backend.rt.get(new) else ""
            header = Text(f"=== {new} ===", style=f"bold {colour}")
            self.log_view.write(header)
            # Blank line after header
            self.log_view.write(Text(""))
        # Render all matching logs
        filtered = [ln for ln in self.all_logs if new is None or ln.startswith(f"[{new}]")]
        for ln in filtered:
            end = ln.find("]")
            if end != -1:
                label = ln[1:end]
                rest = ln[end+1:]
                if new is None:
                    # All tasks: show label padded and colored
                    padded = label.ljust(self.max_label_len)
                    full = f"[{padded}]{rest}"
                    txt = Text(full)
                    if label in self.backend.rt:
                        txt.stylize(self.backend.rt[label].colour, 0, len(padded) + 2)
                else:
                    # Single-task: only content, no label prefix
                    txt = Text(rest)
            else:
                txt = Text(ln)
            self.log_view.write(txt)
        # (Removed unconditional auto-scroll here to respect manual scrolling)



    async def on_button_pressed(self, message: Button.Pressed) -> None:
        """
        Handles button presses.

        This method is called when the user clicks the "Send" button to send
        input to a task.

        Args:
            message: The button press event.
        """
        if message.button == self.stdin_button:
            await self._send_stdin()

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        """
        Handles input submissions.

        This method is called when the user presses Enter in the stdin input
        field.

        Args:
            message: The input submission event.
        """
        if message.input == self.stdin_input:
            await self._send_stdin()

    async def _send_stdin(self) -> None:
        """
        Sends the content of the stdin input to the selected task.

        This method reads the content of the stdin input field and writes it to
        the standard input of the currently selected task.
        """
        content = self.stdin_input.value
        if not content:
            return

        task_name = self.filter_task
        if task_name and task_name != "All":
            task_rt = self.backend.rt.get(task_name)
            if task_rt and task_rt.proc and task_rt.pty_primary >= 0:
                # Add a newline to simulate pressing enter
                data_to_send = (content + "\n").encode()
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, os.write, task_rt.pty_primary, data_to_send)
                    self.stdin_input.value = "" # Clear input after sending
                    self.stdin_input.focus() # Refocus input
                except Exception as e:
                    await self.backend.log_queue.put(f"[App] Error writing to {task_name}: {e}")


    async def on_data_table_row_highlighted(self, message: DataTable.RowHighlighted) -> None:
        """
        Handles row highlighting in the data table.

        This method is called when the user highlights a row in the task table.
        It updates the `filter_task` reactive attribute to filter the log view
        by the selected task.

        Args:
            message: The row highlight event.
        """
        # Handle user-driven row-highlight events
        row = message.cursor_row
        # ignore updates during table refresh
        if self._suppress_row_highlight:
            return
        # ignore repeated highlight events without row change
        if row == self.selected_row:
            return
        # genuine selection: update filter based on selected row (0 = all)
        self.selected_row = row
        if row == 0:
            self.filter_task = None
        elif 0 < row < len(self.tasks):
            self.filter_task = self.tasks[row]
        else:
            self.filter_task = None


def _generate_sample_toml() -> str:
    """
    Generates a sample orchestrate.toml file with extensive comments explaining
    all the configuration options.

    Returns:
        A string containing the sample TOML configuration.
    """
    return """
###############################################################################
# orchestrate.toml — Configuration for the orchestrator
#
# This file defines the tasks that the orchestrator will run.
# Each task is defined in a [[task]] table.
#
# Works on any vanilla macOS/Linux box: only needs /bin/bash, Python ≥3.7
# (for the built-in HTTP server), coreutils (`sleep`, `tail`), and `nc`
# (netcat) for the simple port-readiness probe.
###############################################################################

[defaults]
# Where commands run when a task omits `workdir`.
# Defaults to the current directory.
workdir = "."
# Optional: a command to run before each task's `cmd`.
# If set, each task's cmd will be prefixed with this command using `&&`.
cmd_prefix = ""

###############################################################################
# Tasks are defined as a list of tables.
# Each task has a name, a kind, a command to run, and optional dependencies.
###############################################################################

# A "oneshot" task runs to completion and then exits.
# Downstream tasks can start after it finishes.
[[task]]
name = "setup"
kind = "oneshot"
cmd  = "sleep 1 && echo '[setup] done'"

# A "service" task is a long-running process that is expected to stay alive.
# It becomes "READY" when a readiness probe succeeds.
[[task]]
name       = "web"
kind       = "service"
cmd        = "python -m http.server 9781"
# This task depends on the "setup" aask. It will not start until "setup" is ready.
depends_on = ["setup"]
# The working directory for this task. Overrides the default.
workdir    = "."
# A command to run to check if the service is ready.
# The service is considered ready when this command exits with a status of 0.
ready_cmd  = "bash -c 'nc -z localhost 9781'"

# Another "oneshot" task that waits for the web service to be ready.
[[task]]
name       = "tests"
kind       = "oneshot"
cmd        = "sleep 2 && echo '[tests] integration suite okay'"
depends_on = ["web"]

# A "daemon" task is a long-running process that is not expected to exit.
# It is considered ready as soon as it starts.
[[task]]
name       = "watcher"
kind       = "daemon"
cmd        = "tail -f /dev/null"
depends_on = ["web"]
"""


def main() -> None:
    """
    Main entry point for the application.

    Parses command-line arguments and either prints a sample configuration
    or starts the Textual application.
    """
    if "--sample-toml" in sys.argv:
        print(_generate_sample_toml())
        return

    config = Path("orchestrate.toml")
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        config = Path(sys.argv[1])
    OrchApp(config).run()
    # Reset terminal ANSI formatting on exit to clear any stray styles
    print("\x1b[0m", end="")


if __name__ == "__main__":
    main()