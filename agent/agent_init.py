"""AIAgent initialization logic extracted from run_agent.py for modularity."""

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs, urlunparse

logger = logging.getLogger(__name__)

# Import module-level helpers from run_agent
from hermes_constants import get_hermes_home
from hermes_cli.env_loader import load_hermes_dotenv
from hermes_cli.timeouts import (
    get_provider_request_timeout,
    get_provider_stale_timeout,
)
from model_tools import (
    get_tool_definitions,
    check_toolset_requirements,
)
from tools.terminal_tool import cleanup_vm, get_active_env, is_persistent_env
from tools.terminal_tool import (
    set_approval_callback as _set_approval_callback,
    set_sudo_password_callback as _set_sudo_password_callback,
    _get_approval_callback,
    _get_sudo_password_callback,
)
from tools.tool_result_storage import maybe_persist_tool_result, enforce_turn_budget
from tools.interrupt import set_interrupt as _set_interrupt
from tools.browser_tool import cleanup_browser
from agent.memory_manager import StreamingContextScrubber, build_memory_context_block, sanitize_context
from agent.retry_utils import jittered_backoff
from agent.error_classifier import classify_api_error, FailoverReason
from agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY, PLATFORM_HINTS,
    MEMORY_GUIDANCE, SESSION_SEARCH_GUIDANCE, SKILLS_GUIDANCE,
    HERMES_AGENT_HELP_GUIDANCE,
    build_nous_subscription_prompt,
)
from agent.model_metadata import (
    fetch_model_metadata,
    estimate_tokens_rough, estimate_messages_tokens_rough, estimate_request_tokens_rough,
    get_next_probe_tier, parse_context_limit_from_error,
    parse_available_output_tokens_from_error,
    save_context_length, is_local_endpoint,
    query_ollama_num_ctx,
)
from agent.context_compressor import ContextCompressor
from agent.subdirectory_hints import SubdirectoryHintTracker
from agent.prompt_caching import apply_anthropic_cache_control
from agent.prompt_builder import build_skills_system_prompt, build_context_files_prompt, build_environment_hints, load_soul_md, TOOL_USE_ENFORCEMENT_GUIDANCE, TOOL_USE_ENFORCEMENT_MODELS, GOOGLE_MODEL_OPERATIONAL_GUIDANCE, OPENAI_MODEL_EXECUTION_GUIDANCE
from agent.usage_pricing import estimate_usage_cost, normalize_usage
from agent.codex_responses_adapter import (
    _chat_messages_to_responses_input as _codex_chat_messages_to_responses_input,
    _preflight_codex_input_items as _codex_preflight_input_items,
    _preflight_codex_api_kwargs as _codex_preflight_api_kwargs,
    _normalize_codex_response as _codex_normalize_response,
    _derive_responses_function_call_id as _codex_derive_responses_function_call_id,
    _deterministic_call_id as _codex_deterministic_call_id,
    _split_responses_tool_id as _codex_split_responses_tool_id,
    _summarize_user_message_for_log,
)
from agent.display import (
    KawaiiSpinner, build_tool_preview as _build_tool_preview,
    get_cute_tool_message as _get_cute_tool_message_impl,
    _detect_tool_failure,
    get_tool_emoji as _get_tool_emoji,
    format_context_pressure,
    format_context_pressure_gateway,
)
from agent.trajectory import (
    convert_scratchpad_to_think, has_incomplete_scratchpad,
    save_trajectory as _save_trajectory_to_file,
)
from utils import atomic_json_write, base_url_host_matches, base_url_hostname, env_var_enabled, normalize_proxy_url
from hermes_cli.config import cfg_get, load_config
from hermes_cli.cpa_boundary import (
    CPA_CANONICAL_PROVIDER,
    DEFAULT_CPA_BASE_URL,
    LegacyProviderDisabledError,
    cpa_base_url_boundary_message,
    cpa_api_mode_for_base_url,
    is_known_direct_provider_base_url,
    is_cpa_provider,
    normalize_cpa_base_url,
)
from hermes_logging import setup_logging, setup_verbose_logging
from agent.client_factory import _OpenAIProxy, _load_openai_cls

OpenAI = _OpenAIProxy()

_hermes_home = get_hermes_home()
_project_env = Path(__file__).parent.parent / '.env'
_loaded_env_paths = load_hermes_dotenv(hermes_home=_hermes_home, project_env=_project_env)

