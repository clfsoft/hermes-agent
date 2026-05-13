"""Tests for the central tool registry.

Covers registration, schema/handler retrieval, toolset aliasing,
and edge cases (duplicate names, missing tools).
"""

import json
from unittest.mock import MagicMock

import pytest

from tools.registry import ToolRegistry


def _make_schema(name="test_tool"):
    return {
        "name": name,
        "description": f"A {name}",
        "parameters": {"type": "object", "properties": {}},
    }


# ======================================================================
# register()
# ======================================================================


class TestRegister:
    """ToolRegistry.register() — basic and edge-case registration."""

    def test_register_minimal(self):
        """A tool can be registered with only name, toolset, schema, handler."""
        reg = ToolRegistry()
        handler = MagicMock(return_value=json.dumps({"ok": True}))
        reg.register(
            name="minimal",
            toolset="core",
            schema=_make_schema("minimal"),
            handler=handler,
        )
        entry = reg._tools["minimal"]
        assert entry.name == "minimal"
        assert entry.toolset == "core"
        assert entry.handler is handler

    def test_register_with_all_optional_fields(self):
        """All optional parameters are stored correctly on the entry."""
        reg = ToolRegistry()
        handler = MagicMock()
        check_fn = MagicMock(return_value=True)
        reg.register(
            name="full_tool",
            toolset="advanced",
            schema=_make_schema("full_tool"),
            handler=handler,
            check_fn=check_fn,
            requires_env=["API_KEY", "SECRET"],
            is_async=True,
            description="A fully specified tool",
            emoji="🚀",
            max_result_size_chars=5000,
            read_only=True,
        )
        entry = reg._tools["full_tool"]
        assert entry.name == "full_tool"
        assert entry.toolset == "advanced"
        assert entry.handler is handler
        assert entry.check_fn is check_fn
        assert entry.requires_env == ["API_KEY", "SECRET"]
        assert entry.is_async is True
        assert entry.description == "A fully specified tool"
        assert entry.emoji == "🚀"
        assert entry.max_result_size_chars == 5000
        assert entry.read_only is True

    def test_register_description_falls_back_to_schema(self):
        """When description is empty, it falls back to schema['description']."""
        reg = ToolRegistry()
        schema = _make_schema("desc_tool")
        reg.register(
            name="desc_tool",
            toolset="core",
            schema=schema,
            handler=MagicMock(),
            description="",
        )
        assert reg._tools["desc_tool"].description == "A desc_tool"

    def test_register_empty_requires_env_defaults_to_empty_list(self):
        """requires_env=None becomes an empty list."""
        reg = ToolRegistry()
        reg.register(
            name="no_env",
            toolset="core",
            schema=_make_schema(),
            handler=MagicMock(),
            requires_env=None,
        )
        assert reg._tools["no_env"].requires_env == []

    def test_register_same_toolset_overwrites_previous_entry(self):
        """Registering the same name in the same toolset silently overwrites."""
        reg = ToolRegistry()
        handler_old = MagicMock()
        handler_new = MagicMock()
        reg.register(
            name="overwrite", toolset="core", schema=_make_schema(), handler=handler_old
        )
        reg.register(
            name="overwrite", toolset="core", schema=_make_schema(), handler=handler_new
        )
        assert reg._tools["overwrite"].handler is handler_new

    def test_register_different_toolset_overwrites_with_warning(self):
        """Registering the same name in a different toolset overwrites (with log warning)."""
        reg = ToolRegistry()
        handler_old = MagicMock()
        handler_new = MagicMock()
        reg.register(
            name="conflict", toolset="alpha", schema=_make_schema(), handler=handler_old
        )
        reg.register(
            name="conflict", toolset="beta", schema=_make_schema(), handler=handler_new
        )
        assert reg._tools["conflict"].handler is handler_new
        assert reg._tools["conflict"].toolset == "beta"

    def test_register_check_fn_adds_toolset_check_once(self):
        """The first check_fn for a toolset is stored as the toolset-level check."""
        reg = ToolRegistry()
        check_a = MagicMock(return_value=True)
        check_b = MagicMock(return_value=True)
        reg.register(
            name="t1", toolset="my_set", schema=_make_schema(),
            handler=MagicMock(), check_fn=check_a,
        )
        # Second tool in same toolset should NOT overwrite the toolset check
        reg.register(
            name="t2", toolset="my_set", schema=_make_schema(),
            handler=MagicMock(), check_fn=check_b,
        )
        assert reg._toolset_checks["my_set"] is check_a

    def test_register_no_check_fn_does_not_add_toolset_check(self):
        """A tool without check_fn does not create a toolset-level check entry."""
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="silent", schema=_make_schema(), handler=MagicMock()
        )
        assert "silent" not in reg._toolset_checks

    def test_register_read_only_flag_stored(self):
        """The read_only flag is stored on the entry."""
        reg = ToolRegistry()
        reg.register(
            name="ro", toolset="core", schema=_make_schema(),
            handler=MagicMock(), read_only=True,
        )
        assert reg._tools["ro"].read_only is True

    def test_register_read_only_default_false(self):
        """The read_only flag defaults to False."""
        reg = ToolRegistry()
        reg.register(
            name="rw", toolset="core", schema=_make_schema(), handler=MagicMock()
        )
        assert reg._tools["rw"].read_only is False


