"""Entry point for the Telegram Business assistant."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError

from assistant.claude_cli import ClaudeError
from assistant.config import ConfigError, SETTING_KEYS, Settings, env_file_warning, load_settings
from assistant.handlers import ALLOWED_UPDATES, build_router
from assistant.language import detect_language
from assistant.prompts import build_system_prompt, build_user_prompt
from assistant.service import build_assistant, make_claude_cli, profile_is_filled, read_profile
from assistant.store import ensure_private_dir

logger = logging.getLogger("bot")

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

_FORBIDDEN_KEY_SOURCES = {"ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"}


def settings_from_process_env(*, require_telegram: bool = True) -> Settings:
    """Read settings from the environment, then remove them so child processes cannot inherit them."""
    try:
        source = {key: os.environ[key] for key in SETTING_KEYS if key in os.environ}
        return load_settings(ENV_FILE, require_telegram=require_telegram, source=source)
    finally:
        for key in SETTING_KEYS:
            os.environ.pop(key, None)


def audit_init(init: Optional[Dict[str, Any]]) -> List[str]:
    """Return every way the CLI session failed to be isolated."""
    if init is None:
        return ["the CLI did not emit an init event"]
    problems = [
        f"{field} is not empty: {init.get(field)}"
        for field in ("tools", "mcp_servers", "plugins", "skills")
        if init.get(field)
    ]
    if init.get("apiKeySource") in _FORBIDDEN_KEY_SOURCES:
        problems.append(f"an API key is in use: {init.get('apiKeySource')}")
    if init.get("apiProvider") not in ("firstParty", None):
        problems.append(f"unexpected API provider: {init.get('apiProvider')}")
    return problems


async def check_claude(settings: Settings) -> int:
    try:
        report = await make_claude_cli(settings).inspect()
    except ClaudeError as exc:
        print(f"Claude check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    problems = audit_init(report.get("init"))
    if problems:
        for problem in problems:
            print(f"Claude check failed: {problem}", file=sys.stderr)
        return 1
    print(f"OK. Claude auth: {report['auth']}")
    return 0


async def try_message(settings: Settings, text: str) -> int:
    language = detect_language(text)
    system_prompt = build_system_prompt(
        read_profile(settings.profile_path), datetime.now().astimezone()
    )
    user_prompt = build_user_prompt([{"role": "other", "name": "Friend", "text": text}], language)
    try:
        reply = await make_claude_cli(settings).generate(system_prompt, user_prompt)
    except ClaudeError as exc:
        print(f"Claude failed: {exc}", file=sys.stderr)
        return 1
    print(f"Language: {language or 'unknown'}")
    print(f"Message:  {text}")
    print(f"Reply:    {reply}")
    return 0


async def run_bot(settings: Settings) -> int:
    assistant = build_assistant(settings)
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(assistant))
    try:
        me = await bot.get_me()
        assistant.bot_username = me.username
        logger.info("Bot @%s is running", me.username)
        if settings.auto_send:
            logger.warning("Mode: AUTO-SEND. Replies go out to real people without review.")
        else:
            logger.info("Mode: PREVIEW. Replies are only shown in the logs.")
        logger.info("Claude auth: %s", settings.claude_auth_mode)
        logger.info("Claude binary: %s", settings.claude_bin)
        if assistant.state.paused:
            logger.warning("The bot starts PAUSED. Send /resume to turn it back on.")
        if not profile_is_filled(read_profile(settings.profile_path)):
            logger.warning("profile.md is not filled in. Replies will have nothing to draw on.")
        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
        return 0
    except TelegramUnauthorizedError:
        print(
            "Telegram rejected the bot token. Check TELEGRAM_BOT_TOKEN or create a new one "
            "with @BotFather.",
            file=sys.stderr,
        )
        return 2
    finally:
        await bot.session.close()


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram Business assistant")
    parser.add_argument(
        "--settings-from-env",
        action="store_true",
        help="read settings from environment variables instead of .env (containers)",
    )
    parser.add_argument(
        "--check-claude", action="store_true", help="verify the Claude CLI session and exit"
    )
    parser.add_argument(
        "--try", dest="try_text", metavar="TEXT", help="draft a reply to TEXT and exit"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Replies can be Cyrillic, which legacy Windows code pages cannot print.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    require_telegram = not args.check_claude and args.try_text is None
    try:
        if args.settings_from_env:
            settings = settings_from_process_env(require_telegram=require_telegram)
        else:
            settings = load_settings(ENV_FILE, require_telegram=require_telegram)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if not args.settings_from_env:
        warning = env_file_warning(ENV_FILE)
        if warning:
            logger.warning(warning)

    ensure_private_dir(settings.data_dir)
    ensure_private_dir(settings.data_dir / "claude-run")

    try:
        if args.check_claude:
            return asyncio.run(check_claude(settings))
        if args.try_text is not None:
            return asyncio.run(try_message(settings, args.try_text))
        return asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
