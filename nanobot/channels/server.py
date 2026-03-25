"""Server channel: SSH-based access to remote servers."""

import logging
import stat
from pathlib import Path, PurePosixPath
from dataclasses import dataclass, asdict

import paramiko

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import ServerConfig

logger = logging.getLogger(__name__)


@dataclass
class RemoteFileEntry:
    """Metadata for a single remote file."""
    filename: str
    size:     int
    mtime:    float

    def to_dict(self) -> dict:
        return asdict(self)


class ServerChannel(BaseChannel):
    """
    Manages SSH connection to a remote server and exposes file operations.
    Acts as the sole interface between the agent and the remote server.

    Lifecycle:
        ChannelManager.start_all() → start()   (non-blocking, validates config)
        ChannelManager.stop_all()  → stop()    (cancels watcher, closes SSH)

    File operations (signature-compatible with LocalContext in filesystem.py):
        list_dir(remote_dir)                    → str  (JSON, LLM-readable)
        read_file(remote_path)                  → str
        write_file(remote_path, content)        → str
        edit_file(remote_path, old, new)        → str
    """

    name = "server"

    def __init__(self, config: ServerConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: ServerConfig = config
        self._ssh: "paramiko.SSHClient | None" = None
        self._task_registry = None

    def set_task_registry(self, registry) -> None:
        """Injected by AgentLoop after creation to avoid circular imports."""
        self._task_registry = registry

    def connect(self) -> None:
        """Establish SSH connection."""
        cfg = self.config
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=cfg.hostname,
            username=cfg.username,
            port=cfg.port,
            key_filename=str(Path(cfg.key_path).expanduser()),
            timeout=30,
        )
        client.get_transport().set_keepalive(60)
        self._ssh = client
        logger.info("SSH connected: %s@%s:%d", cfg.username, cfg.hostname, cfg.port)

    def ensure_connected(self) -> None:
        """Reconnect if the SSH connection is lost."""
        try:
            transport = self._ssh.get_transport() if self._ssh else None
            if transport and transport.is_active():
                return
        except Exception:
            pass
        logger.warning("SSH connection lost, reconnecting...")
        self.connect()

    def disconnect(self) -> None:
        """Close SSH connection."""
        if self._ssh:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None
            logger.info("SSH disconnected")

    # filesystem
    def listdir_raw(self, remote_dir: str) -> dict[str, RemoteFileEntry]:
        """Internal use: returns structured data for programmatic access."""
        sftp = None
        try:
            self.ensure_connected()
            sftp = self._ssh.open_sftp()
            result: dict[str, RemoteFileEntry] = {}
        
            for entry in sftp.listdir_attr(remote_dir):
                if not stat.S_ISREG(entry.st_mode or 0):
                    continue
                result[entry.filename] = RemoteFileEntry(
                    filename=entry.filename,
                    size=entry.st_size or 0,
                    mtime=entry.st_mtime or 0.0,
                )
        except (FileNotFoundError, IOError, OSError) as e:
            raise FileNotFoundError(f"Failed to list remote dir {remote_dir}: {e}")
        finally:
            if sftp:
                sftp.close()
        return result
    
    def list_dir(self, remote_dir: str) -> str:
        """For LLM use: returns JSON string."""
        import json
        self.ensure_connected()
        entries = [e.to_dict() for e in self.listdir_raw(remote_dir).values()]
        return json.dumps(entries, indent=2)

    def read_file(self, remote_path: str) -> str:
        """Read a remote file and return its content as a string."""
        sftp = None
        try:
            self.ensure_connected()
            sftp = self._ssh.open_sftp()
            with sftp.open(remote_path, "r") as f:
                return f.read().decode(errors="replace")
        finally:
            if sftp:
                sftp.close()

    def write_file(self, remote_path: str, content: str) -> str:
        """
        Write content to a remote file, creating parent directories if needed.
        """
        sftp = None
        try:
            self.ensure_connected()
            parent = str(PurePosixPath(remote_path).parent)
            _, stdout, _ = self._ssh.exec_command(f"mkdir -p {parent}")
            stdout.channel.recv_exit_status()
            sftp = self._ssh.open_sftp()
            with sftp.open(remote_path, "w") as f:
                f.write(content.encode())
            return f"Successfully wrote {len(content)} bytes to {remote_path}"
        finally:
            if sftp:
                sftp.close()

    def edit_file(self, remote_path: str, old_text: str, new_text: str) -> str:
        """
        Edit a remote file by replacing old_text with new_text.
        old_text must exist exactly once in the file.
        """
        self.ensure_connected()
        content = self.read_file(remote_path)

        if old_text not in content:
            raise ValueError(
                f"old_text not found in {remote_path}. Make sure it matches exactly."
            )
        count = content.count(old_text)
        if count > 1:
            raise ValueError(
                f"old_text appears {count} times in {remote_path}. "
                "Provide more context to make it unique."
            )

        new_content = content.replace(old_text, new_text, 1)
        self.write_file(remote_path, new_content)
        return f"Successfully edited {remote_path}"
    
    async def start(self) -> None:
        """Validate config and mark channel as running. Watcher is managed by tools."""
        self._running = True
        if not self.config.hostname or not self.config.username:
            logger.warning("ServerChannel: hostname/username not configured")
            return
        if not self.config.notify_chat_id:
            logger.warning("ServerChannel: notify_chat_id not set, error notifications may fail")
        logger.info(
            "ServerChannel started | server: %s@%s | notify: %s",
            self.config.username, self.config.hostname,
            self.config.notify_chat_id or "(not set)",
        )

    async def stop(self) -> None:
        """Stop the watcher task and close SSH connection."""
        self._running = False
        if self._task_registry:
            from nanobot.agent.tools.watcher import LogWatcher
            if self._task_registry.is_running(LogWatcher.TASK_NAME):
                self._task_registry.cancel(LogWatcher.TASK_NAME)
        self.disconnect()
        logger.info("ServerChannel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Not supported: ServerChannel is inbound-only."""
        logger.warning("ServerChannel.send() is not supported, message dropped")

    async def report_error(self, parsed) -> None:
        """Report a job failure to the agent via infobus."""
        fixable = "auto-fixable" if parsed.is_auto_fixable else "manual intervention required"
        content = (
            f"[SERVER_ERROR] Job {parsed.job_id} failed\n"
            f"Error type: {parsed.error_type.name} ({fixable})\n"
            f"Summary: {parsed.error_message}\n"
            f"Location: {parsed.error_file or 'unknown'} line {parsed.error_line or '?'}\n"
            f"Suggestion: {parsed.suggestion or 'none'}"
        )
        await self._handle_message(
            sender_id="server_watcher",
            chat_id=self.config.notify_chat_id or "server",
            content=content,
            metadata={
                "source":       "server_watcher",
                "job_id":       parsed.job_id,
                "error_type":   parsed.error_type.name,
                "error_file":   parsed.error_file,
                "error_line":   parsed.error_line,
                "auto_fixable": parsed.is_auto_fixable,
                "traceback":    parsed.full_traceback,
                "suggestion":   parsed.suggestion,
            },
        )
        logger.info("Job %s error reported to infobus", parsed.job_id)

    async def report_exit(self, error: str) -> None:
        """Report an unexpected watcher exit to the agent via infobus."""
        await self._handle_message(
            sender_id="server_watcher",
            chat_id=self.config.notify_chat_id or "server",
            content=(
                f"[WATCHER_FAILED] Server watcher exited unexpectedly\n"
                f"Error: {error}\n"
                f"Please restart the watcher or check the SSH connection."
            ),
            metadata={"source": "server_watcher", "event": "exit_failed"},
        )
        logger.warning("Watcher exit reported to infobus: %s", error)

    async def report_complete(self, job_id: str) -> None:
        """Called when a job completes successfully. Silent by default."""
        logger.info("Job %s completed successfully", job_id)