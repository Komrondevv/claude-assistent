"""Exercises the real subprocess path against a fake ``claude`` executable."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from assistant.claude_cli import (
    ISOLATION_SETTINGS,
    ClaudeAuthError,
    ClaudeCLI,
    ClaudeError,
    ClaudeLimitError,
    ClaudeTimeout,
    classify_failure,
    parse_result,
)

TOKEN = "sk-ant-oat01-" + "t" * 60

FAKE_SCRIPT = r"""
import json, os, stat, subprocess, sys, time

argv = sys.argv[1:]
prompt_path = argv[argv.index("--system-prompt-file") + 1]
with open(prompt_path, encoding="utf-8") as handle:
    prompt = handle.read()
record = {
    "argv": argv,
    "stdin": sys.stdin.buffer.read().decode("utf-8"),
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "listing": sorted(os.listdir(".")),
    "pid": os.getpid(),
    "prompt": prompt,
    "prompt_path": prompt_path,
    "prompt_mode": stat.S_IMODE(os.stat(prompt_path).st_mode),
    "cwd_mode": stat.S_IMODE(os.stat(".").st_mode),
}
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "sleep":
    record["grandchild"] = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    ).pid
with open(os.environ["FAKE_RECORD"] + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(record, handle)
os.replace(os.environ["FAKE_RECORD"] + ".tmp", os.environ["FAKE_RECORD"])


def emit(result, is_error=False, subtype="success", status=None):
    event = {"type": "result", "subtype": subtype, "is_error": is_error, "result": result}
    if status is not None:
        event["api_error_status"] = status
    print(json.dumps(event), flush=True)


token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
if mode == "ok":
    if "stream-json" in argv:
        init = {
            "type": "system", "subtype": "init", "tools": [], "mcp_servers": [],
            "plugins": [], "skills": [], "apiKeySource": "none",
            "apiProvider": "firstParty", "model": "fake-model",
        }
        print(json.dumps(init), flush=True)
    emit("  hello there \n")
elif mode == "sleep":
    time.sleep(60)
elif mode == "limit_epoch":
    emit("Claude usage limit reached|1760000000", is_error=True)
elif mode == "limit_text":
    emit("You've hit your limit, resets 3pm (UTC)", is_error=True)
elif mode == "auth":
    emit("Invalid API key, please run /login. Used " + token, is_error=True, status=401)
elif mode == "crash":
    print("boom", file=sys.stderr, flush=True)
    sys.exit(3)
elif mode == "empty":
    emit("")
elif mode == "echo_token":
    emit("leak " + token)
"""


def _make_fake_claude(directory: Path) -> str:
    if os.name == "posix":
        executable = directory / "claude"
        executable.write_text(f"#!{sys.executable}\n{FAKE_SCRIPT}", encoding="utf-8")
        executable.chmod(0o755)
        return str(executable)
    # Windows ignores shebangs, so a batch shim stands in for the executable.
    script = directory / "fake_claude.py"
    script.write_text(FAKE_SCRIPT, encoding="utf-8")
    shim = directory / "claude.cmd"
    shim.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    return str(shim)


def _pid_alive(pid: int) -> bool:
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    # os.kill(pid, 0) would terminate the process on Windows.
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
    )
    return str(pid) in result.stdout


def _wait_until_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


class Fake:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self.claude_bin = _make_fake_claude(bin_dir)
        self.record_path = tmp_path / "record.json"
        self.workdir = tmp_path / "data" / "claude-run"
        monkeypatch.setenv("FAKE_RECORD", str(self.record_path))
        monkeypatch.setenv("FAKE_MODE", "ok")

    def cli(self, mode: str = "ok", **kwargs) -> ClaudeCLI:
        self.monkeypatch.setenv("FAKE_MODE", mode)
        kwargs.setdefault("timeout", 30)
        return ClaudeCLI(self.claude_bin, workdir=self.workdir, **kwargs)

    @property
    def record(self):
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    def workdir_files(self):
        return list(self.workdir.iterdir())


@pytest.fixture
def fake(tmp_path, monkeypatch) -> Fake:
    return Fake(tmp_path, monkeypatch)


async def test_result_is_stripped(fake):
    assert await fake.cli().generate("system", "user") == "hello there"


async def test_message_goes_only_through_stdin(fake):
    await fake.cli().generate("system", "maxfiy-xabar")
    record = fake.record
    assert record["stdin"] == "maxfiy-xabar"
    assert not any("maxfiy-xabar" in arg for arg in record["argv"])


async def test_unicode_message_survives_stdin(fake):
    await fake.cli().generate("system", "Салом, қалесан? 😀")
    assert fake.record["stdin"] == "Салом, қалесан? 😀"


async def test_profile_is_in_a_private_file_not_argv(fake):
    await fake.cli().generate("maxfiy-profil", "user")
    record = fake.record
    assert record["prompt"] == "maxfiy-profil"
    assert not any("maxfiy-profil" in arg for arg in record["argv"])
    if os.name == "posix":
        assert record["prompt_mode"] == 0o600


async def test_temp_file_is_removed_after_success(fake):
    await fake.cli().generate("system", "user")
    assert not Path(fake.record["prompt_path"]).exists()
    assert fake.workdir_files() == []


async def test_temp_file_is_removed_after_failure(fake):
    with pytest.raises(ClaudeError):
        await fake.cli("crash").generate("system", "user")
    assert fake.workdir_files() == []


async def test_shell_syntax_is_not_executed(fake, tmp_path):
    marker = tmp_path / "pwned"
    await fake.cli().generate("system", f"$(touch {marker}) `touch {marker}`; touch {marker}")
    assert not marker.exists()


async def test_isolation_flags_in_order(fake):
    await fake.cli(effort="medium").generate("system", "user")
    argv = fake.record["argv"]
    assert argv[:3] == ["-p", "--safe-mode", "--settings"]
    assert argv[4:] == [
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt-file",
        fake.record["prompt_path"],
        "--output-format",
        "json",
        "--effort",
        "medium",
    ]
    assert "--mcp-config" not in argv
    assert "--bare" not in argv


async def test_settings_json_disables_hooks_and_plugins(fake):
    await fake.cli().generate("system", "user")
    argv = fake.record["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings == ISOLATION_SETTINGS
    assert settings["disableAllHooks"] is True
    assert settings["enabledPlugins"]
    assert not any(settings["enabledPlugins"].values())


async def test_model_flag_is_added_when_configured(fake):
    await fake.cli(model="sonnet").generate("system", "user")
    argv = fake.record["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"


async def test_no_model_flag_by_default(fake):
    await fake.cli().generate("system", "user")
    assert "--model" not in fake.record["argv"]


async def test_child_environment_is_scrubbed(fake, monkeypatch):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
    ):
        monkeypatch.setenv(name, "must-not-pass")
    monkeypatch.setenv("HOME", "/keep/this/home")
    await fake.cli().generate("system", "user")
    env = fake.record["env"]
    assert "must-not-pass" not in env.values()
    assert env["HOME"] == "/keep/this/home"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


async def test_configured_token_reaches_only_the_child_env(fake):
    cli = fake.cli(oauth_token=TOKEN)
    await cli.generate("system", "user")
    record = fake.record
    assert record["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert TOKEN not in " ".join(record["argv"])
    assert TOKEN not in record["stdin"]
    assert TOKEN not in record["prompt"]
    assert TOKEN not in repr(cli)


async def test_no_token_in_child_when_not_configured(fake, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    await fake.cli().generate("system", "user")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in fake.record["env"]


async def test_token_in_output_is_redacted(fake):
    reply = await fake.cli("echo_token", oauth_token=TOKEN).generate("system", "user")
    assert reply == "leak [REDACTED]"


async def test_rejected_token_message_is_helpful_and_redacted(fake):
    with pytest.raises(ClaudeAuthError) as caught:
        await fake.cli("auth", oauth_token=TOKEN).generate("system", "user")
    message = str(caught.value)
    assert "claude setup-token" in message
    assert "CLI said:" in message
    assert "[REDACTED]" in message
    assert TOKEN not in message
    assert TOKEN not in caught.value.detail


async def test_login_error_without_token_suggests_login(fake):
    with pytest.raises(ClaudeAuthError) as caught:
        await fake.cli("auth").generate("system", "user")
    assert "/login" in str(caught.value)


async def test_inspect_reports_auth_and_init(fake):
    report = await fake.cli(auth_label="subscription token from .env").inspect()
    assert report["auth"] == "subscription token from .env"
    assert report["reply"] == "hello there"
    assert report["init"]["apiProvider"] == "firstParty"
    assert report["init"]["plugins"] == []
    assert "--verbose" in fake.record["argv"]
    assert fake.record["stdin"] == "ping"


def test_auth_mode_defaults():
    assert ClaudeCLI("claude", workdir="x").auth_mode == "this machine's claude login"
    assert ClaudeCLI("claude", workdir="x", oauth_token=TOKEN).auth_mode == "subscription token"


async def test_runs_in_its_own_private_empty_directory(fake):
    await fake.cli().generate("system", "user")
    record = fake.record
    assert os.path.samefile(record["cwd"], fake.workdir)
    assert record["listing"] == [Path(record["prompt_path"]).name]
    if os.name == "posix":
        assert record["cwd_mode"] == 0o700


async def test_timeout_kills_the_whole_process_group(fake):
    cli = fake.cli("sleep", timeout=2)
    started = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        await cli.generate("system", "user")
    assert time.monotonic() - started < 5
    record = fake.record
    assert _wait_until_dead(record["pid"])
    assert _wait_until_dead(record["grandchild"])
    assert fake.workdir_files() == []


async def test_cancellation_kills_the_process(fake):
    task = asyncio.ensure_future(fake.cli("sleep", timeout=30).generate("system", "user"))
    deadline = time.monotonic() + 15
    while not fake.record_path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = fake.record
    assert _wait_until_dead(record["pid"])
    assert _wait_until_dead(record["grandchild"])


async def test_limit_with_epoch(fake):
    with pytest.raises(ClaudeLimitError) as caught:
        await fake.cli("limit_epoch").generate("system", "user")
    assert caught.value.resets_at == 1760000000
    assert "|1760000000" not in caught.value.detail


async def test_limit_with_reset_text(fake):
    with pytest.raises(ClaudeLimitError) as caught:
        await fake.cli("limit_text").generate("system", "user")
    assert caught.value.reset_text == "3pm (UTC)"
    assert str(caught.value) == "Claude subscription usage limit reached (resets 3pm (UTC))"


async def test_crash_is_reported(fake):
    with pytest.raises(ClaudeError, match="boom"):
        await fake.cli("crash").generate("system", "user")


async def test_empty_reply_is_an_error(fake):
    with pytest.raises(ClaudeError, match="empty"):
        await fake.cli("empty").generate("system", "user")


async def test_missing_binary(tmp_path):
    cli = ClaudeCLI(str(tmp_path / "no-such-claude"), workdir=tmp_path / "run")
    with pytest.raises(ClaudeError, match="not found"):
        await cli.generate("system", "user")


# classify_failure


@pytest.mark.parametrize(
    "message",
    [
        "Claude usage limit reached",
        "You've hit your limit",
        "You hit your usage limit",
        "rate_limit_error",
        "Rate limit exceeded",
        "You are out of extra usage",
        "Quota exceeded",
    ],
)
def test_classify_limit_messages(message):
    assert isinstance(classify_failure(message), ClaudeLimitError)


def test_classify_limit_by_status():
    assert isinstance(classify_failure("Too many requests", 429), ClaudeLimitError)


def test_classify_limit_details():
    error = classify_failure("Usage limit reached|1760000000")
    assert isinstance(error, ClaudeLimitError)
    assert error.resets_at == 1760000000
    assert error.detail == "Usage limit reached"


def test_classify_limit_without_reset():
    error = classify_failure("Usage limit reached")
    assert isinstance(error, ClaudeLimitError)
    assert error.resets_at is None
    assert error.reset_text is None
    assert str(error) == "Claude subscription usage limit reached"


@pytest.mark.parametrize(
    "message",
    [
        "Not logged in",
        "Please run /login",
        "Invalid API key",
        "Authentication failed",
        "Unauthorized",
        "unauthorised",
        "OAuth token has expired",
    ],
)
def test_classify_auth_messages(message):
    error = classify_failure(message)
    assert isinstance(error, ClaudeAuthError)
    assert "/login" in str(error)
    assert error.detail == message


@pytest.mark.parametrize("status", [401, 403])
def test_classify_auth_by_status(status):
    assert isinstance(classify_failure("nope", status), ClaudeAuthError)


def test_classify_auth_detail_is_capped():
    assert len(classify_failure("Not logged in " + "x" * 900).detail) == 300


def test_classify_other_is_a_truncated_claude_error():
    error = classify_failure("x" * 900)
    assert type(error) is ClaudeError
    assert len(str(error)) == 500


# parse_result


def _result_line(**fields):
    base = {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
    base.update(fields)
    return json.dumps(base)


def test_parse_uses_the_last_non_empty_line():
    stdout = '{"type": "system"}\n' + _result_line(result="  hi  ") + "\n\n"
    assert parse_result(0, stdout, "") == "hi"


def test_parse_is_error_is_classified():
    with pytest.raises(ClaudeAuthError):
        parse_result(1, _result_line(is_error=True, result="Not logged in"), "")


def test_parse_includes_stderr_when_classifying():
    with pytest.raises(ClaudeLimitError):
        parse_result(1, _result_line(is_error=True, result="failed"), "usage limit reached")


def test_parse_uses_api_status():
    with pytest.raises(ClaudeLimitError):
        parse_result(1, _result_line(is_error=True, result="busy", api_error_status=429), "")


def test_parse_non_success_subtype_is_an_error():
    with pytest.raises(ClaudeError):
        parse_result(0, _result_line(subtype="error_during_execution", result=""), "")


def test_parse_empty_result_is_an_error():
    with pytest.raises(ClaudeError, match="empty"):
        parse_result(0, _result_line(result="   "), "")


def test_parse_non_json_prefers_stderr():
    with pytest.raises(ClaudeError, match="stderr text"):
        parse_result(1, "not json", "stderr text")


def test_parse_non_json_falls_back_to_stdout():
    with pytest.raises(ClaudeError, match="not json"):
        parse_result(1, "not json", "")


def test_parse_nothing_reports_exit_code():
    with pytest.raises(ClaudeError, match="exit code 7"):
        parse_result(7, "", "")
