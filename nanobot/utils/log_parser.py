"""
Parses Slurm output logs to identify job outcome and extract structured error info.
Supports Python exceptions, CUDA OOM, missing dependencies, file errors, timeouts, etc.
"""

import re
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)


# ── Error types ───────────────────────────────────────────────

class ErrorType(Enum):
    SUCCESS          = auto()   # job completed successfully
    PYTHON_EXCEPTION = auto()   # generic Python exception
    CUDA_OOM         = auto()   # GPU out of memory
    MISSING_DEP      = auto()   # missing package or import
    FILE_NOT_FOUND   = auto()   # missing file or directory
    TIMEOUT          = auto()   # job killed by Slurm time limit
    SEGFAULT         = auto()   # segmentation fault (C extension crash)
    NODE_FAILURE     = auto()   # hardware node failure (not auto-fixable)
    UNKNOWN          = auto()   # unrecognised / job still running

    @property
    def is_auto_fixable(self) -> bool:
        """Whether the error is a candidate for automatic code fix."""
        return self in {
            ErrorType.PYTHON_EXCEPTION,
            ErrorType.CUDA_OOM,
            ErrorType.MISSING_DEP,
            ErrorType.FILE_NOT_FOUND,
        }


# ── Data structures ───────────────────────────────────────────

@dataclass
class TracebackFrame:
    """A single frame extracted from a Python traceback."""
    file_path:     str
    line_number:   int
    function_name: str
    code_snippet:  str


@dataclass
class ParsedError:
    """Structured error report produced by SlurmLogParser."""
    job_id:           str | None
    log_path:         str
    error_type:       ErrorType
    error_message:    str                              # one-line summary
    full_traceback:   str                              # raw traceback text
    traceback_frames: list[TracebackFrame] = field(default_factory=list)
    error_file:       str | None = None                # most likely file to fix
    error_line:       int | None = None                # line number in that file
    suggestion:       str = ""                         # hint passed to the LLM fixer
    raw_tail:         str = ""                         # last N lines of the log (for debug)

    @property
    def is_success(self) -> bool:
        return self.error_type == ErrorType.SUCCESS

    @property
    def is_auto_fixable(self) -> bool:
        return self.error_type.is_auto_fixable

    def summary(self) -> str:
        """One-line human-readable summary."""
        if self.is_success:
            return f"✅ Job {self.job_id} completed successfully"
        return (
            f"❌ Job {self.job_id} failed | "
            f"{self.error_type.name} | "
            f"{'auto-fixable' if self.is_auto_fixable else 'manual intervention'} | "
            f"{self.error_message[:120]}"
        )


# ── Detection patterns ────────────────────────────────────────

# Each entry: (ErrorType, [regex patterns])
# Evaluated in order; first match wins.
_ERROR_PATTERNS: list[tuple[ErrorType, list[str]]] = [
    (ErrorType.CUDA_OOM, [
        r"CUDA out of memory",
        r"RuntimeError: CUDA error: out of memory",
        r"torch\.cuda\.OutOfMemoryError",
        r"out of memory on device",
    ]),
    (ErrorType.MISSING_DEP, [
        r"ModuleNotFoundError: No module named",
        r"ImportError: cannot import name",
        r"ImportError: No module named",
        r"cannot import name '(.+)' from '(.+)'",
    ]),
    (ErrorType.FILE_NOT_FOUND, [
        r"FileNotFoundError",
        r"No such file or directory",
        r"OSError: \[Errno 2\]",
    ]),
    (ErrorType.TIMEOUT, [
        r"DUE TO TIME LIMIT",
        r"CANCELLED AT .+ DUE TO TIME LIMIT",
        r"slurmstepd: error:.+CANCELLED",
    ]),
    (ErrorType.SEGFAULT, [
        r"Segmentation fault",
        r"signal 11 \(SIGSEGV\)",
        r"signal 6 \(SIGABRT\)",
    ]),
    (ErrorType.NODE_FAILURE, [
        r"Node failure",
        r"hardware error",
        r"ECC memory error",
        r"FAILED .+ NODE_FAIL",
    ]),
    (ErrorType.PYTHON_EXCEPTION, [
        r"Traceback \(most recent call last\)",
        r"^\w+Error:",
        r"^\w+Exception:",
    ]),
]

_SUCCESS_PATTERNS: list[str] = [
    r"Job completed successfully",
    r"Training complete",
    r"Finished training",
    r"All done",
    r"srun: Job step .+ completed",
]

_TRACEBACK_FRAME_RE = re.compile(
    r'^\s+File "(.+?)", line (\d+), in (.+)\s*$'
)
_EXCEPTION_LAST_LINE_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_.]*(?:Error|Exception|Warning|Fault|Interrupt)): (.+)$"
)

_SKIP_PATHS = [
    "/site-packages/",
    "/dist-packages/",
    "/lib/python",
    "<frozen ",
    "<string>",
]

