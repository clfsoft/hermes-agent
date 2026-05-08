"""Tests for the heartbeat scheduler module."""
import os
import sys
import types
import pytest
import time
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path

from gateway.heartbeat import (
    _parse_interval,
    _is_within_active_hours,
    load_heartbeat_md,
    is_heartbeat_ok_response,
    HeartbeatScheduler,
    _HeartbeatConfig,
    _HeartbeatMdCache,
)


class TestParseInterval:
    def test_zero_minutes(self):
        assert _parse_interval("0m") == 0.0

    def test_zero(self):
        assert _parse_interval("0") == 0.0

    def test_off(self):
        assert _parse_interval("off") == 0.0

    def test_disabled(self):
        assert _parse_interval("disabled") == 0.0

    def test_empty(self):
        assert _parse_interval("") == 0.0

    def test_30_minutes(self):
        assert _parse_interval("30m") == 1800.0

    def test_1_hour(self):
        assert _parse_interval("1h") == 3600.0

    def test_2h30m(self):
        assert _parse_interval("2h30m") == 9000.0

    def test_bare_number_defaults_to_minutes(self):
        assert _parse_interval("30") == 1800.0

    def test_seconds(self):
        assert _parse_interval("90s") == 90.0


class TestIsWithinActiveHours:
    def test_no_restrictions(self):
        assert _is_within_active_hours(None, None) is True
        assert _is_within_active_hours("", "") is True
        assert _is_within_active_hours(None, "18:00") is True
        assert _is_within_active_hours("08:00", None) is True

    def test_invalid_format_returns_true(self):
        assert _is_within_active_hours("abc", "xyz") is True


