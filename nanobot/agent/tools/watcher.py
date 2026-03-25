"""
Log watcher: polls remote directories via ServerChannel and dispatches on file changes.

Responsibilities:
  - Poll remote dirs at a fixed interval (via channel.listdir)
  - Detect new or grown files
  - Apply a filter function to decide which files are worth processing
  - Read matched files (via channel.read_file) and pass content to a parser
  - Route parsed results: UNKNOWN → silent, SUCCESS → on_complete, error → on_error

The watcher holds no SSH connection itself; all IO goes through ServerChannel.
"""

import asyncio
import logging
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from nanobot.agent.task_registry import TaskRegistry
from nanobot.agent.tools.base import Tool
from nanobot.utils.log_parser import parse_slurm_log, ParsedError, ErrorType

from loguru import logger


ErrorCallback    = Callable[[ParsedError], Awaitable[None]]
CompleteCallback = Callable[[], Awaitable[None]]
FileFilter       = Callable[[str], bool]   # filename → should process?


@dataclass
class _FileEntry:
    filename: str
    size:     int
    mtime:    float


@dataclass
class WatchEvent:
    full_path:   str
    filename:    str
    watched_dir: str
    file_size:   int
    timestamp:   datetime = field(default_factory=datetime.now)



def slurm_log_filter(filename: str) -> bool:
    """Accept slurm output files: slurm-<id>.out or slurm_<id>.log."""
    import re
    return bool(
        re.match(r"slurm-\d+\.out$", filename) or
        re.match(r"slurm_\d+\.log$", filename)
    )


class LogWatcher:
    """
    Polls remote directories for file changes and dispatches to callbacks.

    Args:
        channel         ServerChannel instance for all remote IO
        watch_dirs      list of remote directories to monitor
        file_filter     callable(filename) → bool, decides which files to process
                        defaults to slurm_log_filter
        on_error        async callback invoked on job failure, receives ParsedError
        on_complete     async callback invoked on job success, triggers watcher shutdown
        poll_interval   seconds between polls (default 30)
        stable_wait     seconds to wait after detecting a change before reading
                        (allows file write to complete, default 5)
        min_file_size   ignore files smaller than this (default 1, filters empty files)

    Three-way dispatch:
        UNKNOWN  → job still running, silent, continue polling
        SUCCESS  → call on_complete(), return (stops loop)
        other    → call on_error(parsed), return (stops loop)
    """

    TASK_NAME = "server_watcher"

    def __init__(
        self,
        channel,
        watch_dirs: list[str],
        *,
        file_filter:   FileFilter       | None = None,
        on_error:      ErrorCallback    | None = None,
        on_complete:   CompleteCallback | None = None,
        poll_interval: float = 30.0,
        stable_wait:   float = 5.0,
        min_file_size: int   = 1,
    ):
        self._channel       = channel
        self._watch_dirs    = watch_dirs
        self._file_filter   = file_filter or slurm_log_filter
        self._on_error      = on_error
        self._on_complete   = on_complete
        self._poll_interval = poll_interval
        self._stable_wait   = stable_wait
        self._min_file_size = min_file_size
        self._snapshots: dict[str, dict[str, _FileEntry]] = {}

    # ── Snapshot init ─────────────────────────────────────────

    def _init_snapshots(self) -> None:
        """Record existing files so they are not treated as new on first poll."""
        for d in self._watch_dirs:
            entries = self._channel.listdir_raw(d)
            self._snapshots[d] = {
                name: _FileEntry(filename=name, size=e.size, mtime=e.mtime)
                for name, e in entries.items()
            }
            logger.info("Snapshot: %s (%d files)", d, len(entries))

    # ── Poll ──────────────────────────────────────────────────

    def _poll_once(self) -> list[tuple[WatchEvent, str]]:
        """
        Scan all watched dirs.
        Returns list of (WatchEvent, file_content) for files that:
          - are new or have grown since the last snapshot
          - pass the file_filter
        """
        results: list[tuple[WatchEvent, str]] = []

        for watch_dir in self._watch_dirs:
            try:
                current_entries = self._channel.listdir_raw(watch_dir)
            except Exception as e:
                logger.error("Failed to list %s: %s", watch_dir, e)
                continue

            prev = self._snapshots.get(watch_dir, {})
            current = {
                name: _FileEntry(filename=name, size=e.size, mtime=e.mtime)
                for name, e in current_entries.items()
            }

            # Detect new files and files that have grown
            changed = {
                name: entry for name, entry in current.items()
                if entry.size >= self._min_file_size and (
                    name not in prev or entry.size > prev[name].size
                )
            }

            self._snapshots[watch_dir] = current

            for name, entry in changed.items():
                # Apply the pluggable file filter
                if not self._file_filter(name):
                    continue

                full_path = str(PurePosixPath(watch_dir) / name)
                logger.info("File changed: %s", full_path)

                try:
                    content = self._channel.read_file(full_path)
                except Exception as e:
                    logger.error("Failed to read %s: %s", full_path, e)
                    continue

                results.append((
                    WatchEvent(
                        full_path=full_path,
                        filename=name,
                        watched_dir=watch_dir,
                        file_size=entry.size,
                    ),
                    content,
                ))

        return results

    # ── Dispatch ──────────────────────────────────────────────

    async def _process(self, event: WatchEvent, content: str) -> bool:
        """
        Parse file content and dispatch:
          UNKNOWN → silent, return False (keep polling)
          SUCCESS → on_complete(), return True (stop)
          other   → on_error(parsed), return True (stop)
        """
        import re
        job_id_match = re.search(r"(\d+)", event.filename)
        job_id = job_id_match.group(1) if job_id_match else None

        parsed = parse_slurm_log(
            log_content=content,
            log_path=event.full_path,
            job_id=job_id,
        )

        # Running 
        if parsed.error_type == ErrorType.UNKNOWN:
            logger.debug("Job %s still running, silent", parsed.job_id)
            return False

        # Success
        elif parsed.error_type == ErrorType.SUCCESS:
            logger.info("Job %s completed, stopping watcher", parsed.job_id)
            return True

        # Error
        else:
            logger.warning("Job %s failed: %s", parsed.job_id, parsed.error_type.name)
            if self._on_error:
                await self._on_error(parsed)
            return True

    # ── Main loop ─────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the polling loop as a background coroutine.
        Stopped by task.cancel() which raises CancelledError at asyncio.sleep().
        """
        try:
            self._channel.ensure_connected()
            self._init_snapshots()
            logger.info(
                "Watcher started | interval=%.0fs | dirs=%s",
                self._poll_interval, self._watch_dirs,
            )

            while True:
                await asyncio.sleep(self._poll_interval)

                try:
                    self._channel.ensure_connected()
                    changed_files = self._poll_once()
                except Exception as e:
                    logger.error("Poll error: %s", e)
                    continue

                if not changed_files:
                    logger.debug("No changes this poll")
                    continue

                if self._stable_wait > 0:
                    await asyncio.sleep(self._stable_wait)

                for event, content in changed_files:
                    should_stop = await self._process(event, content)
                    if should_stop:
                        logger.info("Job finished, exiting poll loop")
                        await self._on_complete()
                        return

        except asyncio.CancelledError:
            logger.info("Watcher cancelled")
            raise

        except Exception as e:
            logger.error("Watcher crashed: %s", e)
            raise
        
        
        
"""
Server watcher tools: start, stop, and query the background log watcher.

