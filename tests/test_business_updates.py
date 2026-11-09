"""End-to-end behaviour through a real Dispatcher, with Telegram and Claude faked."""
from __future__ import annotations

import asyncio
import json

from aiogram.methods import GetBusinessConnection, SendMessage

from assistant.claude_cli import ClaudeAuthError, ClaudeLimitError, ClaudeTimeout
from assistant.service import Outcome
from tests.conftest import CONNECTION_ID, OTHER_ID, OWNER_ID, assert_private_file

STICKER = {
    "file_id": "s1",
    "file_unique_id": "u1",
    "type": "regular",
    "width": 512,
    "height": 512,
    "is_animated": False,
    "is_video": False,
    "emoji": "😀",
}


# Preview mode


async def test_preview_shows_reply_and_sends_nothing(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Salom") == Outcome.PREVIEWED
    assert h.previews[0].reply == "Salom, hozir bandman"
    assert h.previews[0].incoming == "Salom"
    assert h.sent_messages == []


async def test_preview_is_written_privately_and_not_added_to_history(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Salom")
    path = h.settings.data_dir / "previews.jsonl"
    assert_private_file(path)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["reply"] == "Salom, hozir bandman"
    assert [item["role"] for item in h.assistant.history.get(CONNECTION_ID, OTHER_ID)] == ["other"]


async def test_known_connection_makes_no_telegram_requests_in_preview(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Salom")
    assert h.session.requests == []


async def test_unknown_connection_is_looked_up_once_then_cached(make_harness):
    h = make_harness()
    h.set_connection()
    await h.business_message("Bir")
    await h.business_message("Ikki")
    assert len(h.lookups) == 1
    assert len(h.session.requests) == 1


async def test_lookup_failure_is_no_connection(make_harness):
    h = make_harness()
    assert await h.business_message("Salom", connection_id="missing") == Outcome.NO_CONNECTION
    assert h.generator.calls == []


async def test_message_without_connection_id(make_harness):
    h = make_harness()
    assert await h.business_message("Salom", connection_id=None) == Outcome.NO_CONNECTION


# Which accounts and chats are served


async def test_foreign_account_is_ignored(make_harness):
    h = make_harness()
    await h.connect(owner_id=7777)
    assert await h.business_message("Salom") == Outcome.FOREIGN_CONNECTION
    assert h.generator.calls == []


async def test_disabled_connection_is_ignored(make_harness):
    h = make_harness()
    await h.connect(enabled=False)
    assert await h.business_message("Salom") == Outcome.CONNECTION_DISABLED
    assert h.generator.calls == []


async def test_any_chat_is_answered_without_an_allowlist(make_harness):
    h = make_harness()
    await h.connect()
    for chat_id in (10, 20, 30):
        assert await h.business_message("Salom", from_id=chat_id, chat_id=chat_id) == Outcome.PREVIEWED


# Owner activity


async def test_owner_message_is_recorded_and_not_answered(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Keyin yozaman", from_id=OWNER_ID) == Outcome.OWNER_MESSAGE
    assert h.generator.calls == []
    assert h.assistant.history.get(CONNECTION_ID, OTHER_ID)[-1]["role"] == "owner"


async def test_takeover_silences_chat_then_expires(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Men shu yerdaman", from_id=OWNER_ID)
    assert await h.business_message("Salom") == Outcome.OWNER_ACTIVE
    assert h.generator.calls == []
    h.clock.advance(15 * 60 + 1)
    assert await h.business_message("Salom yana") == Outcome.PREVIEWED


async def test_takeover_only_affects_that_chat(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Men shu yerdaman", from_id=OWNER_ID, chat_id=OTHER_ID)
    assert await h.business_message("Salom", from_id=55, chat_id=55) == Outcome.PREVIEWED


async def test_takeover_can_be_turned_off(make_harness):
    h = make_harness(OWNER_TAKEOVER_MINUTES=0)
    await h.connect()
    await h.business_message("Men shu yerdaman", from_id=OWNER_ID)
    assert await h.business_message("Salom") == Outcome.PREVIEWED


# Bots and duplicates


async def test_messages_sent_by_a_business_bot_are_ignored(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Javob", via_business_bot=True) == Outcome.IGNORED_BOT
    assert h.generator.calls == []


async def test_messages_from_bots_are_ignored(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Beep", is_bot=True) == Outcome.IGNORED_BOT
    assert h.generator.calls == []


async def test_duplicate_in_sequence_is_handled_once(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Salom", message_id=7) == Outcome.PREVIEWED
    assert await h.business_message("Salom", message_id=7) == Outcome.DUPLICATE
    assert len(h.generator.calls) == 1


async def test_duplicate_in_parallel_is_handled_once(make_harness):
    h = make_harness()
    await h.connect()
    await asyncio.gather(
        h.business_message("Salom", message_id=7),
        h.business_message("Salom", message_id=7),
        h.business_message("Salom", message_id=7),
    )
    assert len(h.generator.calls) == 1
    assert sorted(o.value for o in h.assistant.outcomes) == ["duplicate", "duplicate", "previewed"]


async def test_duplicate_after_restart_is_handled_once(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Salom", message_id=7)
    h.restart()
    await h.connect()
    assert await h.business_message("Salom", message_id=7) == Outcome.DUPLICATE
    assert len(h.generator.calls) == 1


async def test_same_message_id_in_another_chat_is_not_a_duplicate(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Salom", message_id=7, from_id=10, chat_id=10)
    assert await h.business_message("Salom", message_id=7, from_id=20, chat_id=20) == Outcome.PREVIEWED


# Message content


async def test_service_message_is_unsupported(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message(None, new_chat_title="Yangi nom") == Outcome.UNSUPPORTED
    assert h.generator.calls == []


async def test_sticker_is_described(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message(None, sticker=STICKER)
    assert "[sticker] 😀" in h.generator.calls[0][1]


async def test_voice_is_described(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message(None, voice={"file_id": "v", "file_unique_id": "u", "duration": 3})
    assert "[voice message]" in h.generator.calls[0][1]


async def test_photo_caption_is_described(make_harness):
    h = make_harness()
    await h.connect()
    photo = [{"file_id": "p", "file_unique_id": "u", "width": 90, "height": 90}]
    await h.business_message(None, photo=photo, caption="Qara")
    assert "[photo] Qara" in h.generator.calls[0][1]


async def test_profile_reaches_the_system_prompt_without_comments(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Salom")
    system_prompt = h.generator.calls[0][0]
    assert "Test Egasi" in system_prompt
    assert "hidden note" not in system_prompt


async def test_history_is_separate_per_connection_and_chat(make_harness):
    h = make_harness()
    await h.connect()
    await h.connect(connection_id="conn-2")
    await h.business_message("birinchi-sir", from_id=10, chat_id=10)
    await h.business_message("ikkinchi-sir", from_id=20, chat_id=20)
    await h.business_message("uchinchi-sir", from_id=10, chat_id=10, connection_id="conn-2")
    assert "birinchi-sir" not in h.generator.calls[1][1]
    assert "birinchi-sir" not in h.generator.calls[2][1]
    assert "ikkinchi-sir" not in h.generator.calls[2][1]


async def test_language_hints(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Привет, как дела?", from_id=1, chat_id=1)
    await h.business_message("Hi, are you free tonight?", from_id=2, chat_id=2)
    await h.business_message("Bugun bo'shmisan?", from_id=3, chat_id=3)
    assert h.generator.calls[0][1].endswith("Their latest message is in Russian.")
    assert h.generator.calls[1][1].endswith("Their latest message is in English.")
    assert h.generator.calls[2][1].endswith("Their latest message is in Uzbek.")


async def test_emoji_uses_the_last_language_of_the_chat(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Привет, как дела?")
    await h.business_message("👍")
    assert h.generator.calls[1][1].endswith("Their latest message is in Russian.")


async def test_skip_is_not_shown(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.replies = ["[SKIP]"]
    assert await h.business_message("ok") == Outcome.SKIPPED
    assert h.previews == []


async def test_skip_is_never_sent(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.replies = ["[SKIP]"]
    assert await h.business_message("ok") == Outcome.SKIPPED
    assert h.session.requests == []


# Auto-send


async def test_auto_send_rechecks_connection_then_sends(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    assert await h.business_message("Salom") == Outcome.SENT
    assert [type(request) for request in h.session.requests] == [GetBusinessConnection, SendMessage]
    sent = h.sent_messages[0]
    assert sent.business_connection_id == CONNECTION_ID
    assert sent.chat_id == OTHER_ID
    last = h.assistant.history.get(CONNECTION_ID, OTHER_ID)[-1]
    assert (last["role"], last["text"]) == ("owner", "Salom, hozir bandman")


async def test_auto_send_splits_long_replies(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.replies = ["x" * 9000]
    assert await h.business_message("Salom") == Outcome.SENT
    assert [len(m.text) for m in h.sent_messages] == [4000, 4000, 1000]


async def test_auto_send_stops_when_reply_right_was_removed(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.set_connection(can_reply=False)
    assert await h.business_message("Salom") == Outcome.PERMISSION_DENIED
    assert h.sent_messages == []
    assert "permission" in h.previews[-1].note


async def test_auto_send_stops_when_connection_was_disabled(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.set_connection(enabled=False)
    assert await h.business_message("Salom") == Outcome.PERMISSION_DENIED
    assert h.sent_messages == []
    assert "disabled" in h.previews[-1].note


async def test_auto_send_stops_when_owner_writes_during_generation(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()

    async def owner_writes():
        await h.business_message("O'zim yozaman", from_id=OWNER_ID)

    h.generator.hook = owner_writes
    assert await h.business_message("Salom") == Outcome.OWNER_ACTIVE
    assert h.sent_messages == []
    assert h.previews[-1].note


# Owner controls


async def test_pause_and_resume_work_for_the_owner(make_harness):
    h = make_harness()
    await h.connect()
    await h.direct_message("/pause")
    assert await h.business_message("Salom") == Outcome.PAUSED
    assert h.generator.calls == []
    await h.direct_message("/resume")
    assert await h.business_message("Salom yana") == Outcome.PREVIEWED


async def test_pause_survives_restart(make_harness):
    h = make_harness()
    await h.connect()
    await h.direct_message("/pause")
    h.restart()
    await h.connect()
    assert await h.business_message("Salom") == Outcome.PAUSED


async def test_commands_from_strangers_do_nothing(make_harness):
    h = make_harness()
    await h.connect()
    await h.direct_message("/pause", from_id=OTHER_ID)
    await h.direct_message("/status", from_id=OTHER_ID)
    assert h.sent_messages == []
    assert await h.business_message("Salom") == Outcome.PREVIEWED


async def test_plain_messages_from_strangers_get_no_answer(make_harness):
    h = make_harness()
    await h.direct_message("Salom bot", from_id=OTHER_ID)
    assert h.session.requests == []
    assert h.generator.calls == []


async def test_status_describes_preview_mode(make_harness):
    h = make_harness()
    await h.direct_message("/status")
    text = h.sent_messages[0].text
    assert "PREVIEW" in text
    assert "only shown in the logs" in text
    assert "running" in text


async def test_start_gives_the_owner_an_uzbek_guide(make_harness):
    h = make_harness()
    await h.direct_message("/start")
    text = h.sent_messages[0].text
    assert "PREVIEW" in text
    assert "ishlayapti" in text
    assert "Chatbots" in text
    for command in ("/pause", "/resume", "/status"):
        assert command in text
    assert "AUTO_SEND=true" in text
    assert "profile.md" in text


async def test_start_reflects_auto_send_and_pause(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.direct_message("/pause")
    await h.direct_message("/start")
    text = h.sent_messages[-1].text
    assert "AUTO-SEND" in text
    assert "pauzada" in text
    assert "AUTO_SEND=true" not in text


async def test_start_for_a_stranger_is_a_short_note(make_harness):
    h = make_harness()
    await h.direct_message("/start", from_id=OTHER_ID)
    text = h.sent_messages[0].text
    assert "faqat egasi uchun" in text
    assert "Test Egasi" not in text
    assert "profile" not in text
    assert h.generator.calls == []


# Usage limits


async def test_limit_waits_for_reset_time_then_continues(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.replies = [ClaudeLimitError("limit", resets_at=h.clock.now + 3600)]
    assert await h.business_message("Bir") == Outcome.LIMITED
    assert await h.business_message("Ikki") == Outcome.LIMITED
    assert len(h.generator.calls) == 1
    h.clock.advance(3601)
    assert await h.business_message("Uch") == Outcome.PREVIEWED


async def test_limit_without_reset_uses_cooldown(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.replies = [ClaudeLimitError("limit")]
    assert await h.business_message("Bir") == Outcome.LIMITED
    h.clock.advance(29 * 60)
    assert await h.business_message("Ikki") == Outcome.LIMITED
    h.clock.advance(2 * 60)
    assert await h.business_message("Uch") == Outcome.PREVIEWED


async def test_limit_survives_restart(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.replies = [ClaudeLimitError("limit", resets_at=h.clock.now + 3600)]
    await h.business_message("Bir")
    h.restart()
    await h.connect()
    assert await h.business_message("Ikki") == Outcome.LIMITED
    assert len(h.generator.calls) == 1


async def test_limit_tells_the_owner_only_in_auto_send(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.replies = [ClaudeLimitError("limit", resets_at=h.clock.now + 3600)]
    await h.business_message("Bir")
    await h.business_message("Ikki")
    assert [m.chat_id for m in h.sent_messages] == [OWNER_ID]
    assert "usage limit" in h.sent_messages[0].text


async def test_limit_is_silent_in_preview(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.replies = [ClaudeLimitError("limit")]
    await h.business_message("Bir")
    assert h.session.requests == []


async def test_resume_clears_the_limit(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.replies = [ClaudeLimitError("limit", resets_at=h.clock.now + 3600)]
    await h.business_message("Bir")
    await h.direct_message("/resume")
    assert await h.business_message("Ikki") == Outcome.PREVIEWED


# CLI failures


async def test_failures_reach_the_owner_at_most_once(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.replies = [
        ClaudeTimeout("too slow"),
        ClaudeAuthError("not logged in", "detail"),
        ClaudeTimeout("too slow"),
    ]
    for text in ("Bir", "Ikki", "Uch"):
        assert await h.business_message(text) == Outcome.ERROR
    assert [m.chat_id for m in h.sent_messages] == [OWNER_ID]


async def test_failure_alerts_again_after_ten_minutes(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.replies = [ClaudeTimeout("too slow"), ClaudeTimeout("too slow")]
    await h.business_message("Bir")
    h.clock.advance(601)
    await h.business_message("Ikki")
    assert [m.chat_id for m in h.sent_messages] == [OWNER_ID, OWNER_ID]


async def test_failures_send_nothing_in_preview(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.replies = [ClaudeTimeout("too slow")]
    assert await h.business_message("Bir") == Outcome.ERROR
    assert h.session.requests == []
    assert h.previews == []