class TestLoadHeartbeatMd:
    def test_missing_file(self, tmp_path):
        with patch("gateway.heartbeat.get_hermes_home", return_value=tmp_path):
            assert load_heartbeat_md() is None

    def test_empty_file(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("")
        with patch("gateway.heartbeat.get_hermes_home", return_value=tmp_path):
            assert load_heartbeat_md() is None

    def test_comments_only(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("# Title\n## Section\n")
        with patch("gateway.heartbeat.get_hermes_home", return_value=tmp_path):
            assert load_heartbeat_md() is None

    def test_valid_content(self, tmp_path):
        content = "# Heartbeat\n- Check emails\n- Review calendar\n"
        (tmp_path / "HEARTBEAT.md").write_text(content)
        with patch("gateway.heartbeat.get_hermes_home", return_value=tmp_path):
            result = load_heartbeat_md()
            assert result is not None
            assert "Check emails" in result


class TestIsHeartbeatOkResponse:
    def test_empty_string(self):
        assert is_heartbeat_ok_response("") is True

    def test_none(self):
        assert is_heartbeat_ok_response("") is True

    def test_heartbeat_ok_only(self):
        assert is_heartbeat_ok_response("HEARTBEAT_OK") is True

    def test_heartbeat_ok_with_short_content(self):
        assert is_heartbeat_ok_response("HEARTBEAT_OK\nAll clear.") is True

    def test_heartbeat_ok_with_long_content(self):
        long_content = "x" * 500
        assert is_heartbeat_ok_response(f"HEARTBEAT_OK\n{long_content}") is False

    def test_no_heartbeat_ok(self):
        assert is_heartbeat_ok_response("I found something important!") is False

    def test_heartbeat_ok_in_middle(self):
        assert is_heartbeat_ok_response("Start HEARTBEAT_OK end") is True

    def test_custom_ack_max_chars(self):
        assert is_heartbeat_ok_response("HEARTBEAT_OK\n" + "x" * 50, ack_max_chars=100) is True
        assert is_heartbeat_ok_response("HEARTBEAT_OK\n" + "x" * 150, ack_max_chars=100) is False


class TestHeartbeatConfig:
    def test_refresh_caches_config(self):
        cfg = _HeartbeatConfig()
        with patch("hermes_cli.config.load_config", return_value={"heartbeat": {"every": "1h"}}):
            cfg.refresh()
            assert cfg._interval == 3600.0
            ts = cfg._ts
            with patch("hermes_cli.config.load_config", return_value={"heartbeat": {"every": "2h"}}) as mock_lc:
                cfg.refresh()
                mock_lc.assert_not_called()
                assert cfg._interval == 3600.0

    def test_refresh_rereads_after_ttl(self):
        cfg = _HeartbeatConfig()
        with patch("hermes_cli.config.load_config", return_value={"heartbeat": {"every": "1h"}}):
            cfg.refresh()
        cfg._ts = time.monotonic() - 61.0
        with patch("hermes_cli.config.load_config", return_value={"heartbeat": {"every": "2h"}}):
            cfg.refresh()
            assert cfg._interval == 7200.0

    def test_env_overrides(self):
        cfg = _HeartbeatConfig()
        with patch.dict(os.environ, {"HERMES_HEARTBEAT_EVERY": "30m", "HERMES_HEARTBEAT_TARGET": "last"}):
            with patch("hermes_cli.config.load_config", side_effect=Exception("no config")):
                cfg.refresh()
                assert cfg._interval == 1800.0
                assert cfg._target == "last"

    def test_light_context_default_true(self):
        cfg = _HeartbeatConfig()
        with patch("hermes_cli.config.load_config", side_effect=Exception("no config")):
            cfg.refresh()
            assert cfg._light_context is True

    def test_light_context_env_false(self):
        cfg = _HeartbeatConfig()
        with patch.dict(os.environ, {"HERMES_HEARTBEAT_LIGHT_CONTEXT": "false"}):
            with patch("hermes_cli.config.load_config", side_effect=Exception("no config")):
                cfg.refresh()
                assert cfg._light_context is False


class TestHeartbeatMdCache:
    def test_returns_none_for_missing_file(self, tmp_path):
        cache = _HeartbeatMdCache()
        with patch("gateway.heartbeat.get_hermes_home", return_value=tmp_path):
            assert cache.get() is None

    def test_caches_content(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("# HB\n- task1\n")
        cache = _HeartbeatMdCache()
        with patch("gateway.heartbeat.get_hermes_home", return_value=tmp_path):
            result1 = cache.get()
            assert result1 is not None
            result2 = cache.get()
            assert result1 is result2

    def test_invalidates_on_mtime_change(self, tmp_path):
        md = tmp_path / "HEARTBEAT.md"
        md.write_text("# HB\n- task1\n")
        cache = _HeartbeatMdCache()
        with patch("gateway.heartbeat.get_hermes_home", return_value=tmp_path):
            result1 = cache.get()
            assert "task1" in result1
            md.write_text("# HB\n- task2\n")
            import os
            os.utime(str(md), (time.time() + 1, time.time() + 1))
            result2 = cache.get()
            assert "task2" in result2


class TestHeartbeatScheduler:
    def _make_runner(self):
        runner = MagicMock()
        runner._running_agents = {}
        runner._model = "openai/gpt-4.1-mini"
        runner._base_url = None
        runner.adapters = {}
        return runner

    def test_update_last_target(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler.update_last_target("session_key_1", "weixin")
        assert scheduler._last_target_session == "session_key_1"
        assert scheduler._last_target_platform == "weixin"

    def test_update_last_target_overwrites(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler.update_last_target("session_1", "weixin")
        scheduler.update_last_target("session_2", "telegram")
        assert scheduler._last_target_session == "session_2"
        assert scheduler._last_target_platform == "telegram"

    def test_resolve_target_none(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler._cfg._target = "none"
        session_key, platform = scheduler._resolve_target_session()
        assert session_key is None
        assert platform is None

    def test_resolve_target_last(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler.update_last_target("session_last", "weixin")
        scheduler._cfg._target = "last"
        session_key, platform = scheduler._resolve_target_session()
        assert session_key == "session_last"
        assert platform == "weixin"

    def test_resolve_target_last_no_previous(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler._cfg._target = "last"
        session_key, platform = scheduler._resolve_target_session()
        assert session_key is None
        assert platform is None

    def test_start_does_nothing_when_interval_zero(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler._cfg._interval = 0.0
        scheduler._cfg._ts = time.monotonic()
        scheduler.start()
        assert scheduler._thread is None

    def test_start_creates_thread(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler._cfg._interval = 3600.0
        scheduler._cfg._target = "none"
        scheduler._cfg._light_context = True
        scheduler._cfg._ts = time.monotonic()
        scheduler.start()
        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()
        scheduler.stop()

    def test_stop_sets_event(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler._cfg._interval = 3600.0
        scheduler._cfg._target = "none"
        scheduler._cfg._light_context = True
        scheduler._cfg._ts = time.monotonic()
        scheduler.start()
        scheduler.stop()
        assert scheduler._stop_event.is_set()

    def test_tick_skips_outside_active_hours(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler._cfg._active_hours_start = "09:00"
        scheduler._cfg._active_hours_end = "18:00"
        scheduler._cfg._ts = time.monotonic()
        with patch("gateway.heartbeat._is_within_active_hours", return_value=False):
            scheduler._tick()

    def test_tick_skips_when_no_heartbeat_md_and_light_context(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        self.scheduler = scheduler
        scheduler._cfg._light_context = True
        scheduler._cfg._ts = time.monotonic()
        self._set_md_cache(None)
        with patch("gateway.heartbeat._is_within_active_hours", return_value=True):
            scheduler._tick()

    def test_tick_skips_when_no_target_session(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        self.scheduler = scheduler
        scheduler._cfg._light_context = True
        scheduler._cfg._target = "none"
        scheduler._cfg._ts = time.monotonic()
        self._set_md_cache("checklist")
        with patch("gateway.heartbeat._is_within_active_hours", return_value=True):
            scheduler._tick()

    def test_tick_skips_when_agent_running(self):
        runner = self._make_runner()
        runner._running_agents = {"session_1": object()}
        scheduler = HeartbeatScheduler(runner)
        self.scheduler = scheduler
        scheduler.update_last_target("session_1", "weixin")
        scheduler._cfg._light_context = True
        scheduler._cfg._target = "last"
        scheduler._cfg._ts = time.monotonic()
        self._set_md_cache("checklist")
        with patch("gateway.heartbeat._is_within_active_hours", return_value=True):
            scheduler._tick()

    def _set_md_cache(self, content):
        mock_cache = MagicMock()
        mock_cache.get.return_value = content
        self.scheduler._md_cache = mock_cache

    def test_tick_runs_heartbeat_agent(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        self.scheduler = scheduler
        scheduler.update_last_target("session_1", "weixin")
        scheduler._cfg._light_context = True
        scheduler._cfg._target = "last"
        scheduler._cfg._prompt = "check"
        scheduler._cfg._ts = time.monotonic()
        self._set_md_cache("checklist")
        with patch("gateway.heartbeat._is_within_active_hours", return_value=True):
            with patch.object(scheduler, "_run_heartbeat_agent") as mock_run:
                scheduler._tick()
                mock_run.assert_called_once()

    def test_run_heartbeat_agent_ok_response(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler._cfg._light_context = True
        scheduler._cfg._ack_max_chars = 300
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "HEARTBEAT_OK"}
        mock_run_agent = types.ModuleType("run_agent")
        mock_run_agent.AIAgent = MagicMock(return_value=mock_agent)
        with patch.dict(sys.modules, {"run_agent": mock_run_agent}):
            with patch("hermes_cli.config.load_config", return_value={}):
                with patch("hermes_cli.config.resolve_agent_turn_limits", return_value={}):
                    scheduler._run_heartbeat_agent("check", "session_1", "weixin", "checklist")
        mock_agent.run_conversation.assert_called_once_with("check")

    def test_run_heartbeat_agent_delivers_non_ok(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler._cfg._light_context = True
        scheduler._cfg._ack_max_chars = 300
        scheduler._cfg._target = "last"
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "You have 3 unread emails!"}
        mock_adapter = MagicMock()
        runner.adapters = {"weixin": mock_adapter}
        mock_run_agent = types.ModuleType("run_agent")
        mock_run_agent.AIAgent = MagicMock(return_value=mock_agent)
        with patch.dict(sys.modules, {"run_agent": mock_run_agent}):
            with patch("hermes_cli.config.load_config", return_value={}):
                with patch("hermes_cli.config.resolve_agent_turn_limits", return_value={}):
                    with patch.object(scheduler, "_deliver_heartbeat_response") as mock_deliver:
                        scheduler._run_heartbeat_agent("check", "session_1", "weixin", "checklist")
                        mock_deliver.assert_called_once_with("You have 3 unread emails!", "session_1", "weixin")

    def test_run_heartbeat_agent_max_iterations_is_3(self):
        runner = self._make_runner()
        scheduler = HeartbeatScheduler(runner)
        scheduler._cfg._light_context = True
        scheduler._cfg._ack_max_chars = 300
        scheduler._cfg._target = "none"
        mock_agent_cls = MagicMock()
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "HEARTBEAT_OK"}
        mock_agent_cls.return_value = mock_agent
        mock_run_agent = types.ModuleType("run_agent")
        mock_run_agent.AIAgent = mock_agent_cls
        with patch.dict(sys.modules, {"run_agent": mock_run_agent}):
            with patch("hermes_cli.config.load_config", return_value={}):
                with patch("hermes_cli.config.resolve_agent_turn_limits", return_value={}):
                    scheduler._run_heartbeat_agent("check", "session_1", "weixin", "checklist")
        _, kwargs = mock_agent_cls.call_args
        assert kwargs["max_iterations"] == 3
