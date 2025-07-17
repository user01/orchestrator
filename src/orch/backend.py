#!/usr/bin/env python3
"""
Backend orchestrator: spawn tasks as PTY-backed subprocesses, manage dependencies,
emit logs and state changes via an asyncio.Queue.
"""
from __future__ import annotations
import asyncio
import dataclasses as dc
import enum
import os
import signal
import time
import pty
from pathlib import Path
from collections import deque

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # fallback

# ─────────── constants & palette ────────────
READY_TIMEOUT = 30
# Default maximum log lines to retain if not specified in config
DEFAULT_MAX_LINES = 2000
TAB10 = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]

def _preexec() -> None:
    """Ensure each subprocess dies with the parent."""
    try:
        os.setsid()
    except Exception:
        pass
    # use prctl to send SIGTERM on parent death (Linux only)
    try:
        import ctypes
        _libc = ctypes.CDLL("libc.so.6")
        _PR_SET_PDEATHSIG = 1
        try:
            _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
        except Exception:
            pass
    except Exception:
        pass

def format_duration(seconds: float) -> str:
    if seconds >= 3600:
        v = seconds / 3600
        unit = "h"
    elif seconds >= 60:
        v = seconds / 60
        unit = "m"
    else:
        v = seconds
        unit = "s"
    if abs(v - int(v)) < 0.05:
        return f"{int(v)}{unit}"
    return f"{v:.1f}{unit}"

class Kind(str, enum.Enum):
    ONESHOT = "oneshot"
    SERVICE = "service"
    DAEMON = "daemon"

class State(str, enum.Enum):
    PENDING = "□"
    RUNNING = "●"
    READY = "■"
    FAILED = "✖"

@dc.dataclass(slots=True)
class TaskCfg:
    name: str
    kind: Kind
    cmd: str
    depends_on: list[str]
    ready_cmd: str | None
    workdir: Path
    # Timeout for readiness probe or execution, in seconds
    ready_timeout: float
    # Maximum log lines to retain for this task
    max_lines: int
    # Timeout for readiness probe or task execution, in seconds
    ready_timeout: float
    # Maximum log lines to retain for this task
    max_lines: int

@dc.dataclass(slots=True)
class TaskRt:
    cfg: TaskCfg
    state: State = State.PENDING
    ready: asyncio.Event = dc.field(default_factory=asyncio.Event)
    proc: asyncio.subprocess.Process | None = None
    colour: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    pty_primary: int = -1
    pty_secondary: int = -1

def load_config(path: Path) -> tuple[dict[str, TaskCfg], int]:
    raw = tomllib.loads(path.read_text())
    defaults = raw.get("defaults", {})
    # Optional command prefix: run before each task's cmd via &&
    prefix_cmd = defaults.get("cmd_prefix", "")
    def_wd = Path(defaults.get("workdir", ".")).expanduser().resolve()
    # Default readiness timeout and max log lines
    def_ready_timeout = float(defaults.get("ready_timeout", READY_TIMEOUT))
    def_max_lines = int(defaults.get("max_lines", DEFAULT_MAX_LINES))
    out: dict[str, TaskCfg] = {}
    for row in raw.get("task", []):
        cmd = row.get("cmd", "")
        if prefix_cmd:
            cmd = f"{prefix_cmd} && {cmd}"
        task_ready_timeout = float(row.get("ready_timeout", def_ready_timeout))
        task_max_lines = int(row.get("max_lines", def_max_lines))
        out[row.get("name", "")] = TaskCfg(
            name=row.get("name", ""),
            kind=Kind(row.get("kind", "oneshot")),
            cmd=cmd,
            depends_on=row.get("depends_on", []),
            ready_cmd=row.get("ready_cmd"),
            workdir=Path(row.get("workdir", def_wd)).expanduser().resolve(),
            ready_timeout=task_ready_timeout,
            max_lines=task_max_lines,
        )
    return out, def_max_lines

