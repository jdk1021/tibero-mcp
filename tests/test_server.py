"""Server wiring that can be checked without a database or a JVM."""

import asyncio

import pytest

from tibero_mcp.config import Settings
from tibero_mcp.server import _name, _split_table, build_server


def settings(**overrides):
    base = {"host": "db.invalid", "dbname": "tibero", "user": "reader", "password": "x"}
    return Settings(_env_file=None, **{**base, **overrides})


def tool_names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def test_read_only_server_has_no_execute_tool():
    names = tool_names(build_server(settings()))
    assert "execute" not in names
    assert {"list_schemas", "list_tables", "describe_table", "search_columns", "query", "explain"} <= names


def test_execute_tool_appears_only_when_writes_are_enabled():
    assert "execute" in tool_names(build_server(settings(read_only=False)))


def test_jdbc_url_is_built_from_parts():
    assert settings(port=9629).jdbc_url == "jdbc:tibero:thin:@db.invalid:9629:tibero"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("sales_order", "SALES_ORDER"), (' "MixedCase" ', "MixedCase"), ("Emp", "EMP")],
)
def test_identifiers_follow_tibero_case_rules(raw, expected):
    assert _name(raw) == expected


def test_qualified_table_names_are_split():
    assert _split_table("hr.emp", None, "ME") == ("HR", "EMP")
    assert _split_table("emp", None, "ME") == ("ME", "EMP")
    assert _split_table("emp", "hr", "ME") == ("HR", "EMP")
