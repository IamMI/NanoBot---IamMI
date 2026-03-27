"""
codex.py
--------
Tool for submitting tasks to Codex running on the remote server.

Codex runs asynchronously: run_codex returns immediately after submitting,
while Codex executes in the background. The caller should poll status.txt
inside the returned task_dir to know when the task is done.

Poll workflow:
    1. Call run_codex_on_server(prompts) → get task_dir
    2. Poll {task_dir}/status.txt via read_file(context="server")
       - "status: running" → still in progress
       - "status: done"    → finished, read {task_dir}/codex.out for results
    3. Read {task_dir}/codex.out for the full output
"""

from typing import Any
import asyncio
import os

from nanobot.agent.tools.base import Tool, BackgroundTask


class RunCodexTool(Tool):
    """Submit a task to Codex on the remote server."""

    def __init__(self, channel) -> None:
        """
        Args:
            channel: ServerChannel instance, provides run_codex().
        """
        self._channel = channel

    @property
    def name(self) -> str:
        return "run_codex_on_server"

    @property
    def description(self) -> str:
        return (
            "Submit a coding task to Codex running on the remote server. "
            "Returns immediately with a task_dir path. "
            "Poll {task_dir}/status.txt to check progress: "
            "'status: running' means in progress, 'status: done' means finished. "
            "Read {task_dir}/codex.out for the full output once done."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompts": {
                    "type": "string",
                    "description": (
                        "The task description to submit to Codex. "
                        "Be specific: include file paths, error messages, "
                        "and what the expected fix should be."
                    ),
                },
            },
            "required": ["prompts"],
        }

    async def execute(self, prompts: str, **kwargs: Any) -> str:
        status, task_dir, error = self._channel.run_codex(prompts)

        if status == "__ERROR__":
            return f"Failed to submit task to Codex: {error}"

        async def poll():
            while True:
                await asyncio.sleep(60)
                try:
                    content = self._channel.read_file(f"{task_dir}/status.txt")
                except (PermissionError, FileNotFoundError, ValueError) as e:
                    content = "" 
                
                if "done" in content:
                    try:
                        output = self._channel.read_file(f"{task_dir}/codex.out")
                    except (PermissionError, FileNotFoundError, ValueError) as e:
                        output = "[NO OUTPUT]"
                    await self._channel.report_codex_done(task_dir, output)
                    return
                    
        async def on_exit(name: str, error: str | None) -> None:
            if error:
                await self._channel.report_exit(f"Codex polling task failed: {error}")
        
        return BackgroundTask(
            name      = f"codex_{os.path.basename(task_dir)}",
            coroutine = poll(),
            on_exit   = on_exit,
            message   = (
                f"Task submitted.\n"
                f"- task_dir: {task_dir}\n"
                f"- Polling every 1 minutes automatically. "
                f"  You don't need to manually invoke 'read_file' or 'check_status' for this task."
                f"  I will proactively notify you once the execution finishes or hits an error."
            ),
        )