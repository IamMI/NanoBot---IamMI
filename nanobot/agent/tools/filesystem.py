"""
filesystem.py
-------------
File system tools: read, write, edit, list_dir.

Each tool ships with a built-in LocalContext that handles local file operations.
External channels (e.g. ServerChannel) can be passed in at registration time
to extend the available execution contexts.

Registration example (loop.py):
    extra = {"server": server_channel} if server_channel else {}
    self.tools.register(ReadFileTool(allowed_dir, extra))
    self.tools.register(WriteFileTool(allowed_dir, extra))
    self.tools.register(EditFileTool(allowed_dir, extra))
    self.tools.register(ListDirTool(allowed_dir, extra))
"""

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool



def _resolve_path(path: str, allowed_dir: Path | None = None) -> Path:
    """Resolve path and optionally enforce directory restriction."""
    resolved = Path(path).expanduser().resolve()
    if allowed_dir and not str(resolved).startswith(str(allowed_dir.resolve())):
        raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


class LocalContext:
    """
    Handles all file operations on the local filesystem.
    Built into every file tool as the default 'local' context.
    """

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    def read_file(self, path: str) -> str:
        file_path = _resolve_path(path, self._allowed_dir)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not file_path.is_file():
            raise ValueError(f"Not a file: {path}")
        return file_path.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        file_path = _resolve_path(path, self._allowed_dir)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} bytes to {path}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        file_path = _resolve_path(path, self._allowed_dir)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        content = file_path.read_text(encoding="utf-8")
        if old_text not in content:
            raise ValueError("old_text not found in file. Make sure it matches exactly.")
        count = content.count(old_text)
        if count > 1:
            raise ValueError(
                f"old_text appears {count} times. Provide more context to make it unique."
            )
        file_path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Successfully edited {path}"

    def list_dir(self, path: str) -> str:
        dir_path = _resolve_path(path, self._allowed_dir)
        if not dir_path.exists():
            raise FileNotFoundError(f"Directory not found: {path}")
        if not dir_path.is_dir():
            raise ValueError(f"Not a directory: {path}")
        items = [
            f"{'📁' if item.is_dir() else '📄'} {item.name}"
            for item in sorted(dir_path.iterdir())
        ]
        return "\n".join(items) if items else f"Directory {path} is empty"



class _ContextAwareTool(Tool):
    """
    Shared logic for tools that support multiple execution contexts.

    Subclasses declare which method name they use (e.g. 'read_file'),
    and this base class handles handler resolution and context parameter injection.
    """

    # Override in subclass: name of the method to call on each context/channel
    _method: str = ""

    def __init__(
        self,
        allowed_dir: Path | None,
        extra_channels: dict[str, Any] | None,
    ):
        local = LocalContext(allowed_dir)
        # Always include local as the default context
        self._handlers: dict[str, Any] = {
            "local": getattr(local, self._method)
        }
        for name, channel in (extra_channels or {}).items():
            handler = getattr(channel, self._method, None)
            if handler:
                self._handlers[name] = handler 

    def _context_param(self) -> dict[str, Any] | None:
        """Return the context parameter schema, or None if only local is available."""
        contexts = list(self._handlers.keys())
        if len(contexts) <= 1:
            return None
        return {
            "type": "string",
            "enum": contexts,
            "default": "local",
            "description": f"Where to perform the operation. Available: {contexts}.",
        }

    def _inject_context_param(self, props: dict) -> dict:
        """Add context param to a properties dict if multiple contexts exist."""
        param = self._context_param()
        if param:
            props["context"] = param
        return props

    def _get_handler(self, context: str):
        handler = self._handlers.get(context)
        if handler is None:
            raise ValueError(
                f"Unknown context '{context}'. Available: {list(self._handlers.keys())}"
            )
        return handler

    def _resolve_context(self, kwargs: dict) -> tuple[str, Any]:
        """Extract context from kwargs and return (context_name, handler)."""
        context = kwargs.get("context", "local")
        return context, self._get_handler(context)


class ReadFileTool(_ContextAwareTool):
    """Read the contents of a file."""

    _method = "read_file"

    def __init__(
        self,
        allowed_dir: Path | None = None,
        extra_channels: dict[str, Any] | None = None,
    ):
        super().__init__(allowed_dir, extra_channels)

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file at the given path."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self._inject_context_param({
                "path": {
                    "type": "string",
                    "description": "The file path to read.",
                },
            }),
            "required": ["path"],
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            _, handler = self._resolve_context(kwargs)
            return handler(path)
        except (PermissionError, FileNotFoundError, ValueError) as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"


class WriteFileTool(_ContextAwareTool):
    """Write content to a file, creating parent directories if needed."""

    _method = "write_file"

    def __init__(
        self,
        allowed_dir: Path | None = None,
        extra_channels: dict[str, Any] | None = None,
    ):
        super().__init__(allowed_dir, extra_channels)

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Creates parent directories if needed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self._inject_context_param({
                "path": {
                    "type": "string",
                    "description": "The file path to write to.",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write.",
                },
            }),
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            _, handler = self._resolve_context(kwargs)
            return handler(path, content)
        except (PermissionError, ValueError) as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"


class EditFileTool(_ContextAwareTool):
    """Edit a file by replacing a unique string."""

    _method = "edit_file"

    def __init__(
        self,
        allowed_dir: Path | None = None,
        extra_channels: dict[str, Any] | None = None,
    ):
        super().__init__(allowed_dir, extra_channels)

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Edit a file by replacing old_text with new_text. old_text must exist exactly once."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self._inject_context_param({
                "path": {
                    "type": "string",
                    "description": "The file path to edit.",
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find and replace.",
                },
                "new_text": {
                    "type": "string",
                    "description": "The replacement text.",
                },
            }),
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        try:
            _, handler = self._resolve_context(kwargs)
            return handler(path, old_text, new_text)
        except (PermissionError, FileNotFoundError, ValueError) as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {e}"


class ListDirTool(_ContextAwareTool):
    """List the contents of a directory."""

    _method = "list_dir"

    def __init__(
        self,
        allowed_dir: Path | None = None,
        extra_channels: dict[str, Any] | None = None,
    ):
        super().__init__(allowed_dir, extra_channels)

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self._inject_context_param({
                "path": {
                    "type": "string",
                    "description": "The directory path to list.",
                },
            }),
            "required": ["path"],
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            _, handler = self._resolve_context(kwargs)
            return handler(path)
        except (PermissionError, FileNotFoundError, ValueError) as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {e}"