# ======================================================================
# get_schema()
# ======================================================================


class TestGetSchema:
    """ToolRegistry.get_schema() — retrieving tool schemas."""

    def test_get_schema_returns_registered_schema(self):
        """Returns the exact schema dict that was registered."""
        reg = ToolRegistry()
        schema = _make_schema("schematic")
        reg.register(
            name="schematic", toolset="core", schema=schema, handler=MagicMock()
        )
        assert reg.get_schema("schematic") is schema

    def test_get_schema_returns_none_for_missing_tool(self):
        """Returns None when the tool name does not exist."""
        reg = ToolRegistry()
        assert reg.get_schema("nonexistent") is None

    def test_get_schema_after_overwrite_returns_new_schema(self):
        """After re-registration, get_schema returns the latest schema."""
        reg = ToolRegistry()
        schema_old = _make_schema("over")
        schema_new = _make_schema("over")
        reg.register(
            name="over", toolset="core", schema=schema_old, handler=MagicMock()
        )
        reg.register(
            name="over", toolset="core", schema=schema_new, handler=MagicMock()
        )
        assert reg.get_schema("over") is schema_new

    def test_get_schema_after_deregister_returns_none(self):
        """After deregistering, get_schema returns None."""
        reg = ToolRegistry()
        reg.register(
            name="temp", toolset="core", schema=_make_schema(), handler=MagicMock()
        )
        reg.deregister("temp")
        assert reg.get_schema("temp") is None

    def test_get_schema_returns_raw_schema_without_check_fn_filtering(self):
        """get_schema bypasses check_fn; even unavailable tools return their schema."""
        reg = ToolRegistry()
        schema = _make_schema("blocked")
        reg.register(
            name="blocked", toolset="s", schema=schema,
            handler=MagicMock(), check_fn=lambda: False,
        )
        assert reg.get_schema("blocked") is schema


# ======================================================================
# get_handler()  (via _tools dict — no public getter exists)
# ======================================================================