def topo_order(cfgs: dict[str, TaskCfg]) -> list[str]:
    indeg = {n: len(c.depends_on) for n, c in cfgs.items()}
    child: dict[str, list[str]] = {}
    for c in cfgs.values():
        for d in c.depends_on:
            child.setdefault(d, []).append(c.name)
    q = deque(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while q:
        n = q.popleft()
        order.append(n)
        for ch in child.get(n, []):
            indeg[ch] -= 1
            if indeg[ch] == 0:
                q.append(ch)
    if len(order) != len(cfgs):
        raise ValueError("dependency cycle")
    return order

class OrchestratorBackend:
    """Core orchestrator without UI dependencies."""
    def __init__(self, cfgs: dict[str, TaskCfg]) -> None:
        self.rt: dict[str, TaskRt] = {}
        for idx, (n, cfg) in enumerate(cfgs.items()):
            tr = TaskRt(cfg=cfg)
            tr.colour = TAB10[idx % len(TAB10)]
            self.rt[n] = tr
        self.log_queue: asyncio.Queue[str] = asyncio.Queue()

    async def run(self) -> None:
        order = topo_order({n: tr.cfg for n, tr in self.rt.items()})
        runners = [asyncio.create_task(self._run_task(self.rt[n])) for n in order]
        await asyncio.gather(*runners)

    async def _run_task(self, tr: TaskRt) -> None:
        # wait for dependencies
        await asyncio.gather(*(self.rt[d].ready.wait() for d in tr.cfg.depends_on))
        tr.state = State.RUNNING
        tr.start_time = time.monotonic()
        await self.log_queue.put(f"[{tr.cfg.name}] started")
        primary_fd, secondary_fd = pty.openpty()
        tr.pty_primary = primary_fd
        tr.pty_secondary = secondary_fd
        tr.proc = await asyncio.create_subprocess_shell(
            tr.cfg.cmd,
            cwd=tr.cfg.workdir,
            stdin=secondary_fd,
            stdout=secondary_fd,
            stderr=secondary_fd,
            executable="/bin/bash",
            preexec_fn=_preexec,
        )
        try:
            os.close(secondary_fd)
        except Exception:
            pass

        async def pump() -> None:
            loop = asyncio.get_running_loop()
            while True:
                try:
                    chunk = await loop.run_in_executor(None, os.read, primary_fd, 1024)
                except Exception:
                    break
                if not chunk:
                    break
                text = chunk.decode(errors="replace")
                for line in text.splitlines():
                    await self.log_queue.put(f"[{tr.cfg.name}] │ {line}")
        asyncio.create_task(pump())

        # handle ready detection
        if tr.cfg.kind is Kind.SERVICE and tr.cfg.ready_cmd:
            try:
                await asyncio.wait_for(self._probe_ready(tr), READY_TIMEOUT)
            except asyncio.TimeoutError:
                tr.state = State.FAILED
                await self.log_queue.put(f"[{tr.cfg.name}] READY TIMEOUT")
        elif tr.cfg.kind in (Kind.SERVICE, Kind.DAEMON):
            tr.state = State.READY
            tr.ready.set()

        # wait for process completion
        if tr.cfg.kind is Kind.ONESHOT:
            code = await tr.proc.wait()
            tr.end_time = time.monotonic()
            tr.state = State.READY if code == 0 else State.FAILED
            tr.ready.set()
        else:
            code = await tr.proc.wait()
            tr.end_time = time.monotonic()
            if code != 0:
                tr.state = State.FAILED

    async def _probe_ready(self, tr: TaskRt) -> None:
        while True:
            proc = await asyncio.create_subprocess_shell(
                tr.cfg.ready_cmd,
                cwd=tr.cfg.workdir,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                executable="/bin/bash",
                preexec_fn=_preexec,
            )
            if await proc.wait() == 0:
                tr.state = State.READY
                tr.ready.set()
                await self.log_queue.put(f"[{tr.cfg.name}] ready")
                return
            await asyncio.sleep(0.5)