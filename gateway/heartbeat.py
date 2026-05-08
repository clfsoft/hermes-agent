"""Heartbeat — periodic agent turns in the main session.

Runs a lightweight agent turn at a configurable interval so the agent can
surface anything that needs attention without spamming the user.

Key differences from cron:
  - Heartbeat runs in the *main* session with conversation context.
  - Multiple checks are batched into a single turn (cheaper).
  - When nothing needs attention, the agent replies HEARTBEAT_OK and the
    response is silently discarded.

Configuration (env vars or config.yaml):
  - HERMES_HEARTBEAT_EVERY   : interval, e.g. "30m", "1h", "0m" to disable (default "0m")
  - HERMES_HEARTBEAT_TARGET  : "last" | "none" | <platform> (default "none")
  - HERMES_HEARTBEAT_PROMPT  : custom prompt body (default built-in)
  - HERMES_HEARTBEAT_ACTIVE_HOURS_START : e.g. "08:00" (default: none)
  - HERMES_HEARTBEAT_ACTIVE_HOURS_END   : e.g. "24:00" (default: none)
  - HERMES_HEARTBEAT_LIGHT_CONTEXT      : "true" to only inject HEARTBEAT.md (default "true")
  - HERMES_HEARTBEAT_ACK_MAX_CHARS      : max chars after HEARTBEAT_OK (default 300)
"""

import asyncio
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_HEARTBEAT_OK_RE = re.compile(r"\bHEARTBEAT_OK\b")
_INTERVAL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(h|m|s)?")
_DISABLE_VALUES = frozenset({"0m", "0", "0s", "0h", "off", "disable", "disabled"})
_TRUTHY_VALUES = frozenset({"true", "1", "yes"})

_DEFAULT_PROMPT = (
    "Read HEARTBEAT.md if it exists (workspace context). "
    "Follow it strictly. Do not infer or repeat old tasks from prior chats. "
    "If nothing needs attention, reply HEARTBEAT_OK."
)

_CONFIG_TTL = 60.0


def _parse_interval(value: str) -> float:
    if not value:
        return 0.0
    stripped = value.strip().lower()
    if stripped in _DISABLE_VALUES:
        return 0.0
    total = 0.0
    for match in _INTERVAL_RE.finditer(stripped):
        num = float(match.group(1))
        unit = (match.group(2) or "m").lower()
        if unit == "h":
            total += num * 3600
        elif unit == "m":
            total += num * 60
        else:
            total += num
    return total


def _is_within_active_hours(start_str: Optional[str], end_str: Optional[str]) -> bool:
    if not start_str or not end_str:
        return True
    try:
        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute
        sp = start_str.strip().split(":", 1)
        ep = end_str.strip().split(":", 1)
        start_minutes = int(sp[0]) * 60 + int(sp[1])
        end_minutes = int(ep[0]) * 60 + int(ep[1])
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes
    except Exception as exc:
        logger.debug("active-hours parse error: %s", exc)
        return True


def load_heartbeat_md() -> Optional[str]:
    try:
        path = get_hermes_home() / "HEARTBEAT.md"
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return content
        return None
    except Exception as exc:
        logger.debug("Could not load HEARTBEAT.md: %s", exc)
        return None


def is_heartbeat_ok_response(text: str, ack_max_chars: int = 300) -> bool:
    if not text:
        return True
    if _HEARTBEAT_OK_RE.search(text):
        stripped = _HEARTBEAT_OK_RE.sub("", text).strip()
        return len(stripped) <= ack_max_chars
    return False


