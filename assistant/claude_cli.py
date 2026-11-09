"""Runs one isolated ``claude -p`` process per reply.

The conversation only ever travels through stdin and the profile only through a
private temporary file, so neither shows up in the process list. A shell is never used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .store import ensure_private_dir

logger = logging.getLogger(__name__)

ISOLATION_SETTINGS: Dict[str, Any] = {
    "disableAllHooks": True,
    "enabledPlugins": {
        "cc-plugin-agents-md@builtin": False,
        "cc-plugin-telemetry@builtin": False,
        "cc-plugin-plugin-authoring@builtin": False,
        "cc-plugin-privacy@builtin": False,
    },
}

_STRIPPED_ENV = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        # Set by --bare; it turns the OAuth login off.
        "CLAUDE_CODE_SIMPLE",
    }
)

_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate[ _-]?limit|out of (extra )?usage|quota",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not logged in|/login|invalid api key|authentication|unauthori[sz]ed|oauth token",
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\|(\d{9,11})\b")
_RESET_TEXT_RE = re.compile(r"resets\s+([^|\n]+)", re.IGNORECASE)

_INIT_FIELDS = ("tools", "mcp_servers", "plugins", "skills", "apiKeySource", "apiProvider", "model")
_KILL_WAIT_SECONDS = 5


class ClaudeError(Exception):
    """Base error. The message is safe to show to the owner."""


class ClaudeTimeout(ClaudeError):
    """The CLI did not finish in time and was killed."""


class ClaudeAuthError(ClaudeError):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class ClaudeLimitError(ClaudeError):
    def __init__(
        self,
        detail: str = "",
        resets_at: Optional[float] = None,
        reset_text: Optional[str] = None,
    ) -> None:
        when = reset_text
        if not when and resets_at:
            when = datetime.fromtimestamp(resets_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        suffix = f" (resets {when})" if when else ""
        super().__init__(f"Claude subscription usage limit reached{suffix}")
        self.detail = detail
        self.resets_at = resets_at
        self.reset_text = reset_text


def classify_failure(message: str, status: Optional[int] = None) -> ClaudeError:
    """Turn CLI failure text into the matching exception (returned, not raised)."""
    if status == 429 or _LIMIT_RE.search(message):
        epoch = _EPOCH_RE.search(message)
        detail = _EPOCH_RE.sub("", message).strip()
        reset = _RESET_TEXT_RE.search(detail)
        return ClaudeLimitError(
            detail=detail,
            resets_at=float(epoch.group(1)) if epoch else None,
            reset_text=reset.group(1).strip().rstrip(".") if reset else None,
        )
    if status in (401, 403) or _AUTH_RE.search(message):
        return ClaudeAuthError(
            "Claude CLI is not logged in. Run `claude` and sign in with /login.",
            detail=message.strip()[:300],
        )
    return ClaudeError(message.strip()[:500] or "Claude CLI failed")


def parse_result(returncode: int, stdout: str, stderr: str) -> str:
    """Return the reply from the last JSON line of the CLI output, or raise."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    payload: Any = None
    if lines:
        try:
            payload = json.loads(lines[-1])
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        raise classify_failure(stderr.strip() or stdout.strip() or f"exit code {returncode}")

    raw_result = payload.get("result")
    result = raw_result if isinstance(raw_result, str) else ""
    if payload.get("is_error") or payload.get("subtype") != "success":
        message = "\n".join(part for part in (result, stderr.strip()) if part)
        raise classify_failure(
            message or str(payload.get("subtype") or f"exit code {returncode}"),
            payload.get("api_error_status"),
        )
    if not result.strip():
        raise ClaudeError("Claude returned an empty reply")
    return result.strip()