_SUGGESTIONS: dict[ErrorType, str] = {
    ErrorType.CUDA_OOM: (
        "GPU out of memory. Try one of the following (in order of preference):\n"
        "1. Reduce batch_size (typically halve it)\n"
        "2. Enable gradient accumulation (gradient_accumulation_steps)\n"
        "3. Use mixed-precision training (torch.cuda.amp)\n"
        "4. Reduce model depth or hidden dimensions"
    ),
    ErrorType.MISSING_DEP: (
        "Missing dependency. Add a pip install step to the sbatch script, "
        "or update the import to use an already-installed alternative."
    ),
    ErrorType.FILE_NOT_FOUND: (
        "File or directory not found. Check:\n"
        "1. Dataset path is correct on the server (use absolute paths)\n"
        "2. Output directory exists (add os.makedirs if needed)\n"
        "3. No local paths hardcoded that don't exist on the server"
    ),
    ErrorType.PYTHON_EXCEPTION: (
        "Locate the root cause from the traceback and make a minimal fix. "
        "Only modify the failing code; leave unrelated logic untouched."
    ),
}


# ── Parser ────────────────────────────────────────────────────

class SlurmLogParser:
    """
    Parses a Slurm output file and returns a ParsedError.

    Usage:
        parser = SlurmLogParser()
        result = parser.parse(log_content, log_path="slurm-12345.out", job_id="12345")
    """

    def __init__(self, tail_lines: int = 120):
        """
        Args:
            tail_lines: number of trailing lines to analyse
                        (most errors appear near the end)
        """
        self._tail_lines = tail_lines

    def parse(
        self,
        log_content: str,
        log_path: str = "",
        job_id: str | None = None,
    ) -> ParsedError:
        lines = log_content.splitlines()
        tail  = lines[-self._tail_lines:] if len(lines) > self._tail_lines else lines
        tail_text = "\n".join(tail)

        # Error patterns take priority over success markers to avoid false positives
        # (e.g. a progress bar showing 100% before a crash)
        error_type = self._detect_error_type(tail_text)

        if error_type == ErrorType.UNKNOWN and self._is_success(tail_text):
            return ParsedError(
                job_id=job_id,
                log_path=log_path,
                error_type=ErrorType.SUCCESS,
                error_message="Job completed successfully",
                full_traceback="",
                raw_tail=tail_text,
            )

        traceback_text, frames = self._extract_traceback(tail_text)
        error_message           = self._extract_error_message(tail_text, traceback_text)
        error_file, error_line  = self._locate_error_source(frames)
        suggestion              = _SUGGESTIONS.get(error_type, "")

        result = ParsedError(
            job_id=job_id,
            log_path=log_path,
            error_type=error_type,
            error_message=error_message,
            full_traceback=traceback_text,
            traceback_frames=frames,
            error_file=error_file,
            error_line=error_line,
            suggestion=suggestion,
            raw_tail=tail_text,
        )
        logger.info("Parsed log: %s", result.summary())
        return result

    # ── Internal helpers ──────────────────────────────────────

    def _is_success(self, text: str) -> bool:
        return any(
            re.search(p, text, re.IGNORECASE | re.MULTILINE)
            for p in _SUCCESS_PATTERNS
        )

    def _detect_error_type(self, text: str) -> ErrorType:
        for error_type, patterns in _ERROR_PATTERNS:
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                    logger.debug("Matched %s via pattern: %s", error_type.name, pattern)
                    return error_type
        return ErrorType.UNKNOWN

    def _extract_traceback(self, text: str) -> tuple[str, list[TracebackFrame]]:
        """Extract the last complete Python traceback block."""
        start = text.rfind("Traceback (most recent call last):")
        if start == -1:
            return "", []

        tb_text = text[start:]
        frames: list[TracebackFrame] = []
        lines = tb_text.splitlines()
        i = 0
        while i < len(lines):
            m = _TRACEBACK_FRAME_RE.match(lines[i])
            if m:
                snippet = lines[i + 1].strip() if i + 1 < len(lines) else ""
                frames.append(TracebackFrame(
                    file_path=m.group(1),
                    line_number=int(m.group(2)),
                    function_name=m.group(3),
                    code_snippet=snippet,
                ))
                i += 2
                continue
            i += 1

        return tb_text, frames

    def _extract_error_message(self, text: str, traceback_text: str) -> str:
        """Extract a one-line error summary from the traceback or tail."""
        search = traceback_text if traceback_text else text
        for line in reversed(search.splitlines()):
            line = line.strip()
            if not line:
                continue
            if _EXCEPTION_LAST_LINE_RE.match(line):
                return line
            if "CUDA out of memory" in line or "OutOfMemoryError" in line:
                return line[:200]
            if "DUE TO TIME LIMIT" in line or "CANCELLED" in line:
                return line.strip()
        for line in reversed(text.splitlines()):
            if line.strip():
                return line.strip()[:200]
        return "Unknown error"

    def _locate_error_source(
        self, frames: list[TracebackFrame]
    ) -> tuple[str | None, int | None]:
        """
        Find the most likely user-code file to fix.
        Skips stdlib and third-party packages; falls back to the last frame.
        """
        for frame in reversed(frames):
            if not any(p in frame.file_path for p in _SKIP_PATHS):
                return frame.file_path, frame.line_number
        if frames:
            return frames[-1].file_path, frames[-1].line_number
        return None, None


# ── Convenience functions ─────────────────────────────────────

def parse_slurm_log(
    log_content: str,
    log_path: str = "",
    job_id: str | None = None,
) -> ParsedError:
    """Module-level shortcut for SlurmLogParser().parse(...)."""
    return SlurmLogParser().parse(log_content, log_path=log_path, job_id=job_id)


def extract_job_id_from_path(log_path: str) -> str | None:
    """Extract job ID from a filename like slurm-12345.out."""
    m = re.search(r"(\d+)", PurePosixPath(log_path).name)
    return m.group(1) if m else None