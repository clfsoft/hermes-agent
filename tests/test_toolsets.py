"""Tests for toolsets.py — toolset resolution, validation, and composition."""

import pytest

from toolsets import (
    TOOLSETS,
    get_toolset,
    resolve_toolset,
    resolve_multiple_toolsets,
    get_all_toolsets,
    get_toolset_names,
    validate_toolset,
    create_custom_toolset,
    get_toolset_info,
    _TIER_CORE_TOOLS,
    _TIER_META_TOOLS,
    _TIER_HEAVY_TOOLS,
    get_tool_tier,
    tools_for_complexity,
)


class TestGetToolset:
    def test_known_toolset(self):
        ts = get_toolset("web")
        assert ts is not None
        assert "web_search" in ts["tools"]

    def test_unknown_returns_none(self):
        assert get_toolset("nonexistent") is None


class TestResolveToolset:
    def test_leaf_toolset(self):
        tools = resolve_toolset("web")
        assert set(tools) == {"web_search", "web_extract"}

    def test_composite_toolset(self):
        tools = resolve_toolset("debugging")
        assert "terminal" in tools
        assert "web_search" in tools
        assert "web_extract" in tools

    def test_cycle_detection(self):
        # Create a cycle: A includes B, B includes A
        TOOLSETS["_cycle_a"] = {"description": "test", "tools": ["t1"], "includes": ["_cycle_b"]}
        TOOLSETS["_cycle_b"] = {"description": "test", "tools": ["t2"], "includes": ["_cycle_a"]}
        try:
            tools = resolve_toolset("_cycle_a")
            # Should not infinite loop — cycle is detected
            assert "t1" in tools
            assert "t2" in tools
        finally:
            del TOOLSETS["_cycle_a"]
            del TOOLSETS["_cycle_b"]

    def test_unknown_toolset_returns_empty(self):
        assert resolve_toolset("nonexistent") == []

    def test_all_alias(self):
        tools = resolve_toolset("all")
        assert len(tools) > 10  # Should resolve all tools from all toolsets

    def test_star_alias(self):
        tools = resolve_toolset("*")
        assert len(tools) > 10


class TestResolveMultipleToolsets:
    def test_combines_and_deduplicates(self):
        tools = resolve_multiple_toolsets(["web", "terminal"])
        assert "web_search" in tools
        assert "web_extract" in tools
        assert "terminal" in tools
        # No duplicates
        assert len(tools) == len(set(tools))

    def test_empty_list(self):
        assert resolve_multiple_toolsets([]) == []


class TestValidateToolset:
    def test_valid(self):
        assert validate_toolset("web") is True
        assert validate_toolset("terminal") is True

    def test_all_alias_valid(self):
        assert validate_toolset("all") is True
        assert validate_toolset("*") is True

    def test_invalid(self):
        assert validate_toolset("nonexistent") is False


class TestGetToolsetInfo:
    def test_leaf(self):
        info = get_toolset_info("web")
        assert info["name"] == "web"
        assert info["is_composite"] is False
        assert info["tool_count"] == 2

    def test_composite(self):
        info = get_toolset_info("debugging")
        assert info["is_composite"] is True
        assert info["tool_count"] > len(info["direct_tools"])

    def test_unknown_returns_none(self):
        assert get_toolset_info("nonexistent") is None


class TestCreateCustomToolset:
    def test_runtime_creation(self):
        create_custom_toolset(
            name="_test_custom",
            description="Test toolset",
            tools=["web_search"],
            includes=["terminal"],
        )
        try:
            tools = resolve_toolset("_test_custom")
            assert "web_search" in tools
            assert "terminal" in tools
            assert validate_toolset("_test_custom") is True
        finally:
            del TOOLSETS["_test_custom"]