def build_env(oauth_token: Optional[str] = None) -> Dict[str, str]:
    """Copy the environment without credentials or settings that would change auth."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ANTHROPIC_") and key not in _STRIPPED_ENV
    }
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def _kill_process_tree(pid: int) -> None:
    if os.name == "posix":
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return
    # Windows (local development only) has no process groups; taskkill walks the tree.
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


class ClaudeCLI:
    def __init__(
        self,
        claude_bin: str,
        *,
        workdir: Union[str, "os.PathLike[str]"],
        model: Optional[str] = None,
        effort: str = "low",
        timeout: float = 120,
        oauth_token: Optional[str] = None,
        auth_label: Optional[str] = None,
    ) -> None:
        self._claude_bin = claude_bin
        self._workdir = Path(workdir)
        self._model = model
        self._effort = effort
        self._timeout = timeout
        self._oauth_token = oauth_token or None
        self._auth_label = auth_label

    def __repr__(self) -> str:
        return (
            f"ClaudeCLI(claude_bin={self._claude_bin!r}, model={self._model!r}, "
            f"effort={self._effort!r}, auth={self.auth_mode!r})"
        )

    @property
    def claude_bin(self) -> str:
        return self._claude_bin

    @property
    def auth_mode(self) -> str:
        if self._auth_label:
            return self._auth_label
        return "subscription token" if self._oauth_token else "this machine's claude login"

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        returncode, stdout, stderr = await self._run(system_prompt, user_prompt, "json")
        return self._parse(returncode, stdout, stderr)

    async def inspect(self) -> Dict[str, Any]:
        """Send a tiny request and report what the CLI session was started with."""
        returncode, stdout, stderr = await self._run(
            "Reply with exactly: OK", "ping", "stream-json"
        )
        init = self._find_init(stdout)
        reply = self._parse(returncode, stdout, stderr)
        return {
            "auth": self.auth_mode,
            "init": {key: init.get(key) for key in _INIT_FIELDS} if init is not None else None,
            "reply": reply,
        }

    def _build_command(self, system_prompt_path: str, output_format: str) -> List[str]:
        command = [
            self._claude_bin,
            "-p",
            "--safe-mode",
            "--settings",
            json.dumps(ISOLATION_SETTINGS),
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file",
            system_prompt_path,
            "--output-format",
            output_format,
            "--effort",
            self._effort,
        ]
        if output_format == "stream-json":
            command.append("--verbose")
        if self._model:
            command.extend(["--model", self._model])
        return command

    def _redact(self, text: str) -> str:
        return text.replace(self._oauth_token, "[REDACTED]") if self._oauth_token else text

    def _parse(self, returncode: int, stdout: str, stderr: str) -> str:
        try:
            return parse_result(returncode, stdout, stderr)
        except ClaudeAuthError as exc:
            if not self._oauth_token:
                raise
            raise ClaudeAuthError(
                "Claude rejected the token or it has expired: run `claude setup-token` on a "
                "computer with a browser and update CLAUDE_CODE_OAUTH_TOKEN. "
                f"CLI said: {exc.detail}",
                detail=exc.detail,
            ) from None

    @staticmethod
    def _find_init(stdout: str) -> Optional[Dict[str, Any]]:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                return event
        return None

    async def _run(
        self, system_prompt: str, user_prompt: str, output_format: str
    ) -> Tuple[int, str, str]:
        ensure_private_dir(self._workdir)
        descriptor, prompt_path = tempfile.mkstemp(
            dir=str(self._workdir), prefix="system-", suffix=".txt"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(system_prompt)
            os.chmod(prompt_path, 0o600)
            return await self._execute(
                self._build_command(prompt_path, output_format), user_prompt
            )
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass

    async def _execute(self, command: List[str], user_prompt: str) -> Tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._workdir),
                env=build_env(self._oauth_token),
                start_new_session=True,
            )
        except OSError as exc:
            raise ClaudeError(
                f"Claude CLI not found or not executable: {self._claude_bin}"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(user_prompt.encode("utf-8")), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            _kill_process_tree(process.pid)
            try:
                await asyncio.wait_for(process.wait(), timeout=_KILL_WAIT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("Claude CLI process %s did not exit after kill", process.pid)
            raise ClaudeTimeout(f"Claude CLI timed out after {self._timeout} seconds") from None
        except asyncio.CancelledError:
            _kill_process_tree(process.pid)
            raise

        return (
            process.returncode if process.returncode is not None else -1,
            self._redact(stdout.decode("utf-8", "replace")),
            self._redact(stderr.decode("utf-8", "replace")),
        )
