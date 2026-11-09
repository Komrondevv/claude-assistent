"""Settings loading.

Settings come from exactly one place and the two are never mixed:

* local runs read only the ``.env`` file (shell variables are ignored);
* container runs get a ``source`` mapping built by ``bot.py --settings-from-env``.

Nothing here writes to ``os.environ``, so tokens cannot leak into other processes.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Union

from dotenv import dotenv_values

PathLike = Union[str, "os.PathLike[str]"]

SETTING_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "AUTO_SEND",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_BIN",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CLAUDE_TIMEOUT_SECONDS",
    "LIMIT_COOLDOWN_MINUTES",
    "OWNER_TAKEOVER_MINUTES",
    "HISTORY_LIMIT",
    "DATA_DIR",
    "PROFILE_PATH",
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}
_BOT_TOKEN_RE = re.compile(r"^\d+:\S+$")

AUTH_MACHINE_LOGIN = "this machine's claude login"
AUTH_TOKEN_ENV_FILE = "subscription token from .env"
AUTH_TOKEN_ENVIRONMENT = "subscription token from environment variables"


class ConfigError(Exception):
    """A setting is missing or invalid. The message never contains the secret itself."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_id: int
    auto_send: bool
    claude_oauth_token: str = field(repr=False)
    claude_bin: str
    claude_model: Optional[str]
    claude_effort: str
    claude_timeout_seconds: int
    limit_cooldown_minutes: int
    owner_takeover_minutes: int
    history_limit: int
    data_dir: Path
    profile_path: Path
    from_environment: bool = False

    @property
    def claude_auth_mode(self) -> str:
        if not self.claude_oauth_token:
            return AUTH_MACHINE_LOGIN
        return AUTH_TOKEN_ENVIRONMENT if self.from_environment else AUTH_TOKEN_ENV_FILE


def load_settings(
    env_file: PathLike,
    *,
    require_telegram: bool = True,
    overrides: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Build ``Settings`` from ``source`` if given, otherwise from the ``env_file``."""
    env_path = Path(env_file)
    base_dir = env_path.resolve().parent
    from_environment = source is not None
    where = "the environment variables" if from_environment else str(env_path)

    values: Dict[str, str] = {}
    if source is not None:
        values.update({key: value for key, value in source.items() if value is not None})
    else:
        if not env_path.is_file():
            raise ConfigError(
                f"{env_path} does not exist. Create it with: "
                "cp .env.example .env && chmod 600 .env"
            )
        # interpolate=False: "${VAR}" must not pull values from the shell environment.
        loaded = dotenv_values(env_path, interpolate=False, encoding="utf-8-sig")
        values.update({key: value for key, value in loaded.items() if value is not None})
    if overrides:
        values.update(overrides)

    def get(key: str) -> str:
        return str(values.get(key, "")).strip()

    bot_token = ""
    owner_id = 0
    if require_telegram:
        bot_token = _parse_bot_token(get("TELEGRAM_BOT_TOKEN"), where)
        owner_id = _parse_owner_id(get("OWNER_ID"), where)

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        auto_send=_parse_bool("AUTO_SEND", get("AUTO_SEND"), False, where),
        claude_oauth_token=_parse_oauth_token(get("CLAUDE_CODE_OAUTH_TOKEN"), where),
        claude_bin=get("CLAUDE_BIN") or _default_claude_bin(),
        claude_model=get("CLAUDE_MODEL") or None,
        claude_effort=_parse_effort(get("CLAUDE_EFFORT"), where),
        claude_timeout_seconds=_parse_int("CLAUDE_TIMEOUT_SECONDS", get("CLAUDE_TIMEOUT_SECONDS"), 120, 1, where),
        limit_cooldown_minutes=_parse_int("LIMIT_COOLDOWN_MINUTES", get("LIMIT_COOLDOWN_MINUTES"), 30, 1, where),
        owner_takeover_minutes=_parse_int("OWNER_TAKEOVER_MINUTES", get("OWNER_TAKEOVER_MINUTES"), 15, 0, where),
        history_limit=_parse_int("HISTORY_LIMIT", get("HISTORY_LIMIT"), 30, 1, where),
        data_dir=_resolve_path(get("DATA_DIR") or "data", base_dir),
        profile_path=_resolve_path(get("PROFILE_PATH") or "profile.md", base_dir),
        from_environment=from_environment,
    )


def env_file_warning(path: PathLike) -> Optional[str]:
    """Return a warning if the file is readable by group or others, else ``None``."""
    if os.name != "posix":
        # NTFS permissions are not expressed in POSIX mode bits.
        return None
    env_path = Path(path)
    try:
        mode = stat.S_IMODE(env_path.stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return (
            f"{env_path} is accessible by other users (mode {mode:o}). "
            f"Restrict it with: chmod 600 {env_path}"
        )
    return None


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path


def _default_claude_bin() -> str:
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def _parse_bot_token(value: str, where: str) -> str:
    if not value:
        raise ConfigError(f"TELEGRAM_BOT_TOKEN is missing. Set it in {where}.")
    if not _BOT_TOKEN_RE.match(value):
        raise ConfigError(f"TELEGRAM_BOT_TOKEN in {where} must look like <digits>:<text>.")
    return value


def _parse_owner_id(value: str, where: str) -> int:
    if not value:
        raise ConfigError(f"OWNER_ID is missing. Set it in {where}.")
    if not value.isascii() or not value.isdigit():
        raise ConfigError(f"OWNER_ID in {where} must contain digits only.")
    return int(value)


def _parse_bool(key: str, value: str, default: bool, where: str) -> bool:
    if not value:
        return default
    lowered = value.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigError(f"{key} in {where} must be one of true/false/1/0/yes/no/on/off.")


def _parse_oauth_token(value: str, where: str) -> str:
    if not value:
        return ""
    if value.startswith("sk-ant-api"):
        raise ConfigError(
            f"CLAUDE_CODE_OAUTH_TOKEN in {where} is an API key, not a subscription token. "
            "Run `claude setup-token` and use the sk-ant-oat... token it prints."
        )
    if not value.startswith("sk-ant-oat") or re.search(r"\s", value):
        raise ConfigError(
            f"CLAUDE_CODE_OAUTH_TOKEN in {where} is not a valid subscription token. "
            "It must start with sk-ant-oat and sit on a single line with no spaces."
        )
    return value


def _parse_effort(value: str, where: str) -> str:
    if not value:
        return "low"
    lowered = value.lower()
    if lowered not in EFFORT_LEVELS:
        raise ConfigError(f"CLAUDE_EFFORT in {where} must be one of {', '.join(EFFORT_LEVELS)}.")
    return lowered


def _parse_int(key: str, value: str, default: int, minimum: int, where: str) -> int:
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{key} in {where} must be a whole number.") from None
    if number < minimum:
        raise ConfigError(f"{key} in {where} must be at least {minimum}.")
    return number