class TestToolsetConsistency:
    """Verify structural integrity of the built-in TOOLSETS dict."""

    def test_all_toolsets_have_required_keys(self):
        for name, ts in TOOLSETS.items():
            assert "description" in ts, f"{name} missing description"
            assert "tools" in ts, f"{name} missing tools"
            assert "includes" in ts, f"{name} missing includes"

    def test_all_includes_reference_existing_toolsets(self):
        for name, ts in TOOLSETS.items():
            for inc in ts["includes"]:
                assert inc in TOOLSETS, f"{name} includes unknown toolset '{inc}'"

    def test_hermes_platforms_share_core_tools(self):
        """All hermes-* platform toolsets should have the same tools."""
        platforms = ["hermes-cli", "hermes-telegram", "hermes-discord", "hermes-whatsapp", "hermes-slack", "hermes-signal", "hermes-homeassistant"]
        tool_sets = [set(TOOLSETS[p]["tools"]) for p in platforms]
        for ts in tool_sets[1:]:
            assert ts == tool_sets[0]


class TestToolTiers:
    def test_core_tier_has_essential_tools(self):
        assert "web_search" in _TIER_CORE_TOOLS
        assert "terminal" in _TIER_CORE_TOOLS
        assert "read_file" in _TIER_CORE_TOOLS
        assert "write_file" in _TIER_CORE_TOOLS

    def test_meta_tier_has_planning_tools(self):
        assert "todo" in _TIER_META_TOOLS
        assert "memory" in _TIER_META_TOOLS
        assert "clarify" in _TIER_META_TOOLS

    def test_heavy_tier_has_expensive_tools(self):
        assert "browser_navigate" in _TIER_HEAVY_TOOLS
        assert "vision_analyze" in _TIER_HEAVY_TOOLS
        assert "image_generate" in _TIER_HEAVY_TOOLS
        assert "execute_code" in _TIER_HEAVY_TOOLS

    def test_tiers_are_disjoint(self):
        core = set(_TIER_CORE_TOOLS)
        meta = set(_TIER_META_TOOLS)
        heavy = set(_TIER_HEAVY_TOOLS)
        assert core & meta == set()
        assert core & heavy == set()
        assert meta & heavy == set()

    def test_tiers_cover_all_core_tools(self):
        all_tiered = set(_TIER_CORE_TOOLS) | set(_TIER_META_TOOLS) | set(_TIER_HEAVY_TOOLS)
        from toolsets import _HERMES_CORE_TOOLS
        assert all_tiered == set(_HERMES_CORE_TOOLS)

    def test_get_tool_tier(self):
        assert get_tool_tier("web_search") == "core"
        assert get_tool_tier("todo") == "meta"
        assert get_tool_tier("browser_navigate") == "heavy"
        assert get_tool_tier("unknown_tool_xyz") == "heavy"

    def test_tools_for_complexity_simple(self):
        full = list(_TIER_CORE_TOOLS) + list(_TIER_META_TOOLS) + list(_TIER_HEAVY_TOOLS)
        result = tools_for_complexity("simple", full)
        assert set(result) == set(_TIER_CORE_TOOLS)

    def test_tools_for_complexity_general(self):
        full = list(_TIER_CORE_TOOLS) + list(_TIER_META_TOOLS) + list(_TIER_HEAVY_TOOLS)
        result = tools_for_complexity("general", full)
        assert set(result) == set(_TIER_CORE_TOOLS) | set(_TIER_META_TOOLS)

    def test_tools_for_complexity_complex(self):
        full = list(_TIER_CORE_TOOLS) + list(_TIER_META_TOOLS) + list(_TIER_HEAVY_TOOLS)
        result = tools_for_complexity("complex", full)
        assert set(result) == set(full)

    def test_core_toolset_resolves(self):
        tools = resolve_toolset("core")
        assert set(tools) == set(_TIER_CORE_TOOLS)

    def test_meta_toolset_resolves(self):
        tools = resolve_toolset("meta")
        assert set(tools) == set(_TIER_META_TOOLS)

    def test_core_plus_meta_resolves(self):
        tools = resolve_multiple_toolsets(["core", "meta"])
        assert set(tools) == set(_TIER_CORE_TOOLS) | set(_TIER_META_TOOLS)

    def test_core_and_meta_toolsets_valid(self):
        assert validate_toolset("core") is True
        assert validate_toolset("meta") is True