class TestGetHandler:
    """Retrieving tool handlers.

    ToolRegistry does not expose a public ``get_handler()`` method.
    Handlers are accessed via ``_tools`` or invoked through ``dispatch()``.
    """

    def test_handler_accessible_via_tools_dict(self):
        """The handler is stored on the ToolEntry and accessible via _tools."""
        reg = ToolRegistry()
        handler = MagicMock(return_value=json.dumps({"done": True}))
        reg.register(
            name="h_tool", toolset="core", schema=_make_schema(), handler=handler
        )
        assert reg._tools["h_tool"].handler is handler

    def test_handler_invocation_through_dispatch(self):
        """dispatch() calls the registered handler with the correct args."""
        reg = ToolRegistry()
        handler = MagicMock(return_value=json.dumps({"done": True}))
        reg.register(
            name="h_tool", toolset="core", schema=_make_schema(), handler=handler
        )
        result = reg.dispatch("h_tool", {"key": "val"})
        handler.assert_called_once_with({"key": "val"})
        assert json.loads(result) == {"done": True}

    def test_handler_for_nonexistent_tool_returns_none_from_dict_get(self):
        """Accessing a non-existent tool's handler via _tools.get returns None."""
        reg = ToolRegistry()
        entry = reg._tools.get("nonexistent")
        assert entry is None

    def test_handler_for_nonexistent_tool_dispatch_returns_error(self):
        """dispatch() for a non-existent tool returns an error JSON."""
        reg = ToolRegistry()
        result = json.loads(reg.dispatch("nowhere", {}))
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_handler_is_callable(self):
        """The registered handler is callable and produces expected output."""
        reg = ToolRegistry()
        handler = MagicMock(return_value=json.dumps({"result": 42}))
        reg.register(
            name="answer", toolset="core", schema=_make_schema(), handler=handler
        )
        output = reg._tools["answer"].handler({"question": "life"})
        assert json.loads(output) == {"result": 42}


# ======================================================================
# get_all_tool_names()
# ======================================================================


