"""CPA-only runtime provider resolution for Hermes.

Hermes now routes all model traffic through the embedded/external CLIProxyAPI
(CPA) OpenAI-compatible surface.  Legacy direct-provider resolution is
intentionally disabled: configure upstream providers inside CPA instead.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from hermes_cli.cpa_boundary import (
    CPA_PROVIDER_ALIASES,
    DEFAULT_CPA_BASE_URL,
    LegacyProviderDisabledError,
    cpa_base_url_boundary_message,
    cpa_api_mode_for_base_url,
    is_known_direct_provider_base_url,
    is_cpa_provider,
    normalize_cpa_base_url,
)
from hermes_cli.config import load_config
from utils import base_url_hostname

def _get_model_config() -> Dict[str, Any]:
    cfg = load_config()
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        return {"default": model_cfg}
    if isinstance(model_cfg, dict):
        return dict(model_cfg)
    return {}


def resolve_requested_provider(requested: Optional[str] = None) -> str:
    if requested and requested.strip():
        provider = requested.strip().lower()
    else:
        provider = str(_get_model_config().get("provider") or "cliproxyapi").strip().lower()
    if not provider or is_cpa_provider(provider):
        return "cliproxyapi"
    raise LegacyProviderDisabledError(
        f"Provider '{provider}' 已禁用。Hermes 现在只通过 CPA/CLIProxyAPI 接入模型；请在 CPA WebUI 中配置上游渠道。"
    )


def resolve_runtime_provider(
    *,
    requested: Optional[str] = None,
    explicit_api_key: Optional[str] = None,
    explicit_base_url: Optional[str] = None,
    target_model: Optional[str] = None,
) -> Dict[str, Any]:
    requested_provider = resolve_requested_provider(requested)
    requested_label = (requested or "").strip().lower() if requested and requested.strip() else requested_provider
    model_cfg = _get_model_config()
    cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
    cfg_base_url = ""
    if not cfg_provider or is_cpa_provider(cfg_provider):
        cfg_base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
    elif cfg_provider:
        raise LegacyProviderDisabledError(
            f"配置中的 provider '{cfg_provider}' 已禁用。请改为 provider: cliproxyapi，并在 CPA 中配置上游。"
        )

    raw_base_url = (
        (explicit_base_url or "").strip().rstrip("/")
        or os.getenv("CLIPROXY_BASE_URL", "").strip().rstrip("/")
        or os.getenv("CPA_BASE_URL", "").strip().rstrip("/")
        or cfg_base_url
        or DEFAULT_CPA_BASE_URL
    )
    if is_known_direct_provider_base_url(raw_base_url):
        raise LegacyProviderDisabledError(cpa_base_url_boundary_message(raw_base_url))
    base_url = normalize_cpa_base_url(raw_base_url)
    api_key = (
        (explicit_api_key or "").strip()
        or os.getenv("CLIPROXY_API_KEY", "").strip()
        or os.getenv("CPA_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
        or "no-key-required"
    )
    return {
        "provider": "cliproxyapi",
        "api_mode": cpa_api_mode_for_base_url(base_url),
        "base_url": base_url,
        "api_key": api_key,
        "source": "cpa",
        "requested_provider": requested_label,
        "target_model": target_model,
    }


def format_runtime_provider_error(exc: Exception) -> str:
    if isinstance(exc, LegacyProviderDisabledError):
        return str(exc)
    return f"CPA 运行时解析失败：{exc}"


def _auto_detect_local_model(base_url: str) -> str:
    """Compatibility shim: CPA owns model discovery, Hermes does not probe providers."""
    return ""


def _get_named_custom_provider(name: str) -> Optional[Dict[str, Any]]:
    """Legacy custom providers are removed in CPA-only mode."""
    return None


def _detect_api_mode_for_url(base_url: str) -> Optional[str]:
    normalized = (base_url or "").strip().lower().rstrip("/")
    if normalized.endswith("/anthropic"):
        return "anthropic_messages"
    if normalized.endswith("/v1") or base_url_hostname(base_url):
        return "chat_completions"
    return None
