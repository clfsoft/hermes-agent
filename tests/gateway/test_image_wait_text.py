"""Tests for the image-wait-text mechanism in GatewayRunner._handle_message.

When a user sends an image without text, the gateway should stash the event
and wait for a text follow-up before processing. If no text arrives within
the timeout, the image is processed with the standard placeholder.
"""
import asyncio
import dataclasses
import pytest
from unittest.mock import AsyncMock, patch

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.WEIXIN: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_image_only_events = {}
    runner._pending_image_only_timers = {}
    runner._image_wait_flushing = set()
    runner._update_prompt_pending = {}
    runner._pending_approvals = {}
    runner._pending_continuations = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._busy_ack_ts = {}
    runner._queued_events = {}
    runner._session_run_generation = {}
    runner._draining = False
    runner._busy_input_mode = "interrupt"
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner.pairing_store = AsyncMock()
    runner.pairing_store._is_rate_limited = AsyncMock(return_value=False)
    runner.hooks = AsyncMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.emit_collect = AsyncMock(return_value=[])
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.WEIXIN,
        chat_id="wx_123",
        chat_name="DM",
        chat_type="dm",
        user_id="user_abc",
        user_name="TestUser",
    )


def _image_event(text: str = "") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO,
        source=_source(),
        media_urls=["/tmp/test_image.jpg"],
        media_types=["image/jpeg"],
    )


def _text_event(text: str = "请分析这张图片") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
    )


class TestIsImageOnlyEvent:
    def test_image_no_text_is_image_only(self):
        runner = _make_runner()
        event = _image_event()
        assert runner._is_image_only_event(event) is True

    def test_image_with_text_is_not_image_only(self):
        runner = _make_runner()
        event = _image_event(text="看这个")
        assert runner._is_image_only_event(event) is False

    def test_text_only_is_not_image_only(self):
        runner = _make_runner()
        event = _text_event()
        assert runner._is_image_only_event(event) is False

    def test_no_media_is_not_image_only(self):
        runner = _make_runner()
        event = MessageEvent(
            text="",
            message_type=MessageType.TEXT,
            source=_source(),
        )
        assert runner._is_image_only_event(event) is False

    def test_audio_only_is_not_image_only(self):
        runner = _make_runner()
        event = MessageEvent(
            text="",
            message_type=MessageType.VOICE,
            source=_source(),
            media_urls=["/tmp/test_audio.ogg"],
            media_types=["audio/ogg"],
        )
        assert runner._is_image_only_event(event) is False


class TestMergeImageEvent:
    def test_merge_image_into_text(self):
        runner = _make_runner()
        pending = _image_event()
        text_event = _text_event("请分析这张图片")
        merged = runner._merge_image_event_into(pending, text_event)
        assert merged.text == "请分析这张图片"
        assert merged.media_urls == ["/tmp/test_image.jpg"]
        assert merged.media_types == ["image/jpeg"]

    def test_merge_preserves_source_from_pending(self):
        runner = _make_runner()
        pending = _image_event()
        text_event = _text_event("hello")
        merged = runner._merge_image_event_into(pending, text_event)
        assert merged.source == pending.source


class TestImageWaitTextFlow:
    @pytest.mark.asyncio
    async def test_image_only_is_stashed(self):
        runner = _make_runner()
        runner._IMAGE_WAIT_TEXT_SECONDS = 30
        runner._is_user_authorized = lambda source: True
        runner._session_key_for_source = lambda source: "wx:user_abc"

        event = _image_event()
        with patch.object(runner, "_handle_message", wraps=runner._handle_message):
            result = await runner._handle_message(event)

        assert result is None
        assert "wx:user_abc" in runner._pending_image_only_events
        assert runner._pending_image_only_events["wx:user_abc"].media_urls == ["/tmp/test_image.jpg"]

        timer = runner._pending_image_only_timers.get("wx:user_abc")
        assert timer is not None
        timer.cancel()

    @pytest.mark.asyncio
    async def test_text_merges_with_pending_image(self):
        runner = _make_runner()
        runner._IMAGE_WAIT_TEXT_SECONDS = 30
        runner._is_user_authorized = lambda source: True
        runner._session_key_for_source = lambda source: "wx:user_abc"

        image_event = _image_event()
        runner._pending_image_only_events["wx:user_abc"] = image_event

        text_event = _text_event("请分析这张图片")

        with patch.object(runner, "_handle_message", wraps=runner._handle_message):
            with patch.object(runner, "_handle_message_with_agent", new_callable=AsyncMock) as mock_agent:
                mock_agent.return_value = "分析结果"
                result = await runner._handle_message(text_event)

        assert "wx:user_abc" not in runner._pending_image_only_events
        call_event = mock_agent.call_args[0][0]
        assert call_event.media_urls == ["/tmp/test_image.jpg"]
        assert call_event.text == "请分析这张图片"

    @pytest.mark.asyncio
    async def test_timeout_flushes_image(self):
        runner = _make_runner()
        runner._IMAGE_WAIT_TEXT_SECONDS = 0.1
        runner._is_user_authorized = lambda source: True
        runner._session_key_for_source = lambda source: "wx:user_abc"

        image_event = _image_event()
        runner._pending_image_only_events["wx:user_abc"] = image_event

        with patch.object(runner, "_handle_message_with_agent", new_callable=AsyncMock) as mock_agent:
            mock_agent.return_value = "收到图片"
            await runner._flush_pending_image_only("wx:user_abc")

        assert "wx:user_abc" not in runner._pending_image_only_events
        assert mock_agent.called

    @pytest.mark.asyncio
    async def test_session_reset_clears_pending_image(self):
        runner = _make_runner()
        runner._pending_image_only_events["wx:user_abc"] = _image_event()
        timer_task = asyncio.create_task(asyncio.sleep(100))
        runner._pending_image_only_timers["wx:user_abc"] = timer_task

        runner._clear_session_boundary_security_state("wx:user_abc")

        assert "wx:user_abc" not in runner._pending_image_only_events
        assert "wx:user_abc" not in runner._pending_image_only_timers
        assert timer_task.cancelled() or timer_task.cancelling()

    @pytest.mark.asyncio
    async def test_image_wait_disabled_when_zero(self):
        runner = _make_runner()
        runner._IMAGE_WAIT_TEXT_SECONDS = 0
        runner._is_user_authorized = lambda source: True
        runner._session_key_for_source = lambda source: "wx:user_abc"

        event = _image_event()
        with patch.object(runner, "_handle_message_with_agent", new_callable=AsyncMock) as mock_agent:
            mock_agent.return_value = "收到图片"
            await runner._handle_message(event)

        assert "wx:user_abc" not in runner._pending_image_only_events

    @pytest.mark.asyncio
    async def test_image_wait_skipped_when_agent_running(self):
        runner = _make_runner()
        runner._IMAGE_WAIT_TEXT_SECONDS = 30
        runner._is_user_authorized = lambda source: True
        runner._session_key_for_source = lambda source: "wx:user_abc"
        runner._running_agents["wx:user_abc"] = object()

        event = _image_event()
        with patch.object(runner, "_handle_message", wraps=runner._handle_message):
            result = await runner._handle_message(event)

        assert "wx:user_abc" not in runner._pending_image_only_events