class _HeartbeatConfig:
    """Cached heartbeat configuration — reads config.yaml once per TTL."""

    __slots__ = (
        "_interval", "_target", "_prompt", "_light_context",
        "_ack_max_chars", "_active_hours_start", "_active_hours_end",
        "_model", "_ts",
    )

    def __init__(self):
        self._ts = 0.0
        self._interval: float = 0.0
        self._target: str = "none"
        self._prompt: str = _DEFAULT_PROMPT
        self._light_context: bool = True
        self._ack_max_chars: int = 300
        self._active_hours_start: Optional[str] = None
        self._active_hours_end: Optional[str] = None
        self._model: str = ""

    def refresh(self):
        now = time.monotonic()
        if now - self._ts < _CONFIG_TTL:
            return
        self._ts = now

        hb_cfg: dict = {}
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            hb_cfg = cfg.get("heartbeat") or {}
            if not isinstance(hb_cfg, dict):
                hb_cfg = {}
        except Exception:
            pass

        raw_every = os.getenv("HERMES_HEARTBEAT_EVERY", "0m").strip()
        cfg_every = hb_cfg.get("every")
        if cfg_every:
            raw_every = str(cfg_every)
        self._interval = _parse_interval(raw_every)

        raw_target = os.getenv("HERMES_HEARTBEAT_TARGET", "none").strip().lower()
        cfg_target = hb_cfg.get("target")
        if cfg_target:
            raw_target = str(cfg_target).lower()
        self._target = raw_target

        raw_prompt = os.getenv("HERMES_HEARTBEAT_PROMPT", "").strip()
        cfg_prompt = hb_cfg.get("prompt", "")
        self._prompt = raw_prompt or str(cfg_prompt) if cfg_prompt else _DEFAULT_PROMPT

        raw_lc = os.getenv("HERMES_HEARTBEAT_LIGHT_CONTEXT", "true").strip().lower()
        cfg_lc = hb_cfg.get("lightContext")
        if cfg_lc is not None:
            self._light_context = bool(cfg_lc)
        else:
            self._light_context = raw_lc in _TRUTHY_VALUES

        try:
            self._ack_max_chars = int(os.getenv("HERMES_HEARTBEAT_ACK_MAX_CHARS", "300").strip())
        except (ValueError, TypeError):
            self._ack_max_chars = 300

        raw_start = os.getenv("HERMES_HEARTBEAT_ACTIVE_HOURS_START", "").strip()
        raw_end = os.getenv("HERMES_HEARTBEAT_ACTIVE_HOURS_END", "").strip()
        ah = hb_cfg.get("activeHours") or {}
        if isinstance(ah, dict):
            self._active_hours_start = raw_start or str(ah.get("start", "")) or None
            self._active_hours_end = raw_end or str(ah.get("end", "")) or None
        else:
            self._active_hours_start = raw_start or None
            self._active_hours_end = raw_end or None

        hb_model = os.getenv("HERMES_HEARTBEAT_MODEL", "").strip()
        cfg_model = hb_cfg.get("model", "")
        self._model = hb_model or str(cfg_model) if cfg_model else ""


class _HeartbeatMdCache:
    """mtime-based cache for HEARTBEAT.md — avoids re-reading unchanged files."""

    __slots__ = ("_path", "_mtime", "_content")

    def __init__(self):
        self._path: Optional[Path] = None
        self._mtime: float = 0.0
        self._content: Optional[str] = None

    def get(self) -> Optional[str]:
        try:
            path = get_hermes_home() / "HEARTBEAT.md"
        except Exception:
            return None

        try:
            stat = path.stat()
        except OSError:
            self._mtime = 0.0
            self._content = None
            self._path = None
            return None

        mtime = stat.st_mtime
        if path == self._path and mtime == self._mtime and self._content is not None:
            return self._content

        self._path = path
        self._mtime = mtime
        self._content = load_heartbeat_md()
        return self._content


