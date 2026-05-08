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
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_HEARTBEAT_OK_RE = re.compile(r"\bHEARTBEAT_OK\b")

_DEFAULT_PROMPT = (
    "Read HEARTBEAT.md if it exists (workspace context). "
    "Follow it strictly. Do not infer or repeat old tasks from prior chats. "
    "If nothing needs attention, reply HEARTBEAT_OK."
)


def _parse_interval(value: str) -> float:
    """Parse a human-readable interval like '30m', '1h', '2h30m' into seconds."""
    if not value or value.strip().lower() in ("0m", "0", "0s", "0h", "off", "disable", "disabled"):
        return 0.0
    value = value.strip().lower()
    total = 0.0
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(h|m|s)?", value):
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
    """Check if the current local time is within the active hours window."""
    if not start_str or not end_str:
        return True
    try:
        from datetime import datetime
        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute
        parts = start_str.strip().split(":")
        start_minutes = int(parts[0]) * 60 + int(parts[1])
        parts = end_str.strip().split(":")
        end_minutes = int(parts[0]) * 60 + int(parts[1])
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        else:
            return current_minutes >= start_minutes or current_minutes < end_minutes
    except Exception as exc:
        logger.debug("active-hours parse error: %s", exc)
        return True


def load_heartbeat_md() -> Optional[str]:
    """Load HEARTBEAT.md from HERMES_HOME. Returns None if missing or empty."""
    try:
        path = get_hermes_home() / "HEARTBEAT.md"
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        lines = [l for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
        if not lines:
            return None
        return content
    except Exception as exc:
        logger.debug("Could not load HEARTBEAT.md: %s", exc)
        return None


def is_heartbeat_ok_response(text: str, ack_max_chars: int = 300) -> bool:
    """Check if a heartbeat response is a silent ack (HEARTBEAT_OK).

    HEARTBEAT_OK at the start or end of the reply is treated as an ack.
    The token is stripped and the reply is dropped if the remaining
    content is <= ack_max_chars.
    """
    if not text:
        return True
    if _HEARTBEAT_OK_RE.search(text):
        stripped = _HEARTBEAT_OK_RE.sub("", text).strip()
        if len(stripped) <= ack_max_chars:
            return True
    return False


class HeartbeatScheduler:
    """Manages periodic heartbeat agent turns."""

    def __init__(self, runner):
        self._runner = runner
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_target_session: Optional[str] = None
        self._last_target_platform = None

    @property
    def _interval(self) -> float:
        raw = os.getenv("HERMES_HEARTBEAT_EVERY", "0m").strip()
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            hb_cfg = cfg.get("heartbeat", {})
            if isinstance(hb_cfg, dict):
                cfg_val = hb_cfg.get("every", raw)
                if cfg_val:
                    raw = str(cfg_val)
        except Exception:
            pass
        return _parse_interval(raw)

    @property
    def _target(self) -> str:
        raw = os.getenv("HERMES_HEARTBEAT_TARGET", "none").strip().lower()
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            hb_cfg = cfg.get("heartbeat", {})
            if isinstance(hb_cfg, dict):
                cfg_val = hb_cfg.get("target", raw)
                if cfg_val:
                    raw = str(cfg_val).lower()
        except Exception:
            pass
        return raw

    @property
    def _prompt(self) -> str:
        raw = os.getenv("HERMES_HEARTBEAT_PROMPT", "").strip()
        if raw:
            return raw
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            hb_cfg = cfg.get("heartbeat", {})
            if isinstance(hb_cfg, dict):
                cfg_val = hb_cfg.get("prompt", "")
                if cfg_val:
                    return str(cfg_val)
        except Exception:
            pass
        return _DEFAULT_PROMPT

    @property
    def _light_context(self) -> bool:
        raw = os.getenv("HERMES_HEARTBEAT_LIGHT_CONTEXT", "true").strip().lower()
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            hb_cfg = cfg.get("heartbeat", {})
            if isinstance(hb_cfg, dict):
                cfg_val = hb_cfg.get("lightContext", None)
                if cfg_val is not None:
                    return bool(cfg_val)
        except Exception:
            pass
        return raw in ("true", "1", "yes")

    @property
    def _ack_max_chars(self) -> int:
        raw = os.getenv("HERMES_HEARTBEAT_ACK_MAX_CHARS", "300").strip()
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 300

    @property
    def _active_hours_start(self) -> Optional[str]:
        raw = os.getenv("HERMES_HEARTBEAT_ACTIVE_HOURS_START", "").strip()
        if raw:
            return raw
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            hb_cfg = cfg.get("heartbeat", {})
            if isinstance(hb_cfg, dict):
                ah = hb_cfg.get("activeHours", {})
                if isinstance(ah, dict):
                    start = ah.get("start", "")
                    if start:
                        return str(start)
        except Exception:
            pass
        return None

    @property
    def _active_hours_end(self) -> Optional[str]:
        raw = os.getenv("HERMES_HEARTBEAT_ACTIVE_HOURS_END", "").strip()
        if raw:
            return raw
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            hb_cfg = cfg.get("heartbeat", {})
            if isinstance(hb_cfg, dict):
                ah = hb_cfg.get("activeHours", {})
                if isinstance(ah, dict):
                    end = ah.get("end", "")
                    if end:
                        return str(end)
        except Exception:
            pass
        return None

    def start(self):
        """Start the heartbeat background thread."""
        interval = self._interval
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
            interval, self._target, self._light_context,
        )

    def stop(self):
        """Stop the heartbeat background thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Heartbeat scheduler stopped")

    def update_last_target(self, session_key: str, platform):
        """Track the last session that received a message, for 'last' target."""
        self._last_target_session = session_key
        self._last_target_platform = platform

    def _resolve_target_session(self):
        """Resolve the target session/platform for heartbeat delivery."""
        target = self._target
        if target == "none":
            return None, None
        if target == "last":
            return self._last_target_session, self._last_target_platform
        platform_key = target
        runner = self._runner
        for plat, adapter in runner.adapters.items():
            pval = plat.value if hasattr(plat, "value") else str(plat)
            if pval == platform_key:
                home_chat = adapter.get_home_chat_id() if hasattr(adapter, "get_home_chat_id") else None
                if home_chat:
                    from gateway.session import SessionSource
                    from gateway.config import Platform
                    try:
                        plat_enum = Platform(pval)
                    except ValueError:
                        plat_enum = None
                    if plat_enum:
                        source = SessionSource(
                            platform=plat_enum,
                            chat_id=home_chat,
                            chat_name="heartbeat",
                            chat_type="dm",
                            user_id="heartbeat",
                            user_name="Heartbeat",
                        )
                        session_key = runner._session_key_for_source(source)
                        return session_key, plat
        return None, None

    def _run_loop(self):
        """Background loop that triggers heartbeat turns."""
        interval = self._interval
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
        """Execute one heartbeat tick."""
        if not _is_within_active_hours(self._active_hours_start, self._active_hours_end):
            logger.debug("Heartbeat skipped — outside active hours")
            return

        heartbeat_md = load_heartbeat_md()
        if self._light_context and heartbeat_md is None:
            logger.debug("Heartbeat skipped — lightContext on but no HEARTBEAT.md")
            return

        session_key, platform = self._resolve_target_session()
        if not session_key:
            logger.debug("Heartbeat skipped — no target session (target=%s)", self._target)
            return

        runner = self._runner
        if session_key in runner._running_agents:
            logger.debug("Heartbeat skipped — agent running for session %s", session_key)
            return

        prompt = self._prompt
        logger.info(
            "Heartbeat firing for session %s (platform=%s lightContext=%s)",
            session_key,
            platform.value if hasattr(platform, "value") else platform,
            self._light_context,
        )

        try:
            self._run_heartbeat_agent(prompt, session_key, platform, heartbeat_md)
        except Exception as exc:
            logger.warning("Heartbeat agent run failed: %s", exc)

    def _run_heartbeat_agent(self, prompt: str, session_key: str, platform, heartbeat_md: Optional[str]):
        """Run a lightweight agent turn for the heartbeat."""
        from run_agent import AIAgent
        from hermes_cli.config import load_config, resolve_agent_turn_limits

        runner = self._runner
        cfg = load_config()
        model_cfg = cfg.get("model", {})
        model = model_cfg.get("default", "") or runner._model
        base_url = model_cfg.get("base_url", "") or runner._base_url
        api_key_env = model_cfg.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env) or os.environ.get("OPENAI_API_KEY") or ""

        hb_model = os.getenv("HERMES_HEARTBEAT_MODEL", "").strip()
        if hb_model:
            model = hb_model
        try:
            hb_cfg = cfg.get("heartbeat", {})
            if isinstance(hb_cfg, dict) and hb_cfg.get("model"):
                model = hb_cfg["model"]
        except Exception:
            pass

        turn_limits = resolve_agent_turn_limits(cfg)

        ephemeral_prompt = ""
        if heartbeat_md and self._light_context:
            ephemeral_prompt = f"Heartbeat checklist:\n\n{heartbeat_md}\n"

        agent = AIAgent(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_iterations=10,
            quiet_mode=True,
            skip_context_files=self._light_context,
            load_soul_identity=not self._light_context,
            skip_memory=True,
            ephemeral_system_prompt=ephemeral_prompt if ephemeral_prompt else None,
            platform="heartbeat",
            enabled_toolsets=["send_message", "web_search", "memory"],
            continuation_policy=turn_limits.get("continuation_policy"),
        )

        try:
            result = agent.run_conversation(prompt)
            response = result.get("final_response", "") if isinstance(result, dict) else str(result)

            if is_heartbeat_ok_response(response, self._ack_max_chars):
                logger.info("Heartbeat OK — nothing needs attention")
                return

            if response and self._target != "none":
                self._deliver_heartbeat_response(response, session_key, platform)

        except Exception as exc:
            logger.warning("Heartbeat agent execution failed: %s", exc)

    def _deliver_heartbeat_response(self, response: str, session_key: str, platform):
        """Deliver the heartbeat response to the target platform."""
        runner = self._runner
        adapter = runner.adapters.get(platform)
        if not adapter:
            logger.warning("Heartbeat delivery: no adapter for platform %s", platform)
            return

        try:
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