Three tools are exposed to the LLM:
  server_start_watcher   start monitoring remote dirs for job log changes
  server_stop_watcher    stop the watcher
  server_watcher_status  query current watcher state

Use make_watcher_tools() to create all three in one call.
The file_filter parameter allows callers to plug in custom filtering logic,
keeping the monitor (polling) and filter (selection) concerns separate.
"""


# ── Tool 1: Start watcher ─────────────────────────────────────

class ServerStartWatcherTool(Tool):
    """Start the server log watcher as a background task."""

    def __init__(
        self,
        task_registry: TaskRegistry,
        channel,
        watch_dirs: list[str],
        *,
        file_filter:    FileFilter | None = None,
        poll_interval:  float = 30.0,
        stable_wait:    float = 5.0,
        on_watcher_exit = None,
    ):
        self._registry        = task_registry
        self._channel         = channel
        self._watch_dirs      = watch_dirs
        self._file_filter     = file_filter
        self._poll_interval   = poll_interval
        self._stable_wait     = stable_wait
        self._on_watcher_exit = on_watcher_exit

    @property
    def name(self) -> str:
        return "server_start_watcher"

    @property
    def description(self) -> str:
        return (
            "Start monitoring remote server directories for job log changes. "
            "Runs in the background: silent on success, reports errors automatically. "
            "No-op if already running."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "poll_interval": {
                    "type": "integer",
                    "description": "Polling interval in seconds (default 30, min 10, max 3600)",
                    "minimum": 10,
                    "maximum": 3600,
                    "default": 30,
                },
                "watch_dirs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Override default watch directories (optional)",
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        task_name = LogWatcher.TASK_NAME

        if self._registry.is_running(task_name):
            return "⚠️ Watcher is already running. Stop it first if you want to restart."

        poll_interval = kwargs.get("poll_interval", self._poll_interval)
        watch_dirs    = kwargs.get("watch_dirs", self._watch_dirs)

        registry = self._registry

        async def on_complete() -> None:
            logger.info("Job completed, shutting down watcher")
            await registry.cancel(task_name)

        watcher = LogWatcher(
            channel       = self._channel,
            watch_dirs    = watch_dirs,
            file_filter   = self._file_filter,
            on_error      = self._channel.report_error,
            on_complete   = on_complete,
            poll_interval = poll_interval,
            stable_wait   = self._stable_wait,
        )

        task = asyncio.create_task(watcher.start())
        self._registry.register(
            task_name,
            task,
            on_exit=self._on_watcher_exit,
        )

        await asyncio.sleep(0)  # yield to let watcher initialize

        dirs_str = ", ".join(watch_dirs)
        return (
            f"✅ Server watcher started\n"
            f"- Watching: {dirs_str}\n"
            f"- Poll interval: {poll_interval}s\n"
            f"- Errors will be reported automatically"
        )


# ── Tool 2: Stop watcher ──────────────────────────────────────

class ServerStopWatcherTool(Tool):
    """Stop the server log watcher."""

    def __init__(self, task_registry: TaskRegistry):
        self._registry = task_registry

    @property
    def name(self) -> str:
        return "server_stop_watcher"

    @property
    def description(self) -> str:
        return (
            "Stop the server log watcher. "
            "Running jobs are not affected; monitoring simply stops."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        task_name = LogWatcher.TASK_NAME

        if not self._registry.is_running(task_name):
            status = self._registry.status(task_name)
            if status is None:
                return "ℹ️ Watcher has never been started."
            return f"ℹ️ Watcher is already {status['state']}, nothing to stop."

        await self._registry.cancel(task_name)
        return "🛑 Server watcher stopped."


# ── Tool 3: Status ────────────────────────────────────────────

class ServerWatcherStatusTool(Tool):
    """Query the current state of the server log watcher."""

    def __init__(self, task_registry: TaskRegistry):
        self._registry = task_registry

    @property
    def name(self) -> str:
        return "server_watcher_status"

    @property
    def description(self) -> str:
        return "Check whether the server watcher is running, stopped, or failed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        status = self._registry.status(LogWatcher.TASK_NAME)

        if status is None:
            return "ℹ️ Watcher has never been started this session."

        state   = status["state"]
        started = status["started_at"]
        stopped = status.get("stopped_at") or "—"
        error   = status.get("error") or "none"
        icon    = {"RUNNING": "🟢", "STOPPED": "⚫", "FAILED": "🔴"}.get(state, "❓")

        return (
            f"{icon} Server watcher: {state}\n"
            f"- Started:  {started}\n"
            f"- Stopped:  {stopped}\n"
            f"- Last error: {error}"
        )


# ── Factory ───────────────────────────────────────────────────

def make_watcher_tools(
    task_registry: TaskRegistry,
    channel,
    watch_dirs: list[str],
    *,
    file_filter:   FileFilter | None = None,
    poll_interval: float = 30.0,
    stable_wait:   float = 5.0,
) -> list[Tool]:
    """
    Create all three watcher tools wired to the given channel.

    Args:
        task_registry   TaskRegistry from AgentLoop
        channel         ServerChannel instance (owns SSH connection and report_error)
        watch_dirs      remote directories to monitor
        file_filter     callable(filename) → bool, pluggable file selection logic
                        defaults to slurm_log_filter (matches slurm-*.out / slurm_*.log)
        poll_interval   seconds between directory scans
        stable_wait     seconds to wait after a change before reading the file

    To use a custom filter (e.g. monitor all .log files):
        make_watcher_tools(..., file_filter=lambda name: name.endswith(".log"))
    """
    async def on_watcher_exit(name: str, error: str | None) -> None:
        if error:
            await channel.report_exit(error)

    start_tool = ServerStartWatcherTool(
        task_registry   = task_registry,
        channel         = channel,
        watch_dirs      = watch_dirs,
        file_filter     = file_filter or slurm_log_filter,
        poll_interval   = poll_interval,
        stable_wait     = stable_wait,
        on_watcher_exit = on_watcher_exit,
    )

    return [
        start_tool,
        ServerStopWatcherTool(task_registry),
        ServerWatcherStatusTool(task_registry),
    ]