class HeartbeatScheduler:
    """Manages periodic heartbeat agent turns."""

    def __init__(self, runner):
        self._runner = runner
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_target_session: Optional[str] = None
        self._last_target_platform = None
        self._cfg = _HeartbeatConfig()
        self._md_cache = _HeartbeatMdCache()

    def start(self):
        self._cfg.refresh()
        interval = self._cfg._interval
        if interval <= 0:
            logger.info("Heartbeat disabled (interval=0)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="heartbeat-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Heartbeat scheduler started (interval=%.0fs target=%s lightContext=%s)",
            interval, self._cfg._target, self._cfg._light_context,
        )

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Heartbeat scheduler stopped")

    def update_last_target(self, session_key: str, platform):
        self._last_target_session = session_key
        self._last_target_platform = platform

    def _resolve_target_session(self):
        target = self._cfg._target
        if target == "none":
            return None, None
        if target == "last":
            return self._last_target_session, self._last_target_platform
        runner = self._runner
        for plat, adapter in runner.adapters.items():
            pval = plat.value if hasattr(plat, "value") else str(plat)
            if pval == target:
                home_chat = getattr(adapter, "get_home_chat_id", None)
                if home_chat:
                    chat_id = home_chat()
                else:
                    continue
                if chat_id:
                    from gateway.session import SessionSource
                    from gateway.config import Platform
                    try:
                        plat_enum = Platform(pval)
                    except ValueError:
                        continue
                    source = SessionSource(
                        platform=plat_enum,
                        chat_id=chat_id,
                        chat_name="heartbeat",
                        chat_type="dm",
                        user_id="heartbeat",
                        user_name="Heartbeat",
                    )
                    return runner._session_key_for_source(source), plat
        return None, None

    def _run_loop(self):
        interval = self._cfg._interval
        if interval <= 0:
            return
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=interval)
            if self._stop_event.is_set():
                break
            try:
                self._tick()
            except Exception as exc:
                logger.warning("Heartbeat tick error: %s", exc)

    def _tick(self):
        self._cfg.refresh()

        if not _is_within_active_hours(self._cfg._active_hours_start, self._cfg._active_hours_end):
            logger.debug("Heartbeat skipped — outside active hours")
            return

        heartbeat_md = self._md_cache.get()
        if self._cfg._light_context and heartbeat_md is None:
            logger.debug("Heartbeat skipped — lightContext on but no HEARTBEAT.md")
            return

        session_key, platform = self._resolve_target_session()
        if not session_key:
            logger.debug("Heartbeat skipped — no target session (target=%s)", self._cfg._target)
            return

        runner = self._runner
        if session_key in runner._running_agents:
            logger.debug("Heartbeat skipped — agent running for session %s", session_key)
            return

        prompt = self._cfg._prompt
        logger.info(
            "Heartbeat firing for session %s (platform=%s lightContext=%s)",
            session_key,
            platform.value if hasattr(platform, "value") else platform,
            self._cfg._light_context,
        )

        try:
            self._run_heartbeat_agent(prompt, session_key, platform, heartbeat_md)
        except Exception as exc:
            logger.warning("Heartbeat agent run failed: %s", exc)

    def _run_heartbeat_agent(self, prompt: str, session_key: str, platform, heartbeat_md: Optional[str]):
        from run_agent import AIAgent
        from hermes_cli.config import load_config, resolve_agent_turn_limits

        runner = self._runner
        cfg = load_config()
        model_cfg = cfg.get("model", {})
        model = model_cfg.get("default", "") or runner._model
        base_url = model_cfg.get("base_url", "") or runner._base_url
        api_key_env = model_cfg.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env) or os.environ.get("OPENAI_API_KEY") or ""

        hb_model = self._cfg._model
        if hb_model:
            model = hb_model

        turn_limits = resolve_agent_turn_limits(cfg)

        light = self._cfg._light_context
        ephemeral_prompt = None
        if heartbeat_md and light:
            ephemeral_prompt = f"Heartbeat checklist:\n\n{heartbeat_md}\n"

        agent = AIAgent(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_iterations=3,
            quiet_mode=True,
            skip_context_files=light,
            load_soul_identity=not light,
            skip_memory=True,
            ephemeral_system_prompt=ephemeral_prompt,
            platform="heartbeat",
            enabled_toolsets=["send_message", "web_search", "memory"],
            continuation_policy=turn_limits.get("continuation_policy"),
        )

        try:
            result = agent.run_conversation(prompt)
            response = result.get("final_response", "") if isinstance(result, dict) else str(result)

            if is_heartbeat_ok_response(response, self._cfg._ack_max_chars):
                logger.info("Heartbeat OK — nothing needs attention")
                return

            if response and self._cfg._target != "none":
                self._deliver_heartbeat_response(response, session_key, platform)

        except Exception as exc:
            logger.warning("Heartbeat agent execution failed: %s", exc)

    def _deliver_heartbeat_response(self, response: str, session_key: str, platform):
        runner = self._runner
        adapter = runner.adapters.get(platform)
        if not adapter:
            logger.warning("Heartbeat delivery: no adapter for platform %s", platform)
            return

        try:
            loop = getattr(runner, "_loop", None)
            if loop is None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()

            if loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    adapter.send_text(response, session_key),
                    loop,
                )
            else:
                loop.run_until_complete(adapter.send_text(response, session_key))
            logger.info("Heartbeat delivered %d chars to %s", len(response), session_key)
        except Exception as exc:
            logger.warning("Heartbeat delivery failed: %s", exc)
