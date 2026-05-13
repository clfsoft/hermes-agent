"""CPA-only runtime boundary helpers.

Hermes user-facing model traffic is intentionally routed through exactly one
provider: CLIProxyAPI (CPA).  CPA owns upstream provider selection, key pools,
OAuth/imported credentials, failover, and protocol conversion.  Hermes may
still keep legacy provider metadata around for migration, diagnostics, or the
CPA management UI, but runtime provider selection must stop here.
"""

from __future__ import annotations

import urllib.parse

CPA_CANONICAL_PROVIDER = "cliproxyapi"
CPA_DISPLAY_NAME = "CLIProxyAPI / CPA"
DEFAULT_CPA_BASE_URL = "http://127.0.0.1:8080/v1"

CPA_PROVIDER_ALIASES = frozenset({
    "",
    "auto",
    "cliproxyapi",
    "cpa",
    "cliproxy",
    "cli-proxy-api",
    "cli_proxy_api",
    "cli proxy api",
})

CPA_API_KEY_ENV_VARS = ("CLIPROXY_API_KEY", "CPA_API_KEY", "OPENAI_API_KEY")
CPA_BASE_URL_ENV_VARS = ("CLIPROXY_BASE_URL", "CPA_BASE_URL")

KNOWN_DIRECT_PROVIDER_HOST_SUFFIXES = (
    "openrouter.ai",
    "api.openai.com",
    "api.anthropic.com",
    "api.x.ai",
    "api.githubcopilot.com",
    "models.github.ai",
    "api.kimi.com",
    "moonshot.ai",
    "moonshot.cn",
    "api.minimax.io",
    "api.minimaxi.com",
    "dashscope.aliyuncs.com",
    "generativelanguage.googleapis.com",
    "api.groq.com",
    "api.deepseek.com",
    "api.mistral.ai",
    "api.cohere.ai",
    "router.huggingface.co",
    "ai-gateway.vercel.sh",
)


class LegacyProviderDisabledError(RuntimeError):
    """Raised when code tries to route Hermes inference outside CPA."""


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def is_cpa_provider(provider: object) -> bool:
    """Return True when *provider* is CPA or an accepted CPA alias."""
    return _norm(provider) in CPA_PROVIDER_ALIASES


def normalize_cpa_provider(provider: object = None, *, strict: bool = True) -> str:
    """Normalize a provider selection to the only runtime provider.

    When ``strict`` is true, non-CPA providers raise a clear boundary error.
    When false, legacy values are silently collapsed to CPA for migration paths
    that must keep old configs running while removing direct-provider routing.
    """
    value = _norm(provider)
    if value in CPA_PROVIDER_ALIASES:
        return CPA_CANONICAL_PROVIDER
    if strict:
        raise LegacyProviderDisabledError(legacy_provider_disabled_message(value))
    return CPA_CANONICAL_PROVIDER


def legacy_provider_disabled_message(provider: object) -> str:
    name = _norm(provider) or "auto"
    return (
        f"Provider '{name}' 已禁用。Hermes 现在只通过 CPA/CLIProxyAPI 接入模型；"
        "请在 CPA WebUI 中配置上游渠道、OAuth、Key 池和故障转移。"
    )


def is_known_direct_provider_base_url(base_url: object) -> bool:
    """Return True for well-known upstream provider endpoints.

    This intentionally blocks obvious direct-provider URLs from being smuggled
    through the CPA provider slot. Custom domains are allowed because users may
    expose their CPA instance behind any reverse proxy hostname.
    """
    raw = str(base_url or "").strip()
    if not raw:
        return False
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host.startswith("bedrock-runtime.") and host.endswith(".amazonaws.com"):
        return True
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in KNOWN_DIRECT_PROVIDER_HOST_SUFFIXES)


def cpa_base_url_boundary_message(base_url: object) -> str:
    value = str(base_url or "").strip() or "(empty)"
    return (
        f"CPA base_url 不能指向已知直连供应商地址：{value}。"
        "Hermes 运行时只能连接 CPA/CLIProxyAPI；请把上游渠道配置到 CPA WebUI，"
        "然后把 Hermes 的 base_url 指向 CPA 的 /v1 或 /anthropic。"
    )


def normalize_cpa_base_url(base_url: str) -> str:
    """Normalize a CPA URL to either its OpenAI or Anthropic surface.

    ``/v1`` and ``/anthropic`` are both valid CPA protocol endpoints. Bare host
    URLs are treated as the OpenAI-compatible ``/v1`` surface.
    """
    value = (base_url or "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1") or path.endswith("/anthropic"):
        return value
    if path.endswith("/v0/management"):
        path = path[: -len("/v0/management")].rstrip("/")
    path = f"{path}/v1" if path else "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def cpa_api_mode_for_base_url(base_url: str) -> str:
    """Return the wire protocol Hermes should use for the CPA endpoint."""
    normalized = (base_url or "").strip().lower().rstrip("/")
    if normalized.endswith("/anthropic"):
        return "anthropic_messages"
    return "chat_completions"


def cpa_management_base_url(base_url: str) -> str:
    """Return the CPA management base URL without protocol suffixes."""
    value = (base_url or "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path.rstrip("/")
    for suffix in ("/v1", "/anthropic", "/v0/management"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")