class TestGetAllToolNames:
    """ToolRegistry.get_all_tool_names() — listing registered tools."""

    def test_empty_when_no_tools_registered(self):
        """Returns an empty list when no tools have been registered."""
        reg = ToolRegistry()
        assert reg.get_all_tool_names() == []

    def test_returns_sorted_names(self):
        """Returns tool names in alphabetical order."""
        reg = ToolRegistry()
        reg.register(
            name="zebra", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="alpha", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="beta", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_all_tool_names() == ["alpha", "beta", "zebra"]

    def test_includes_all_registered_tools(self):
        """All registered tool names appear in the returned list."""
        reg = ToolRegistry()
        reg.register(
            name="tool_a", toolset="s1", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="tool_b", toolset="s2", schema=_make_schema(), handler=MagicMock()
        )
        assert set(reg.get_all_tool_names()) == {"tool_a", "tool_b"}

    def test_after_deregister_excludes_removed_tool(self):
        """Deregistered tools no longer appear in the list."""
        reg = ToolRegistry()
        reg.register(
            name="keep", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="remove", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        reg.deregister("remove")
        assert reg.get_all_tool_names() == ["keep"]

    def test_after_register_overwrite_name_still_appears_once(self):
        """Overwriting a tool name does not create duplicate entries."""
        reg = ToolRegistry()
        reg.register(
            name="dup", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="dup", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_all_tool_names() == ["dup"]


# ======================================================================
# resolve_toolset_alias()
# ======================================================================


class TestResolveToolsetAlias:
    """ToolRegistry.resolve_toolset_alias() — alias resolution."""

    def test_resolves_known_alias_to_target(self):
        """A registered alias resolves to its target toolset name."""
        reg = ToolRegistry()
        reg.register_toolset_alias("mcp-filesystem", "mcp_filesystem")
        assert reg.resolve_toolset_alias("mcp-filesystem") == "mcp_filesystem"

    def test_returns_original_for_unknown_alias(self):
        """An unregistered name is returned as-is."""
        reg = ToolRegistry()
        assert reg.resolve_toolset_alias("unknown") == "unknown"

    def test_returns_original_for_none_input(self):
        """None is passed through as-is (no alias match)."""
        reg = ToolRegistry()
        assert reg.resolve_toolset_alias(None) is None

    def test_returns_original_for_empty_string(self):
        """Empty string is passed through as-is."""
        reg = ToolRegistry()
        assert reg.resolve_toolset_alias("") == ""

    def test_after_unregister_alias_returns_original(self):
        """After unregistering an alias, it no longer resolves."""
        reg = ToolRegistry()
        reg.register_toolset_alias("short", "very_long_name")
        reg.unregister_toolset_alias("short")
        assert reg.resolve_toolset_alias("short") == "short"

    def test_multiple_aliases_resolve_independently(self):
        """Multiple aliases can be registered and resolved independently."""
        reg = ToolRegistry()
        reg.register_toolset_alias("fs", "filesystem")
        reg.register_toolset_alias("br", "browser")
        assert reg.resolve_toolset_alias("fs") == "filesystem"
        assert reg.resolve_toolset_alias("br") == "browser"
        assert reg.resolve_toolset_alias("other") == "other"

    def test_alias_to_self_is_not_registered(self):
        """An alias equal to its target is not stored."""
        reg = ToolRegistry()
        reg.register_toolset_alias("same", "same")
        assert "same" not in reg._toolset_aliases

    def test_alias_with_empty_target_is_not_registered(self):
        """An alias with an empty target is not stored."""
        reg = ToolRegistry()
        reg.register_toolset_alias("orphan", "")
        assert "orphan" not in reg._toolset_aliases

    def test_alias_with_empty_alias_is_not_registered(self):
        """An empty alias with a valid target is not stored."""
        reg = ToolRegistry()
        reg.register_toolset_alias("", "target")
        assert "" not in reg._toolset_aliases


# ======================================================================
# get_toolset_for_tool()
# ======================================================================


class TestGetToolsetForTool:
    """ToolRegistry.get_toolset_for_tool() — mapping tools to toolsets."""

    def test_returns_toolset_for_registered_tool(self):
        """Returns the toolset name for a registered tool."""
        reg = ToolRegistry()
        reg.register(
            name="my_tool", toolset="my_set", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_toolset_for_tool("my_tool") == "my_set"

    def test_returns_none_for_unregistered_tool(self):
        """Returns None when the tool is not registered."""
        reg = ToolRegistry()
        assert reg.get_toolset_for_tool("ghost") is None

    def test_returns_updated_toolset_after_overwrite(self):
        """After re-registration with a different toolset, returns the new toolset."""
        reg = ToolRegistry()
        reg.register(
            name="mobile", toolset="old_set", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="mobile", toolset="new_set", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_toolset_for_tool("mobile") == "new_set"

    def test_returns_none_after_deregister(self):
        """After deregistering, returns None."""
        reg = ToolRegistry()
        reg.register(
            name="gone", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        reg.deregister("gone")
        assert reg.get_toolset_for_tool("gone") is None

    def test_different_tools_in_different_toolsets(self):
        """Multiple tools in different toolsets report correctly."""
        reg = ToolRegistry()
        reg.register(
            name="a", toolset="set_a", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="b", toolset="set_b", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_toolset_for_tool("a") == "set_a"
        assert reg.get_toolset_for_tool("b") == "set_b"


# ======================================================================
# get_toolset_aliases()
# ======================================================================


class TestGetToolsetAliases:
    """ToolRegistry.get_toolset_aliases() — retrieving all aliases."""

    def test_returns_empty_dict_initially(self):
        """Before any aliases are registered, returns an empty dict."""
        reg = ToolRegistry()
        assert reg.get_toolset_aliases() == {}

    def test_returns_all_registered_aliases(self):
        """Returns all registered alias → target mappings."""
        reg = ToolRegistry()
        reg.register_toolset_alias("short", "long_name")
        reg.register_toolset_alias("mini", "maxi")
        aliases = reg.get_toolset_aliases()
        assert aliases == {"short": "long_name", "mini": "maxi"}

    def test_returns_copy_not_reference(self):
        """The returned dict is a copy; mutating it does not affect the registry."""
        reg = ToolRegistry()
        reg.register_toolset_alias("x", "y")
        returned = reg.get_toolset_aliases()
        returned["x"] = "hacked"
        assert reg.get_toolset_aliases()["x"] == "y"

    def test_after_unregister_excludes_removed_alias(self):
        """After unregistering an alias, it no longer appears in the dict."""
        reg = ToolRegistry()
        reg.register_toolset_alias("tmp", "temporary")
        reg.unregister_toolset_alias("tmp")
        assert "tmp" not in reg.get_toolset_aliases()

    def test_register_toolset_alias_with_none_alias(self):
        """register_toolset_alias with None alias does not create an entry."""
        reg = ToolRegistry()
        reg.register_toolset_alias(None, "target")
        assert reg.get_toolset_aliases() == {}

    def test_register_toolset_alias_with_none_target(self):
        """register_toolset_alias with None target does not create an entry."""
        reg = ToolRegistry()
        reg.register_toolset_alias("alias", None)
        assert reg.get_toolset_aliases() == {}

    def test_register_toolset_alias_whitespace_only(self):
        """Whitespace-only alias or target is treated as empty and not registered."""
        reg = ToolRegistry()
        reg.register_toolset_alias("  ", "target")
        reg.register_toolset_alias("alias", "  ")
        assert reg.get_toolset_aliases() == {}


# ======================================================================
# deregister()
# ======================================================================


class TestDeregister:
    """ToolRegistry.deregister() — removing tools."""

    def test_deregister_removes_tool(self):
        """A deregistered tool is removed from _tools."""
        reg = ToolRegistry()
        reg.register(
            name="goner", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        reg.deregister("goner")
        assert "goner" not in reg._tools

    def test_deregister_nonexistent_tool_does_not_raise(self):
        """Deregistering a non-existent tool is a no-op."""
        reg = ToolRegistry()
        reg.deregister("phantom")  # should not raise

    def test_deregister_cleans_up_toolset_check_when_last_tool(self):
        """When the last tool of a toolset is removed, the toolset check is cleaned up."""
        reg = ToolRegistry()
        check_fn = MagicMock(return_value=True)
        reg.register(
            name="only", toolset="lonely", schema=_make_schema(),
            handler=MagicMock(), check_fn=check_fn,
        )
        assert "lonely" in reg._toolset_checks
        reg.deregister("only")
        assert "lonely" not in reg._toolset_checks

    def test_deregister_keeps_toolset_check_when_other_tools_remain(self):
        """When other tools remain in the toolset, the check is preserved."""
        reg = ToolRegistry()
        check_fn = MagicMock(return_value=True)
        reg.register(
            name="t1", toolset="group", schema=_make_schema(),
            handler=MagicMock(), check_fn=check_fn,
        )
        reg.register(
            name="t2", toolset="group", schema=_make_schema(),
            handler=MagicMock(), check_fn=check_fn,
        )
        reg.deregister("t1")
        assert "group" in reg._toolset_checks


# ======================================================================
# mark_read_only() / read_only_names()
# ======================================================================


class TestReadOnly:
    """ToolRegistry.mark_read_only() and read_only_names()."""

    def test_mark_read_only_sets_flag(self):
        """mark_read_only sets read_only=True on matching tools."""
        reg = ToolRegistry()
        reg.register(
            name="r", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        count = reg.mark_read_only(["r"])
        assert count == 1
        assert reg._tools["r"].read_only is True

    def test_mark_read_only_returns_hit_count(self):
        """mark_read_only returns the number of tools that were actually flagged."""
        reg = ToolRegistry()
        reg.register(
            name="a", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="b", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        count = reg.mark_read_only(["a", "b", "phantom"])
        assert count == 2

    def test_mark_read_only_ignores_unknown_names(self):
        """mark_read_only silently skips names that are not registered."""
        reg = ToolRegistry()
        count = reg.mark_read_only(["nowhere"])
        assert count == 0

    def test_read_only_names_returns_set(self):
        """read_only_names returns the set of tool names flagged read-only."""
        reg = ToolRegistry()
        reg.register(
            name="ro_a", toolset="s", schema=_make_schema(),
            handler=MagicMock(), read_only=True,
        )
        reg.register(
            name="ro_b", toolset="s", schema=_make_schema(),
            handler=MagicMock(), read_only=True,
        )
        reg.register(
            name="rw", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.read_only_names() == {"ro_a", "ro_b"}

    def test_read_only_names_empty_when_none_marked(self):
        """read_only_names returns an empty set when no tools are read-only."""
        reg = ToolRegistry()
        assert reg.read_only_names() == set()


# ======================================================================
# get_max_result_size()
# ======================================================================


class TestGetMaxResultSize:
    """ToolRegistry.get_max_result_size() — per-tool size limits."""

    def test_returns_registered_value(self):
        """Returns the max_result_size_chars that was registered."""
        reg = ToolRegistry()
        reg.register(
            name="big", toolset="s", schema=_make_schema(),
            handler=MagicMock(), max_result_size_chars=99999,
        )
        assert reg.get_max_result_size("big") == 99999

    def test_returns_default_when_not_set_and_default_provided(self):
        """When the tool has no max_result_size_chars, the explicit default is returned."""
        reg = ToolRegistry()
        reg.register(
            name="normal", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_max_result_size("normal", default=50000) == 50000

    def test_returns_global_default_when_no_default_arg(self):
        """When no default is given, falls back to the module-level constant."""
        reg = ToolRegistry()
        reg.register(
            name="normal", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        from tools.budget_config import DEFAULT_RESULT_SIZE_CHARS
        assert reg.get_max_result_size("normal") == DEFAULT_RESULT_SIZE_CHARS

    def test_returns_default_for_unknown_tool(self):
        """For an unregistered tool, the explicit default is returned."""
        reg = ToolRegistry()
        assert reg.get_max_result_size("ghost", default=1234) == 1234

    def test_registered_value_overrides_default(self):
        """A registered max_result_size_chars takes priority over the provided default."""
        reg = ToolRegistry()
        reg.register(
            name="custom", toolset="s", schema=_make_schema(),
            handler=MagicMock(), max_result_size_chars=777,
        )
        assert reg.get_max_result_size("custom", default=999) == 777


# ======================================================================
# get_emoji()
# ======================================================================


class TestGetEmoji:
    """ToolRegistry.get_emoji() — per-tool emoji lookup."""

    def test_returns_registered_emoji(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=MagicMock(), emoji="🔥",
        )
        assert reg.get_emoji("t") == "🔥"

    def test_returns_default_when_emoji_unset(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_emoji("t") == "⚡"

    def test_returns_custom_default_when_emoji_unset(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_emoji("t", default="🔧") == "🔧"

    def test_returns_default_for_unknown_tool(self):
        reg = ToolRegistry()
        assert reg.get_emoji("nonexistent") == "⚡"

    def test_returns_custom_default_for_unknown_tool(self):
        reg = ToolRegistry()
        assert reg.get_emoji("nonexistent", default="❓") == "❓"

    def test_empty_string_emoji_treated_as_unset(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=MagicMock(), emoji="",
        )
        assert reg.get_emoji("t") == "⚡"


# ======================================================================
# get_tool_to_toolset_map()
# ======================================================================


class TestGetToolToToolsetMap:
    """ToolRegistry.get_tool_to_toolset_map() — full tool→toolset mapping."""

    def test_returns_empty_dict_initially(self):
        reg = ToolRegistry()
        assert reg.get_tool_to_toolset_map() == {}

    def test_returns_mapping_for_all_tools(self):
        reg = ToolRegistry()
        reg.register(
            name="a", toolset="s1", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="b", toolset="s2", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_tool_to_toolset_map() == {"a": "s1", "b": "s2"}

    def test_updates_after_overwrite(self):
        reg = ToolRegistry()
        reg.register(
            name="x", toolset="old", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="x", toolset="new", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.get_tool_to_toolset_map() == {"x": "new"}


# ======================================================================
# is_toolset_available()
# ======================================================================


class TestIsToolsetAvailable:
    """ToolRegistry.is_toolset_available() — toolset availability checks."""

    def test_no_check_fn_returns_true(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="free", schema=_make_schema(), handler=MagicMock()
        )
        assert reg.is_toolset_available("free") is True

    def test_check_fn_returns_true_when_check_passes(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="ok", schema=_make_schema(),
            handler=MagicMock(), check_fn=lambda: True,
        )
        assert reg.is_toolset_available("ok") is True

    def test_check_fn_returns_false_when_check_fails(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="nope", schema=_make_schema(),
            handler=MagicMock(), check_fn=lambda: False,
        )
        assert reg.is_toolset_available("nope") is False

    def test_check_fn_exception_returns_false(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="broken", schema=_make_schema(),
            handler=MagicMock(), check_fn=lambda: 1 / 0,
        )
        assert reg.is_toolset_available("broken") is False

    def test_unknown_toolset_returns_true(self):
        """A toolset with no registered tools and no check is considered available."""
        reg = ToolRegistry()
        assert reg.is_toolset_available("unknown") is True


# ======================================================================
# get_available_toolsets()
# ======================================================================


class TestGetAvailableToolsets:
    """ToolRegistry.get_available_toolsets() — UI-facing toolset metadata."""

    def test_returns_empty_dict_with_no_tools(self):
        reg = ToolRegistry()
        assert reg.get_available_toolsets() == {}

    def test_includes_tool_names_in_toolset(self):
        reg = ToolRegistry()
        reg.register(
            name="t1", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        reg.register(
            name="t2", toolset="s", schema=_make_schema(), handler=MagicMock()
        )
        result = reg.get_available_toolsets()
        assert set(result["s"]["tools"]) == {"t1", "t2"}

    def test_includes_availability(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=MagicMock(), check_fn=lambda: True,
        )
        assert reg.get_available_toolsets()["s"]["available"] is True

    def test_includes_requirements(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=MagicMock(), requires_env=["API_KEY"],
        )
        assert "API_KEY" in reg.get_available_toolsets()["s"]["requirements"]


# ======================================================================
# dispatch()
# ======================================================================


class TestDispatch:
    """ToolRegistry.dispatch() — tool execution."""

    def test_dispatch_calls_handler_with_args(self):
        reg = ToolRegistry()
        handler = MagicMock(return_value=json.dumps({"ok": True}))
        reg.register(
            name="echo", toolset="core", schema=_make_schema(), handler=handler
        )
        reg.dispatch("echo", {"msg": "hello"})
        handler.assert_called_once_with({"msg": "hello"})

    def test_dispatch_passes_extra_kwargs(self):
        reg = ToolRegistry()
        handler = MagicMock(return_value=json.dumps({"ok": True}))
        reg.register(
            name="kw", toolset="core", schema=_make_schema(), handler=handler
        )
        reg.dispatch("kw", {}, extra="data")
        handler.assert_called_once_with({}, extra="data")

    def test_dispatch_unknown_tool_returns_error(self):
        reg = ToolRegistry()
        result = json.loads(reg.dispatch("nowhere", {}))
        assert result["error"] == "Unknown tool: nowhere"

    def test_dispatch_handler_exception_returns_error(self):
        reg = ToolRegistry()

        def crash(args, **kw):
            raise ValueError("boom")

        reg.register(
            name="crash", toolset="s", schema=_make_schema(), handler=crash
        )
        result = json.loads(reg.dispatch("crash", {}))
        assert "error" in result
        assert "ValueError" in result["error"]


# ======================================================================
# check_tool_availability()
# ======================================================================


class TestCheckToolAvailability:
    """ToolRegistry.check_tool_availability() — legacy availability check."""

    def test_returns_available_and_unavailable(self):
        reg = ToolRegistry()
        reg.register(
            name="a", toolset="good", schema=_make_schema(),
            handler=MagicMock(), check_fn=lambda: True,
        )
        reg.register(
            name="b", toolset="bad", schema=_make_schema(),
            handler=MagicMock(), check_fn=lambda: False,
        )
        available, unavailable = reg.check_tool_availability()
        assert "good" in available
        assert any(u["name"] == "bad" for u in unavailable)

    def test_no_check_fn_is_available(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="free", schema=_make_schema(), handler=MagicMock()
        )
        available, unavailable = reg.check_tool_availability()
        assert "free" in available

    def test_unavailable_includes_env_vars_and_tools(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="blocked", schema=_make_schema(),
            handler=MagicMock(), check_fn=lambda: False,
            requires_env=["SECRET_KEY"],
        )
        _, unavailable = reg.check_tool_availability()
        entry = next(u for u in unavailable if u["name"] == "blocked")
        assert "SECRET_KEY" in entry["env_vars"]
        assert "t" in entry["tools"]