def init_agent(
    self,
    base_url: str = None,
    api_key: str = None,
    provider: str = None,
    api_mode: str = None,
    acp_command: str = None,
    acp_args: list[str] | None = None,
    command: str = None,
    args: list[str] | None = None,
    model: str = "",
    max_iterations: int = 60,
    max_iterations_with_approval: int = None,
    iteration_extension_step: int = None,
    tool_delay: float = 1.0,
    enabled_toolsets: List[str] = None,
    disabled_toolsets: List[str] = None,
    save_trajectories: bool = False,
    verbose_logging: bool = False,
    quiet_mode: bool = False,
    ephemeral_system_prompt: str = None,
    log_prefix_chars: int = 100,
    log_prefix: str = "",
    providers_allowed: List[str] = None,
    providers_ignored: List[str] = None,
    providers_order: List[str] = None,
    provider_sort: str = None,
    provider_require_parameters: bool = False,
    provider_data_collection: str = None,
    session_id: str = None,
    tool_progress_callback: callable = None,
    tool_start_callback: callable = None,
    tool_complete_callback: callable = None,
    thinking_callback: callable = None,
    reasoning_callback: callable = None,
    clarify_callback: callable = None,
    continuation_callback: callable = None,
    step_callback: callable = None,
    stream_delta_callback: callable = None,
    interim_assistant_callback: callable = None,
    tool_gen_callback: callable = None,
    status_callback: callable = None,
    task_mode: str = None,
    continuation_policy: Dict[str, Any] = None,
    max_tokens: int = None,
    reasoning_config: Dict[str, Any] = None,
    service_tier: str = None,
    request_overrides: Dict[str, Any] = None,
    prefill_messages: List[Dict[str, Any]] = None,
    platform: str = None,
    user_id: str = None,
    user_name: str = None,
    chat_id: str = None,
    chat_name: str = None,
    chat_type: str = None,
    thread_id: str = None,
    gateway_session_key: str = None,
    skip_context_files: bool = False,
    load_soul_identity: bool = False,
    skip_memory: bool = False,
    session_db=None,
    parent_session_id: str = None,
    iteration_budget: "IterationBudget" = None,
    fallback_model: Dict[str, Any] = None,
    credential_pool=None,
    checkpoints_enabled: bool = False,
    checkpoint_max_snapshots: int = 50,
    pass_session_id: bool = False,
):
    """Initialize the AI Agent.

    Args:
        base_url (str): Base URL for the model API (optional)
        api_key (str): API key for authentication (optional, uses env var if not provided)
        provider (str): Provider identifier (optional; used for telemetry/routing hints)
        api_mode (str): API mode override: "chat_completions" or "codex_responses"
        model (str): Model name to use (default: "anthropic/claude-opus-4.6")
        max_iterations (int): Maximum number of tool calling iterations before asking to continue (default: 60)
        max_iterations_with_approval (int): Hard cap after approved continuations (default: max_iterations)
        iteration_extension_step (int): How many iterations each approval adds (default: disabled)
        tool_delay (float): Delay between tool calls in seconds (default: 1.0)
        enabled_toolsets (List[str]): Only enable tools from these toolsets (optional)
        disabled_toolsets (List[str]): Disable tools from these toolsets (optional)
        save_trajectories (bool): Whether to save conversation trajectories to JSONL files (default: False)
        verbose_logging (bool): Enable verbose logging for debugging (default: False)
        quiet_mode (bool): Suppress progress output for clean CLI experience (default: False)
        ephemeral_system_prompt (str): System prompt used during agent execution but NOT saved to trajectories (optional)
        log_prefix_chars (int): Number of characters to show in log previews for tool calls/responses (default: 100)
        log_prefix (str): Prefix to add to all log messages for identification in parallel processing (default: "")
        providers_allowed (List[str]): OpenRouter providers to allow (optional)
        providers_ignored (List[str]): OpenRouter providers to ignore (optional)
        providers_order (List[str]): OpenRouter providers to try in order (optional)
        provider_sort (str): Sort providers by price/throughput/latency (optional)
        session_id (str): Pre-generated session ID for logging (optional, auto-generated if not provided)
        tool_progress_callback (callable): Callback function(tool_name, args_preview) for progress notifications
        clarify_callback (callable): Callback function(question, choices) -> str for interactive user questions.
            Provided by the platform layer (CLI or gateway). If None, the clarify tool returns an error.
        continuation_callback (callable): Callback function(payload) -> bool/str used when the
            agent reaches the current iteration cap and wants approval to continue.
        task_mode (str): Routed task mode for the current turn (``light`` / ``heavy`` / ``inherit``).
        continuation_policy (Dict[str, Any]): Normalized continuation strategy injected by the caller.
        max_tokens (int): Maximum tokens for model responses (optional, uses model default if not set)
        reasoning_config (Dict): OpenRouter reasoning configuration override (e.g. {"effort": "none"} to disable thinking).
            If None, defaults to {"enabled": True, "effort": "medium"} for OpenRouter. Set to disable/customize reasoning.
        prefill_messages (List[Dict]): Messages to prepend to conversation history as prefilled context.
            Useful for injecting a few-shot example or priming the model's response style.
            Example: [{"role": "user", "content": "Hi!"}, {"role": "assistant", "content": "Hello!"}]
            NOTE: Anthropic Sonnet 4.6+ and Opus 4.6+ reject a conversation that ends on an
            assistant-role message (400 error).  For those models use structured outputs or
            output_config.format instead of a trailing-assistant prefill.
        platform (str): The interface platform the user is on (e.g. "cli", "telegram", "discord", "whatsapp").
            Used to inject platform-specific formatting hints into the system prompt.
        skip_context_files (bool): If True, skip auto-injection of SOUL.md, AGENTS.md, and .cursorrules
            into the system prompt. Use this for batch processing and data generation to avoid
            polluting trajectories with user-specific persona or project instructions.
        load_soul_identity (bool): If True, still use ~/.hermes/SOUL.md as the primary
            identity even when skip_context_files=True. Project context files from the cwd
            remain skipped.
    """
    # Lazy imports to avoid circular dependency with run_agent
    from run_agent import (
        _SafeWriter,
        _install_safe_stdio,
        IterationBudget,
        _normalize_continuation_policy,
        _routermint_headers,
        _qwen_portal_headers,
        _openrouter_prewarm_done,
    )

    _install_safe_stdio()

    self.model = model
    self.max_iterations = max_iterations
    self.max_iterations_with_approval = max(
        self.max_iterations,
        int(max_iterations_with_approval or max_iterations),
    )
    _extension_step = iteration_extension_step
    if _extension_step is None and self.max_iterations_with_approval > self.max_iterations:
        _extension_step = self.max_iterations_with_approval - self.max_iterations
    try:
        _extension_step = int(_extension_step or 0)
    except (TypeError, ValueError):
        _extension_step = 0
    self.iteration_extension_step = max(0, _extension_step)
    self._base_max_iterations = self.max_iterations
    self._base_max_iterations_with_approval = self.max_iterations_with_approval
    self._base_iteration_extension_step = self.iteration_extension_step
    self._continuation_policy = _normalize_continuation_policy(continuation_policy)
    # Shared iteration budget — parent creates, children inherit.
    # Consumed by every LLM turn across parent + all subagents.
    self.iteration_budget = iteration_budget or IterationBudget(max_iterations)
    self.tool_delay = tool_delay
    self.save_trajectories = save_trajectories
    self.verbose_logging = verbose_logging
    self.quiet_mode = quiet_mode
    self.ephemeral_system_prompt = ephemeral_system_prompt
    self.platform = platform  # "cli", "telegram", "discord", "whatsapp", etc.
    self._user_id = user_id  # Platform user identifier (gateway sessions)
    self._user_name = user_name
    self._chat_id = chat_id
    self._chat_name = chat_name
    self._chat_type = chat_type
    self._thread_id = thread_id
    self._gateway_session_key = gateway_session_key  # Stable per-chat key (e.g. agent:main:telegram:dm:123)
    # Pluggable print function — CLI replaces this with _cprint so that
    # raw ANSI status lines are routed through prompt_toolkit's renderer
    # instead of going directly to stdout where patch_stdout's StdoutProxy
    # would mangle the escape sequences.  None = use builtins.print.
    self._print_fn = None
    self.background_review_callback = None  # Optional sync callback for gateway delivery
    self.skip_context_files = skip_context_files
    self.load_soul_identity = load_soul_identity
    self.pass_session_id = pass_session_id
    self._credential_pool = credential_pool
    self.log_prefix_chars = log_prefix_chars
    self.log_prefix = f"{log_prefix} " if log_prefix else ""
    provider_name = provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
    legacy_provider_requested = bool(provider_name and not is_cpa_provider(provider_name))
    explicit_cpa_base_url = "" if legacy_provider_requested else (base_url or "")
    # Store effective CPA base URL for feature detection. Hermes runtime is
    # CPA-only; upstream providers live behind CPA, not in this process.
    # If a legacy provider was explicitly passed, ignore its direct base_url
    # to avoid accidentally bypassing CPA.
    self.base_url = normalize_cpa_base_url(
        explicit_cpa_base_url or DEFAULT_CPA_BASE_URL
    )
    if explicit_cpa_base_url and is_known_direct_provider_base_url(explicit_cpa_base_url):
        raise ValueError(cpa_base_url_boundary_message(explicit_cpa_base_url))
    if legacy_provider_requested:
        logger.warning("Ignoring legacy provider %r; Hermes runtime is CPA-only", provider_name)
    self.provider = CPA_CANONICAL_PROVIDER
    self.acp_command = acp_command or command
    self.acp_args = list(acp_args or args or [])
    if api_mode in {"chat_completions", "anthropic_messages"}:
        self.api_mode = api_mode
    else:
        self.api_mode = cpa_api_mode_for_base_url(self.base_url)

    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider as _resolve_cpa_runtime
        _runtime = _resolve_cpa_runtime(
            requested=self.provider,
            explicit_api_key=api_key,
            explicit_base_url=explicit_cpa_base_url,
            target_model=self.model,
        )
        api_key = api_key or _runtime.get("api_key") or "no-key-required"
        self.base_url = _runtime.get("base_url") or self.base_url
        base_url = self.base_url
        if api_mode not in {"chat_completions", "anthropic_messages"}:
            self.api_mode = _runtime.get("api_mode") or cpa_api_mode_for_base_url(self.base_url)
    except LegacyProviderDisabledError:
        raise
    except Exception as exc:
        logger.debug("CPA runtime normalization skipped: %s", exc)
        api_key = api_key or "no-key-required"
        base_url = self.base_url

    # Eagerly warm the transport cache so import errors surface at init,
    # not mid-conversation.  Also validates the api_mode is registered.
    try:
        self._get_transport()
    except Exception:
        logger.debug("_init_agent transport not available", exc_info=True)
        pass  # Non-fatal — transport may not exist for all modes yet

    try:
        from hermes_cli.model_normalize import (
            _AGGREGATOR_PROVIDERS,
            normalize_model_for_provider,
        )

        if self.provider not in _AGGREGATOR_PROVIDERS:
            self.model = normalize_model_for_provider(self.model, self.provider)
    except Exception:
        logger.debug("model normalization failed", exc_info=True)
        pass

    # GPT-5.x models usually require the Responses API path, but some
    # providers have exceptions (for example Copilot's gpt-5-mini still
    # uses chat completions). Also auto-upgrade for direct OpenAI URLs
    # (api.openai.com) since all newer tool-calling models prefer
    # Responses there. ACP runtimes are excluded: CopilotACPClient
    # handles its own routing and does not implement the Responses API
    # surface.
    # When api_mode was explicitly provided, respect it — the user
    # knows what their endpoint supports (#10473).
    # Exception: Azure OpenAI serves gpt-5.x on /chat/completions and
    # does NOT support the Responses API — skip the upgrade for Azure
    # (openai.azure.com), even though it looks OpenAI-compatible.
    if (
        api_mode is None
        and self.api_mode == "chat_completions"
        and not is_cpa_provider(self.provider)
        and self.provider != "copilot-acp"
        and not str(self.base_url or "").lower().startswith("acp://copilot")
        and not str(self.base_url or "").lower().startswith("acp+tcp://")
        and not self._is_azure_openai_url()
        and (
            self._is_direct_openai_url()
            or self._provider_model_requires_responses_api(
                self.model,
                provider=self.provider,
            )
        )
    ):
        self.api_mode = "codex_responses"
        # Invalidate the eager-warmed transport cache — api_mode changed
        # from chat_completions to codex_responses after the warm at __init__.
        if hasattr(self, "_transport_cache"):
            self._transport_cache.clear()

    # Pre-warm OpenRouter model metadata cache in a background thread.
    # fetch_model_metadata() is cached for 1 hour; this avoids a blocking
    # HTTP request on the first API response when pricing is estimated.
    # Use a process-level Event so this thread is only spawned once — a new
    # AIAgent is created for every gateway request, so without the guard
    # each message leaks one OS thread and the process eventually exhausts
    # the system thread limit (RuntimeError: can't start new thread).
    if (self.provider == "openrouter" or self._is_openrouter_url()) and \
            not _openrouter_prewarm_done.is_set():
        _openrouter_prewarm_done.set()
        threading.Thread(
            target=fetch_model_metadata,
            daemon=True,
            name="openrouter-prewarm",
        ).start()

    self.tool_progress_callback = tool_progress_callback
    self.tool_start_callback = tool_start_callback
    self.tool_complete_callback = tool_complete_callback
    self.suppress_status_output = False
    self.thinking_callback = thinking_callback
    self.reasoning_callback = reasoning_callback
    self.clarify_callback = clarify_callback
    self.continuation_callback = continuation_callback
    self.step_callback = step_callback
    self.stream_delta_callback = stream_delta_callback
    self.interim_assistant_callback = interim_assistant_callback
    self.status_callback = status_callback
    self.tool_gen_callback = tool_gen_callback
    self.task_mode = str(task_mode or "").strip().lower()

    
    # Tool execution state — allows _vprint during tool execution
    # even when stream consumers are registered (no tokens streaming then)
    self._executing_tools = False

    # Interrupt mechanism for breaking out of tool loops
    self._interrupt_requested = False
    self._interrupt_message = None  # Optional message that triggered interrupt
    self._execution_thread_id: int | None = None  # Set at run_conversation() start
    self._interrupt_thread_signal_pending = False
    self._client_lock = threading.RLock()

    # /steer mechanism — inject a user note into the next tool result
    # without interrupting the agent. Unlike interrupt(), steer() does
    # NOT set _interrupt_requested; it waits for the current tool batch
    # to finish naturally, then the drain hook appends the text to the
    # last tool result's content so the model sees it on its next
    # iteration. Message-role alternation is preserved (we modify an
    # existing tool message rather than inserting a new user turn).
    self._pending_steer: Optional[str] = None
    self._pending_steer_lock = threading.Lock()

    # Concurrent-tool worker thread tracking.  `_execute_tool_calls_concurrent`
    # runs each tool on its own ThreadPoolExecutor worker — those worker
    # threads have tids distinct from `_execution_thread_id`, so
    # `_set_interrupt(True, _execution_thread_id)` alone does NOT cause
    # `is_interrupted()` inside the worker to return True.  Track the
    # workers here so `interrupt()` / `clear_interrupt()` can fan out to
    # their tids explicitly.
    self._tool_worker_threads: set[int] = set()
    self._tool_worker_threads_lock = threading.Lock()
    
    # Subagent delegation state
    self._delegate_depth = 0        # 0 = top-level agent, incremented for children
    self._active_children = []      # Running child AIAgents (for interrupt propagation)
    self._active_children_lock = threading.Lock()
    
    # Store OpenRouter provider preferences
    self.providers_allowed = providers_allowed
    self.providers_ignored = providers_ignored
    self.providers_order = providers_order
    self.provider_sort = provider_sort
    self.provider_require_parameters = provider_require_parameters
    self.provider_data_collection = provider_data_collection

    # Store toolset filtering options
    self.enabled_toolsets = enabled_toolsets
    self.disabled_toolsets = disabled_toolsets
    
    # Model response configuration
    self.max_tokens = max_tokens  # None = use model default
    self.reasoning_config = reasoning_config  # None = use default (medium for OpenRouter)
    self.service_tier = service_tier
    self.request_overrides = dict(request_overrides or {})
    self.prefill_messages = prefill_messages or []  # Prefilled conversation turns
    self._force_ascii_payload = False
    
    # Anthropic prompt caching: auto-enabled for Claude models on native
    # Anthropic, OpenRouter, and third-party gateways that speak the
    # Anthropic protocol (``api_mode == 'anthropic_messages'``). Reduces
    # input costs by ~75% on multi-turn conversations. Uses system_and_3
    # strategy (4 breakpoints). See ``_anthropic_prompt_cache_policy``
    # for the layout-vs-transport decision.
    self._use_prompt_caching, self._use_native_cache_layout = (
        self._anthropic_prompt_cache_policy()
    )
    # Anthropic supports "5m" (default) and "1h" cache TTL tiers. Read from
    # config.yaml under prompt_caching.cache_ttl; unknown values keep "5m".
    # 1h tier costs 2x on write vs 1.25x for 5m, but amortizes across long
    # sessions with >5-minute pauses between turns (#14971).
    self._cache_ttl = "5m"
    try:
        from hermes_cli.config import load_config as _load_pc_cfg

        _pc_cfg = _load_pc_cfg().get("prompt_caching", {}) or {}
        _ttl = _pc_cfg.get("cache_ttl", "5m")
        if _ttl in ("5m", "1h"):
            self._cache_ttl = _ttl
    except Exception:
        logger.debug("prompt caching TTL config read failed", exc_info=True)
        pass

    # Iteration budget: the LLM is only notified when it actually exhausts
    # the iteration budget (api_call_count >= max_iterations).  At that
    # point we inject ONE message, allow one final API call, and if the
    # model doesn't produce a text response, force a user-message asking
    # it to summarise.  No intermediate pressure warnings — they caused
    # models to "give up" prematurely on complex tasks (#7915).
    self._budget_exhausted_injected = False
    self._budget_grace_call = False
    self._turn_used_tools = False

    # Activity tracking — updated on each API call, tool execution, and
    # stream chunk.  Used by the gateway timeout handler to report what the
    # agent was doing when it was killed, and by the "still working"
    # notifications to show progress.
    self._last_activity_ts: float = time.time()
    self._last_activity_desc: str = "initializing"
    self._current_tool: str | None = None
    self._api_call_count: int = 0

    # Rate limit tracking — updated from x-ratelimit-* response headers
    # after each API call.  Accessed by /usage slash command.
    self._rate_limit_state: Optional["RateLimitState"] = None

    # Centralized logging — agent.log (INFO+) and errors.log (WARNING+)
    # both live under ~/.hermes/logs/.  Idempotent, so gateway mode
    # (which creates a new AIAgent per message) won't duplicate handlers.
    from hermes_logging import setup_logging, setup_verbose_logging
    setup_logging(hermes_home=_hermes_home)

    if self.verbose_logging:
        setup_verbose_logging()
        logger.info("Verbose logging enabled (third-party library logs suppressed)")
    else:
        if self.quiet_mode:
            # In quiet mode (CLI default), suppress all tool/infra log
            # noise on the *console*. The TUI has its own rich display
            # for status; logger INFO/WARNING messages just clutter it.
            # File handlers (agent.log, errors.log) still capture everything.
            for quiet_logger in [
                'tools',               # all tools.* (terminal, browser, web, file, etc.)
                'run_agent',            # agent runner internals
                'trajectory_compressor',
                'cron',                 # scheduler (only relevant in daemon mode)
                'hermes_cli',           # CLI helpers
            ]:
                logging.getLogger(quiet_logger).setLevel(logging.ERROR)
    
    # Internal stream callback (set during streaming TTS).
    # Initialized here so _vprint can reference it before run_conversation.
    self._stream_callback = None
    # Deferred paragraph break flag — set after tool iterations so a
    # single "\n\n" is prepended to the next real text delta.
    self._stream_needs_break = False
    # Stateful scrubber for <memory-context> spans split across stream
    # deltas (#5719).  sanitize_context() alone can't survive chunk
    # boundaries because the block regex needs both tags in one string.
    self._stream_context_scrubber = StreamingContextScrubber()
    # Visible assistant text already delivered through live token callbacks
    # during the current model response. Used to avoid re-sending the same
    # commentary when the provider later returns it as a completed interim
    # assistant message.
    self._current_streamed_assistant_text = ""

    # Optional current-turn user-message override used when the API-facing
    # user message intentionally differs from the persisted transcript
    # (e.g. CLI voice mode adds a temporary prefix for the live call only).
    self._persist_user_message_idx = None
    self._persist_user_message_override = None

    # Cache anthropic image-to-text fallbacks per image payload/URL so a
    # single tool loop does not repeatedly re-run auxiliary vision on the
    # same image history.
    self._anthropic_image_fallback_cache: Dict[str, str] = {}

    # Initialize LLM client via centralized provider router.
    # The router handles auth resolution, base URL, headers, and
    # Codex/Anthropic wrapping for all known providers.
    # raw_codex=True because the main agent needs direct responses.stream()
    # access for Codex Responses API streaming.
    self._anthropic_client = None
    self._is_anthropic_oauth = False

    # Resolve per-provider / per-model request timeout once up front so
    # every client construction path below (Anthropic native, OpenAI-wire,
    # router-based implicit auth) can apply it consistently.  Bedrock
    # Claude uses its own timeout path and is not covered here.
    _provider_timeout = get_provider_request_timeout(self.provider, self.model)

    if self.api_mode == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_client, resolve_anthropic_token
        # Bedrock + Claude → use AnthropicBedrock SDK for full feature parity
        # (prompt caching, thinking budgets, adaptive thinking).
        _is_bedrock_anthropic = self.provider == "bedrock"
        if _is_bedrock_anthropic:
            from agent.anthropic_adapter import build_anthropic_bedrock_client
            _region_match = _BEDROCK_REGION_RE.search(base_url or "")
            _br_region = _region_match.group(1) if _region_match else "us-east-1"
            self._bedrock_region = _br_region
            self._anthropic_client = build_anthropic_bedrock_client(_br_region)
            self._anthropic_api_key = "aws-sdk"
            self._anthropic_base_url = base_url
            self._is_anthropic_oauth = False
            self.api_key = "aws-sdk"
            self.client = None
            self._client_kwargs = {}
            if not self.quiet_mode:
                logger.info(f"🤖 AI Agent initialized with model: {self.model} (AWS Bedrock + AnthropicBedrock SDK, {_br_region})")
        else:
            # Only fall back to ANTHROPIC_TOKEN when the provider is actually Anthropic.
            # Other anthropic_messages providers (MiniMax, Alibaba, etc.) must use their own API key.
            # Falling back would send Anthropic credentials to third-party endpoints (Fixes #1739, #minimax-401).
            _is_native_anthropic = self.provider == "anthropic"
            effective_key = (api_key or resolve_anthropic_token() or "") if _is_native_anthropic else (api_key or "")
            self.api_key = effective_key
            self._anthropic_api_key = effective_key
            self._anthropic_base_url = base_url
            # Only mark the session as OAuth-authenticated when the token
            # genuinely belongs to native Anthropic.  Third-party providers
            # (MiniMax, Kimi, GLM, LiteLLM proxies) that accept the
            # Anthropic protocol must never trip OAuth code paths — doing
            # so injects Claude-Code identity headers and system prompts
            # that cause 401/403 on their endpoints.  Guards #1739 and
            # the third-party identity-injection bug.
            from agent.anthropic_adapter import _is_oauth_token as _is_oat
            self._is_anthropic_oauth = _is_oat(effective_key) if _is_native_anthropic else False
            self._anthropic_client = build_anthropic_client(effective_key, base_url, timeout=_provider_timeout)
            # No OpenAI client needed for Anthropic mode
            self.client = None
            self._client_kwargs = {}
            if not self.quiet_mode:
                logger.info(f"🤖 AI Agent initialized with model: {self.model} (Anthropic native)")
                if effective_key and len(effective_key) > 12:
                    logger.debug(f"🔑 Using token: {effective_key[:8]}...{effective_key[-4:]}")
    elif self.api_mode == "bedrock_converse":
        # AWS Bedrock — uses boto3 directly, no OpenAI client needed.
        # Region is extracted from the base_url or defaults to us-east-1.
        _region_match = _BEDROCK_REGION_RE.search(base_url or "")
        self._bedrock_region = _region_match.group(1) if _region_match else "us-east-1"
        # Guardrail config — read from config.yaml at init time.
        self._bedrock_guardrail_config = None
        try:
            from hermes_cli.config import load_config as _load_br_cfg
            _gr = _load_br_cfg().get("bedrock", {}).get("guardrail", {})
            if _gr.get("guardrail_identifier") and _gr.get("guardrail_version"):
                self._bedrock_guardrail_config = {
                    "guardrailIdentifier": _gr["guardrail_identifier"],
                    "guardrailVersion": _gr["guardrail_version"],
                }
                if _gr.get("stream_processing_mode"):
                    self._bedrock_guardrail_config["streamProcessingMode"] = _gr["stream_processing_mode"]
                if _gr.get("trace"):
                    self._bedrock_guardrail_config["trace"] = _gr["trace"]
        except Exception:
            logger.debug("bedrock guardrail config read failed", exc_info=True)
            pass
        self.client = None
        self._client_kwargs = {}
        if not self.quiet_mode:
            _gr_label = " + Guardrails" if self._bedrock_guardrail_config else ""
            logger.info(f"🤖 AI Agent initialized with model: {self.model} (AWS Bedrock, {self._bedrock_region}{_gr_label})")
    else:
        if api_key:
            # Explicit credentials from CLI/gateway — construct directly.
            # The runtime provider resolver already handled auth for us.
            # Extract query params (e.g. Azure api-version) from base_url
            # and pass via default_query to prevent loss during SDK URL
            # joining (httpx drops query string when joining paths).
            if base_url:
                _parsed_url = urlparse(base_url)
                if _parsed_url.query:
                    _clean_url = urlunparse(_parsed_url._replace(query=""))
                    _query_params = {
                        k: v[0] for k, v in parse_qs(_parsed_url.query).items()
                    }
                    client_kwargs = {
                        "api_key": api_key,
                        "base_url": _clean_url,
                        "default_query": _query_params,
                    }
                else:
                    client_kwargs = {"api_key": api_key, "base_url": base_url}
            else:
                client_kwargs = {"api_key": api_key}
            if _provider_timeout is not None:
                client_kwargs["timeout"] = _provider_timeout
            if self.provider == "copilot-acp":
                client_kwargs["command"] = self.acp_command
                client_kwargs["args"] = self.acp_args
            effective_base = base_url or ""
            if effective_base and base_url_host_matches(effective_base, "openrouter.ai"):
                client_kwargs["default_headers"] = {
                    "HTTP-Referer": "https://hermes-agent.nousresearch.com",
                    "X-OpenRouter-Title": "Hermes Agent",
                    "X-OpenRouter-Categories": "productivity,cli-agent",
                }
            elif effective_base and base_url_host_matches(effective_base, "api.routermint.com"):
                client_kwargs["default_headers"] = _routermint_headers()
            elif effective_base and base_url_host_matches(effective_base, "api.githubcopilot.com"):
                from hermes_cli.models import copilot_default_headers

                client_kwargs["default_headers"] = copilot_default_headers()
            elif effective_base and base_url_host_matches(effective_base, "api.kimi.com"):
                client_kwargs["default_headers"] = {
                    "User-Agent": "claude-code/0.1.0",
                }
            elif effective_base and base_url_host_matches(effective_base, "portal.qwen.ai"):
                client_kwargs["default_headers"] = _qwen_portal_headers()
            elif effective_base and base_url_host_matches(effective_base, "chatgpt.com"):
                from agent.auxiliary_client import _codex_cloudflare_headers
                client_kwargs["default_headers"] = _codex_cloudflare_headers(api_key)
        else:
            # No explicit creds — use the centralized provider router
            from agent.auxiliary_client import resolve_provider_client
            _routed_client, _ = resolve_provider_client(
                self.provider or "auto", model=self.model, raw_codex=True)
            if _routed_client is not None:
                client_kwargs = {
                    "api_key": _routed_client.api_key,
                    "base_url": str(_routed_client.base_url),
                }
                if _provider_timeout is not None:
                    client_kwargs["timeout"] = _provider_timeout
                # Preserve any default_headers the router set
                if hasattr(_routed_client, '_default_headers') and _routed_client._default_headers:
                    client_kwargs["default_headers"] = dict(_routed_client._default_headers)
            else:
                # When the user explicitly chose a non-OpenRouter provider
                # but no credentials were found, fail fast with a clear
                # message instead of silently routing through OpenRouter.
                _explicit = (self.provider or "").strip().lower()
                if _explicit and _explicit not in ("auto", "openrouter", "custom"):
                    # Look up the actual env var name from the provider
                    # config — some providers use non-standard names
                    # (e.g. alibaba → DASHSCOPE_API_KEY, not ALIBABA_API_KEY).
                    _env_hint = f"{_explicit.upper()}_API_KEY"
                    try:
                        from hermes_cli.auth import PROVIDER_REGISTRY
                        _pcfg = PROVIDER_REGISTRY.get(_explicit)
                        if _pcfg and _pcfg.api_key_env_vars:
                            _env_hint = _pcfg.api_key_env_vars[0]
                    except Exception:
                        logger.debug("PROVIDER_REGISTRY lookup failed", exc_info=True)
                        pass
                    raise RuntimeError(
                        f"Provider '{_explicit}' is set in config.yaml but no API key "
                        f"was found. Set the {_env_hint} environment "
                        f"variable, or switch to a different provider with `hermes model`."
                    )
                # No provider configured — reject with a clear message.
                raise RuntimeError(
                    "No LLM provider configured. Run `hermes model` to "
                    "select a provider, or run `hermes setup` for first-time "
                    "configuration."
                )
        
        self._client_kwargs = client_kwargs  # stored for rebuilding after interrupt

        # Enable fine-grained tool streaming for Claude on OpenRouter.
        # Without this, Anthropic buffers the entire tool call and goes
        # silent for minutes while thinking — OpenRouter's upstream proxy
        # times out during the silence.  The beta header makes Anthropic
        # stream tool call arguments token-by-token, keeping the
        # connection alive.
        _effective_base = str(client_kwargs.get("base_url", "")).lower()
        if base_url_host_matches(_effective_base, "openrouter.ai") and "claude" in (self.model or "").lower():
            headers = client_kwargs.get("default_headers") or {}
            existing_beta = headers.get("x-anthropic-beta", "")
            _FINE_GRAINED = "fine-grained-tool-streaming-2025-05-14"
            if _FINE_GRAINED not in existing_beta:
                if existing_beta:
                    headers["x-anthropic-beta"] = f"{existing_beta},{_FINE_GRAINED}"
                else:
                    headers["x-anthropic-beta"] = _FINE_GRAINED
                client_kwargs["default_headers"] = headers

        self.api_key = client_kwargs.get("api_key", "")
        self.base_url = client_kwargs.get("base_url", self.base_url)
        try:
            self.client = self._create_openai_client(client_kwargs, reason="agent_init", shared=True)
            if (
                self.api_mode == "chat_completions"
                and self.provider == "openrouter"
                and self.client.__class__.__module__.startswith("openai")
            ):
                try:
                    from agent.auxiliary_client import resolve_provider_client as _resolve_provider_client
                    routed_client, _ = _resolve_provider_client(
                        self.provider,
                        model=self.model,
                        api_key=self.api_key,
                        base_url=self.base_url,
                    )
                    if routed_client is not None:
                        self.client = routed_client
                except Exception:
                    logger.debug("resolve_provider_client fallback failed", exc_info=True)
                    pass
            if not self.quiet_mode:
                logger.info(f"🤖 AI Agent initialized with model: {self.model}")
                if base_url:
                    logger.info(f"🔗 Using custom base URL: {base_url}")
                # Always show API key info (masked) for debugging auth issues
                key_used = client_kwargs.get("api_key", "none")
                if key_used and key_used != "dummy-key" and len(key_used) > 12:
                    logger.debug(f"🔑 Using API key: {key_used[:8]}...{key_used[-4:]}")
                else:
                    logger.warning(f"⚠️  Warning: API key appears invalid or missing (got: '{key_used[:20] if key_used else 'none'}...')")
        except Exception as e:
            raise RuntimeError(f"Failed to initialize OpenAI client: {e}")
    
    # Provider fallback chain — ordered list of backup providers tried
    # when the primary is exhausted (rate-limit, overload, connection
    # failure).  Supports both legacy single-dict ``fallback_model`` and
    # new list ``fallback_providers`` format.
    self._fallback_chain = self._normalize_fallback_chain(fallback_model)
    self._fallback_index = 0
    self._fallback_activated = False
    # Legacy attribute kept for backward compat (tests, external callers)
    self._fallback_model = self._fallback_chain[0] if self._fallback_chain else None
    if self._fallback_chain and not self.quiet_mode:
        if len(self._fallback_chain) == 1:
            fb = self._fallback_chain[0]
            logger.info(f"🔄 Fallback model: {fb['model']} ({fb['provider']})")
        else:
            logger.info(f"🔄 Fallback chain ({len(self._fallback_chain)} providers): " +
                  " → ".join(f"{f['model']} ({f['provider']})" for f in self._fallback_chain))

    # Get available tools with filtering
    self.tools = get_tool_definitions(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        quiet_mode=self.quiet_mode,
    )
    
    # Show tool configuration and store valid tool names for validation
    self.valid_tool_names = set()
    if self.tools:
        self.valid_tool_names = {tool["function"]["name"] for tool in self.tools}
        tool_names = sorted(self.valid_tool_names)
        if not self.quiet_mode:
            logger.info(f"🛠️  Loaded {len(self.tools)} tools: {', '.join(tool_names)}")
            
            # Show filtering info if applied
            if enabled_toolsets:
                logger.info(f"   ✅ Enabled toolsets: {', '.join(enabled_toolsets)}")
            if disabled_toolsets:
                logger.info(f"   ❌ Disabled toolsets: {', '.join(disabled_toolsets)}")
    elif not self.quiet_mode:
        logger.info("🛠️  No tools loaded (all tools filtered out or unavailable)")
    
    # Check tool requirements
    if self.tools and not self.quiet_mode:
        requirements = check_toolset_requirements()
        missing_reqs = [name for name, available in requirements.items() if not available]
        if missing_reqs:
            logger.warning(f"⚠️  Some tools may not work due to missing requirements: {missing_reqs}")
    
    # Show trajectory saving status
    if self.save_trajectories and not self.quiet_mode:
        logger.info("📝 Trajectory saving enabled")
    
    # Show ephemeral system prompt status
    if self.ephemeral_system_prompt and not self.quiet_mode:
        prompt_preview = self.ephemeral_system_prompt[:60] + "..." if len(self.ephemeral_system_prompt) > 60 else self.ephemeral_system_prompt
        logger.info(f"🔒 Ephemeral system prompt: '{prompt_preview}' (not saved to trajectories)")
    
    # Show prompt caching status
    if self._use_prompt_caching and not self.quiet_mode:
        if self._use_native_cache_layout and self.provider == "anthropic":
            source = "native Anthropic"
        elif self._use_native_cache_layout:
            source = "Anthropic-compatible endpoint"
        else:
            source = "Claude via OpenRouter"
        logger.info(f"💾 Prompt caching: ENABLED ({source}, {self._cache_ttl} TTL)")
    
    # Session logging setup - auto-save conversation trajectories for debugging
    self.session_start = datetime.now()
    if session_id:
        # Use provided session ID (e.g., from CLI)
        self.session_id = session_id
    else:
        # Generate a new session ID
        timestamp_str = self.session_start.strftime("%Y%m%d_%H%M%S")
        short_uuid = uuid.uuid4().hex[:6]
        self.session_id = f"{timestamp_str}_{short_uuid}"
    
    # Session logs go into ~/.hermes/sessions/ alongside gateway sessions
    hermes_home = get_hermes_home()
    self.logs_dir = hermes_home / "sessions"
    self.logs_dir.mkdir(parents=True, exist_ok=True)
    self.session_log_file = self.logs_dir / f"session_{self.session_id}.json"
    
    # Track conversation messages for session logging
    self._session_messages: List[Dict[str, Any]] = []
    self._memory_write_origin = "assistant_tool"
    self._memory_write_context = "foreground"
    
    # Cached system prompt -- built once per session, only rebuilt on compression
    self._cached_system_prompt: Optional[str] = None
    
    # Filesystem checkpoint manager (transparent — not a tool)
    from tools.checkpoint_manager import CheckpointManager
    self._checkpoint_mgr = CheckpointManager(
        enabled=checkpoints_enabled,
        max_snapshots=checkpoint_max_snapshots,
    )
    
    # SQLite session store (optional -- provided by CLI or gateway)
    self._session_db = session_db
    self._parent_session_id = parent_session_id
    self._last_flushed_db_idx = 0  # tracks DB-write cursor to prevent duplicate writes
    if self._session_db:
        try:
            self._session_db.create_session(
                session_id=self.session_id,
                source=self.platform or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
                model=self.model,
                model_config={
                    "max_iterations": self.max_iterations,
                    "reasoning_config": reasoning_config,
                    "max_tokens": max_tokens,
                },
                user_id=None,
                parent_session_id=self._parent_session_id,
            )
        except Exception as e:
            # Transient SQLite lock contention (e.g. CLI and gateway writing
            # concurrently) must NOT permanently disable session_search for
            # this agent.  Keep _session_db alive — subsequent message
            # flushes and session_search calls will still work once the
            # lock clears.  The session row may be missing from the index
            # for this run, but that is recoverable (flushes upsert rows).
            logger.warning(
                "Session DB create_session failed (session_search still available): %s", e
            )
    
    # In-memory todo list for task planning (one per agent/session)
    from tools.todo_tool import TodoStore
    self._todo_store = TodoStore()
    
    # Load config once for memory, skills, and compression sections
    try:
        from hermes_cli.config import load_config as _load_agent_config
        _agent_cfg = _load_agent_config()
    except Exception:
        _agent_cfg = {}
    try:
        from agent.sleep_mode import resolve_sleep_mode as _resolve_sleep_mode
        self._sleep_mode = _resolve_sleep_mode(_agent_cfg)
    except Exception:
        self._sleep_mode = {"enabled": True}
    # Cache only the derived auxiliary compression context override that is
    # needed later by the startup feasibility check.  Avoid exposing a
    # broad pseudo-public config object on the agent instance.
    self._aux_compression_context_length_config = None

    # Memory provider prefetch budget (<memory-context> injection), English logs.
    self._memory_prefetch_char_limit = 6000
    self._memory_prefetch_debug = False
    self._memory_prefetch_snapshot = True
    self._memory_prefetch_snapshot_preview_chars = 400
    self._memory_episodic_trace_chars = 0

    # Persistent memory (MEMORY.md + USER.md) -- loaded from disk
    self._memory_store = None
    self._memory_enabled = False
    self._user_profile_enabled = False
    self._memory_nudge_interval = 10
    self._turns_since_memory = 0
    self._iters_since_skill = 0
    if not skip_memory:
        try:
            mem_config = _agent_cfg.get("memory", {})
            self._memory_enabled = mem_config.get("memory_enabled", False)
            self._user_profile_enabled = mem_config.get("user_profile_enabled", False)
            self._memory_nudge_interval = int(mem_config.get("nudge_interval", 10))
            sleep_interval = self._sleep_mode.get("memory_review_interval")
            if sleep_interval is not None:
                self._memory_nudge_interval = int(sleep_interval)
            if self._memory_enabled or self._user_profile_enabled:
                from tools.memory_tool import MemoryStore
                self._memory_store = MemoryStore(
                    memory_char_limit=mem_config.get("memory_char_limit", 2200),
                    user_char_limit=mem_config.get("user_char_limit", 1375),
                )
                self._memory_store.load_from_disk()
            try:
                self._memory_prefetch_char_limit = int(
                    mem_config.get("prefetch_char_limit", self._memory_prefetch_char_limit)
                )
            except (TypeError, ValueError):
                self._memory_prefetch_char_limit = 6000
            self._memory_prefetch_debug = bool(mem_config.get("prefetch_debug", False))
            self._memory_prefetch_snapshot = bool(mem_config.get("prefetch_snapshot", True))
            try:
                self._memory_prefetch_snapshot_preview_chars = max(
                    0,
                    int(mem_config.get("prefetch_snapshot_preview_chars", 400)),
                )
            except (TypeError, ValueError):
                self._memory_prefetch_snapshot_preview_chars = 400
            try:
                self._memory_episodic_trace_chars = max(
                    0,
                    int(mem_config.get("episodic_trace_chars", 0)),
                )
            except (TypeError, ValueError):
                self._memory_episodic_trace_chars = 0
        except Exception:
            logger.debug("memory config initialization failed", exc_info=True)
            pass  # Memory is optional -- don't break agent init
    


    # Memory provider plugin (external — one at a time, alongside built-in)
    # Reads memory.provider from config to select which plugin to activate.
    self._memory_manager = None
    if not skip_memory:
        try:
            _mem_provider_name = mem_config.get("provider", "") if mem_config else ""

            if _mem_provider_name:
                from agent.memory_manager import MemoryManager as _MemoryManager
                from plugins.memory import load_memory_provider as _load_mem
                self._memory_manager = _MemoryManager()
                _mp = _load_mem(_mem_provider_name)
                if _mp and _mp.is_available():
                    self._memory_manager.add_provider(_mp)
                if self._memory_manager.providers:
                    _init_kwargs = {
                        "session_id": self.session_id,
                        "platform": platform or "cli",
                        "hermes_home": str(get_hermes_home()),
                        "agent_context": "primary",
                    }
                    # Thread session title for memory provider scoping
                    # (e.g. honcho uses this to derive chat-scoped session keys)
                    if self._session_db:
                        try:
                            _st = self._session_db.get_session_title(self.session_id)
                            if _st:
                                _init_kwargs["session_title"] = _st
                        except Exception:
                            logger.debug("session title read failed", exc_info=True)
                            pass
                    # Thread gateway user identity for per-user memory scoping
                    if self._user_id:
                        _init_kwargs["user_id"] = self._user_id
                    if self._user_name:
                        _init_kwargs["user_name"] = self._user_name
                    if self._chat_id:
                        _init_kwargs["chat_id"] = self._chat_id
                    if self._chat_name:
                        _init_kwargs["chat_name"] = self._chat_name
                    if self._chat_type:
                        _init_kwargs["chat_type"] = self._chat_type
                    if self._thread_id:
                        _init_kwargs["thread_id"] = self._thread_id
                    # Thread gateway session key for stable per-chat Honcho session isolation
                    if self._gateway_session_key:
                        _init_kwargs["gateway_session_key"] = self._gateway_session_key
                    # Profile identity for per-profile provider scoping
                    try:
                        from hermes_cli.profiles import get_active_profile_name
                        _profile = get_active_profile_name()
                        _init_kwargs["agent_identity"] = _profile
                        _init_kwargs["agent_workspace"] = "hermes"
                    except Exception:
                        logger.debug("profile identity read failed", exc_info=True)
                        pass
                    self._memory_manager.initialize_all(**_init_kwargs)
                    logger.info("Memory provider '%s' activated", _mem_provider_name)
                else:
                    logger.debug("Memory provider '%s' not found or not available", _mem_provider_name)
                    self._memory_manager = None
        except Exception as _mpe:
            logger.warning("Memory provider plugin init failed: %s", _mpe)
            self._memory_manager = None

    # Inject memory provider tool schemas into the tool surface.
    # Skip tools whose names already exist (plugins may register the
    # same tools via ctx.register_tool(), which lands in self.tools
    # through get_tool_definitions()).  Duplicate function names cause
    # 400 errors on providers that enforce unique names (e.g. Xiaomi
    # MiMo via Nous Portal).
    if self._memory_manager and self.tools is not None:
        _existing_tool_names = {
            t.get("function", {}).get("name")
            for t in self.tools
            if isinstance(t, dict)
        }
        for _schema in self._memory_manager.get_all_tool_schemas():
            _tname = _schema.get("name", "")
            if _tname and _tname in _existing_tool_names:
                continue  # already registered via plugin path
            _wrapped = {"type": "function", "function": _schema}
            self.tools.append(_wrapped)
            if _tname:
                self.valid_tool_names.add(_tname)
                _existing_tool_names.add(_tname)

    # Skills config: nudge interval for skill creation reminders
    self._skill_nudge_interval = 10
    try:
        skills_config = _agent_cfg.get("skills", {})
        self._skill_nudge_interval = int(skills_config.get("creation_nudge_interval", 10))
        sleep_interval = self._sleep_mode.get("skill_review_interval")
        if sleep_interval is not None:
            self._skill_nudge_interval = int(sleep_interval)
    except Exception:
        logger.debug("skills config read failed", exc_info=True)
        pass

    # Tool-use enforcement config: "auto" (default — matches hardcoded
    # model list), true (always), false (never), or list of substrings.
    _agent_section = _agent_cfg.get("agent", {})
    if not isinstance(_agent_section, dict):
        _agent_section = {}
    self._tool_use_enforcement = _agent_section.get("tool_use_enforcement", "auto")

    # App-level API retry count (wraps each model API call).  Default 3,
    # overridable via agent.api_max_retries in config.yaml.  See #11616.
    try:
        _raw_api_retries = _agent_section.get("api_max_retries", 3)
        _api_retries = int(_raw_api_retries)
        if _api_retries < 1:
            _api_retries = 1  # 1 = no retry (single attempt)
    except (TypeError, ValueError):
        _api_retries = 3
    self._api_max_retries = _api_retries

    # Initialize context compressor for automatic context management
    # Compresses conversation when approaching model's context limit
    # Configuration via config.yaml (compression section)
    _compression_cfg = _agent_cfg.get("compression", {})
    if not isinstance(_compression_cfg, dict):
        _compression_cfg = {}
    compression_threshold = float(_compression_cfg.get("threshold", 0.50))
    compression_enabled = str(_compression_cfg.get("enabled", True)).lower() in ("true", "1", "yes")
    compression_target_ratio = float(_compression_cfg.get("target_ratio", 0.20))
    compression_protect_last = int(_compression_cfg.get("protect_last_n", 20))

    # Read optional explicit context_length override for the auxiliary
    # compression model. Custom endpoints often cannot report this via
    # /models, so the startup feasibility check needs the config hint.
    try:
        _aux_cfg = cfg_get(_agent_cfg, "auxiliary", "compression", default={})
    except Exception:
        _aux_cfg = {}
    if isinstance(_aux_cfg, dict):
        _aux_context_config = _aux_cfg.get("context_length")
    else:
        _aux_context_config = None
    if _aux_context_config is not None:
        try:
            _aux_context_config = int(_aux_context_config)
        except (TypeError, ValueError):
            _aux_context_config = None
    self._aux_compression_context_length_config = _aux_context_config

    # Read explicit context_length override from model config
    _model_cfg = _agent_cfg.get("model", {})
    if isinstance(_model_cfg, dict):
        _config_context_length = _model_cfg.get("context_length")
    else:
        _config_context_length = None
    if _config_context_length is not None:
        try:
            _config_context_length = int(_config_context_length)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid model.context_length in config.yaml: %r — "
                "must be a plain integer (e.g. 256000, not '256K'). "
                "Falling back to auto-detection.",
                _config_context_length,
            )
            logger.warning(
                "\n⚠ Invalid model.context_length in config.yaml: %r\n"
                "  Must be a plain integer (e.g. 256000, not '256K').\n"
                "  Falling back to auto-detected context window.",
                _config_context_length,
            )
            _config_context_length = None

    # Resolve custom_providers list once for reuse below (startup
    # context-length override and plugin context-engine init).
    try:
        from hermes_cli.config import get_compatible_custom_providers
        _custom_providers = get_compatible_custom_providers(_agent_cfg)
    except Exception:
        _custom_providers = _agent_cfg.get("custom_providers")
        if not isinstance(_custom_providers, list):
            _custom_providers = []

    # Check custom_providers per-model context_length
    if _config_context_length is None and _custom_providers:
        try:
            from hermes_cli.config import get_custom_provider_context_length
            _cp_ctx_resolved = get_custom_provider_context_length(
                model=self.model,
                base_url=self.base_url,
                custom_providers=_custom_providers,
            )
            if _cp_ctx_resolved:
                _config_context_length = int(_cp_ctx_resolved)
        except Exception:
            _cp_ctx_resolved = None

        # Surface a clear warning if the user set a context_length but it
        # wasn't a valid positive int — the helper silently skips those.
        if _config_context_length is None:
            _target = self.base_url.rstrip("/") if self.base_url else ""
            for _cp_entry in _custom_providers:
                if not isinstance(_cp_entry, dict):
                    continue
                _cp_url = (_cp_entry.get("base_url") or "").rstrip("/")
                if _target and _cp_url == _target:
                    _cp_models = _cp_entry.get("models", {})
                    if isinstance(_cp_models, dict):
                        _cp_model_cfg = _cp_models.get(self.model, {})
                        if isinstance(_cp_model_cfg, dict):
                            _cp_ctx = _cp_model_cfg.get("context_length")
                            if _cp_ctx is not None:
                                try:
                                    _parsed = int(_cp_ctx)
                                    if _parsed <= 0:
                                        raise ValueError
                                except (TypeError, ValueError):
                                    logger.warning(
                                        "Invalid context_length for model %r in "
                                        "custom_providers: %r — must be a positive "
                                        "integer (e.g. 256000, not '256K'). "
                                        "Falling back to auto-detection.",
                                        self.model, _cp_ctx,
                                    )
                                    logger.warning(
                                        "\n⚠ Invalid context_length for model %r in custom_providers: %r\n"
                                        "  Must be a positive integer (e.g. 256000, not '256K').\n"
                                        "  Falling back to auto-detected context window.",
                                        self.model, _cp_ctx,
                                    )
                    break

    # Persist for reuse on switch_model / fallback activation. Must come
    # AFTER the custom_providers branch so per-model overrides aren't lost.
    self._config_context_length = _config_context_length

    self._ensure_lmstudio_runtime_loaded(_config_context_length)



    # Select context engine: config-driven (like memory providers).
    # 1. Check config.yaml context.engine setting
    # 2. Check plugins/context_engine/<name>/ directory (repo-shipped)
    # 3. Check general plugin system (user-installed plugins)
    # 4. Fall back to built-in ContextCompressor
    _selected_engine = None
    _engine_name = "compressor"  # default
    try:
        _ctx_cfg = _agent_cfg.get("context", {}) if isinstance(_agent_cfg, dict) else {}
        _engine_name = _ctx_cfg.get("engine", "compressor") or "compressor"
    except Exception:
        logger.debug("context engine config read failed", exc_info=True)
        pass

    if _engine_name != "compressor":
        # Try loading from plugins/context_engine/<name>/
        try:
            from plugins.context_engine import load_context_engine
            _selected_engine = load_context_engine(_engine_name)
        except Exception as _ce_load_err:
            logger.debug("Context engine load from plugins/context_engine/: %s", _ce_load_err)

        # Try general plugin system as fallback
        if _selected_engine is None:
            try:
                from hermes_cli.plugins import get_plugin_context_engine
                _candidate = get_plugin_context_engine()
                if _candidate and _candidate.name == _engine_name:
                    _selected_engine = _candidate
            except Exception:
                logger.debug("plugin context engine load failed", exc_info=True)
                pass

        if _selected_engine is None:
            logger.warning(
                "Context engine '%s' not found — falling back to built-in compressor",
                _engine_name,
            )
    # else: config says "compressor" — use built-in, don't auto-activate plugins

    if _selected_engine is not None:
        self.context_compressor = _selected_engine
        # Resolve context_length for plugin engines — mirrors switch_model() path
        from agent.model_metadata import get_model_context_length
        _plugin_ctx_len = get_model_context_length(
            self.model,
            base_url=self.base_url,
            api_key=getattr(self, "api_key", ""),
            config_context_length=_config_context_length,
            provider=self.provider,
            custom_providers=_custom_providers,
        )
        self.context_compressor.update_model(
            model=self.model,
            context_length=_plugin_ctx_len,
            base_url=self.base_url,
            api_key=getattr(self, "api_key", ""),
            provider=self.provider,
        )
        if not self.quiet_mode:
            logger.info("Using context engine: %s", _selected_engine.name)
    else:
        self.context_compressor = ContextCompressor(
            model=self.model,
            threshold_percent=compression_threshold,
            protect_first_n=3,
            protect_last_n=compression_protect_last,
            summary_target_ratio=compression_target_ratio,
            summary_model_override=None,
            quiet_mode=self.quiet_mode,
            base_url=self.base_url,
            api_key=getattr(self, "api_key", ""),
            config_context_length=_config_context_length,
            provider=self.provider,
            api_mode=self.api_mode,
        )
    self.compression_enabled = compression_enabled

    # Reject models whose context window is below the minimum required
    # for reliable tool-calling workflows (64K tokens).
    from agent.model_metadata import MINIMUM_CONTEXT_LENGTH
    _ctx = getattr(self.context_compressor, "context_length", 0)
    if _ctx and _ctx < MINIMUM_CONTEXT_LENGTH:
        raise ValueError(
            f"Model {self.model} has a context window of {_ctx:,} tokens, "
            f"which is below the minimum {MINIMUM_CONTEXT_LENGTH:,} required "
            f"by Hermes Agent.  Choose a model with at least "
            f"{MINIMUM_CONTEXT_LENGTH // 1000}K context, or set "
            f"model.context_length in config.yaml to override."
        )

    # Inject context engine tool schemas (e.g. lcm_grep, lcm_describe, lcm_expand)
    self._context_engine_tool_names: set = set()
    if hasattr(self, "context_compressor") and self.context_compressor and self.tools is not None:
        for _schema in self.context_compressor.get_tool_schemas():
            _wrapped = {"type": "function", "function": _schema}
            self.tools.append(_wrapped)
            _tname = _schema.get("name", "")
            if _tname:
                self.valid_tool_names.add(_tname)
                self._context_engine_tool_names.add(_tname)

    # Notify context engine of session start
    if hasattr(self, "context_compressor") and self.context_compressor:
        try:
            self.context_compressor.on_session_start(
                self.session_id,
                hermes_home=str(get_hermes_home()),
                platform=self.platform or "cli",
                model=self.model,
                context_length=getattr(self.context_compressor, "context_length", 0),
            )
        except Exception as _ce_err:
            logger.debug("Context engine on_session_start: %s", _ce_err)

    self._subdirectory_hints = SubdirectoryHintTracker(
        working_dir=os.getenv("TERMINAL_CWD") or None,
    )
    self._user_turn_count = 0
    self._context_pressure_warned_at = 0.0

    # Cumulative token usage for the session
    self.session_prompt_tokens = 0
    self.session_completion_tokens = 0
    self.session_total_tokens = 0
    self.session_api_calls = 0
    self.session_input_tokens = 0
    self.session_output_tokens = 0
    self.session_cache_read_tokens = 0
    self.session_cache_write_tokens = 0
    self.session_reasoning_tokens = 0
    self.session_estimated_cost_usd = 0.0
    self.session_cost_status = "unknown"
    self.session_cost_source = "none"
    
    # ── Ollama num_ctx injection ──
    # Ollama defaults to 2048 context regardless of the model's capabilities.
    # When running against an Ollama server, detect the model's max context
    # and pass num_ctx on every chat request so the full window is used.
    # User override: set model.ollama_num_ctx in config.yaml to cap VRAM use.
    self._ollama_num_ctx: int | None = None
    _ollama_num_ctx_override = None
    if isinstance(_model_cfg, dict):
        _ollama_num_ctx_override = _model_cfg.get("ollama_num_ctx")
    if _ollama_num_ctx_override is not None:
        try:
            self._ollama_num_ctx = int(_ollama_num_ctx_override)
        except (TypeError, ValueError):
            logger.debug("Invalid ollama_num_ctx config value: %r", _ollama_num_ctx_override)
    if self._ollama_num_ctx is None and self.base_url and is_local_endpoint(self.base_url):
        try:
            _detected = query_ollama_num_ctx(self.model, self.base_url, api_key=self.api_key or "")
            if _detected and _detected > 0:
                self._ollama_num_ctx = _detected
        except Exception as exc:
            logger.debug("Ollama num_ctx detection failed: %s", exc)
    if self._ollama_num_ctx and not self.quiet_mode:
        logger.info(
            "Ollama num_ctx: will request %d tokens (model max from /api/show)",
            self._ollama_num_ctx,
        )

    if not self.quiet_mode:
        if compression_enabled:
            logger.info(f"📊 Context limit: {self.context_compressor.context_length:,} tokens (compress at {int(compression_threshold*100)}% = {self.context_compressor.threshold_tokens:,})")
        else:
            logger.info(f"📊 Context limit: {self.context_compressor.context_length:,} tokens (auto-compression disabled)")

    # Check immediately for interactive callers so they see the warning at
    # startup. Quiet/background callers without a status callback defer the
    # feasibility probe until the first run_conversation() to avoid paying
    # the auxiliary-provider detection cost during bulk cache warmups.
    self._compression_warning = None
    self._compression_warning_checked = False
    if self.status_callback or not self.quiet_mode:
        self._check_compression_model_feasibility()
        self._compression_warning_checked = True

    # Snapshot primary runtime for per-turn restoration.  When fallback
    # activates during a turn, the next turn restores these values so the
    # preferred model gets a fresh attempt each time.  Uses a single dict
    # so new state fields are easy to add without N individual attributes.
    _cc = self.context_compressor
    self._primary_runtime = {
        "model": self.model,
        "provider": self.provider,
        "base_url": self.base_url,
        "api_mode": self.api_mode,
        "api_key": getattr(self, "api_key", ""),
        "client_kwargs": dict(self._client_kwargs),
        "use_prompt_caching": self._use_prompt_caching,
        "use_native_cache_layout": self._use_native_cache_layout,
        # Context engine state that _try_activate_fallback() overwrites.
        # Use getattr for model/base_url/api_key/provider since plugin
        # engines may not have these (they're ContextCompressor-specific).
        "compressor_model": getattr(_cc, "model", self.model),
        "compressor_base_url": getattr(_cc, "base_url", self.base_url),
        "compressor_api_key": getattr(_cc, "api_key", ""),
        "compressor_provider": getattr(_cc, "provider", self.provider),
        "compressor_context_length": _cc.context_length,
        "compressor_threshold_tokens": _cc.threshold_tokens,
    }
    if self.api_mode == "anthropic_messages":
        self._primary_runtime.update({
            "anthropic_api_key": self._anthropic_api_key,
            "anthropic_base_url": self._anthropic_base_url,
            "is_anthropic_oauth": self._is_anthropic_oauth,
        })
