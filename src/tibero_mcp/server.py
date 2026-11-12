"""MCP tools for exploring and querying a Tibero database."""

# No `from __future__ import annotations` here: tool signatures are inspected at
# registration time and some annotations close over local settings.
import argparse
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from . import __version__
from .config import Settings
from .db import Database, DatabaseError, Param
from .driver import DriverError
from .sqlguard import SqlRejected, parse_for_execute, require_read

log = logging.getLogger("tibero_mcp")

SYSTEM_SCHEMAS = ("SYS", "SYSCAT", "SYSGIS", "OUTLN", "TIBERO", "TIBERO1", "PUBLIC")

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

# Column type as written in DDL, e.g. VARCHAR(20), NUMBER(11,0), TIMESTAMP(6).
TYPE_EXPR = (
    "c.DATA_TYPE || CASE"
    " WHEN c.DATA_TYPE IN ('VARCHAR', 'VARCHAR2', 'NVARCHAR', 'NVARCHAR2', 'CHAR', 'NCHAR', 'RAW')"
    "  THEN '(' || c.CHAR_LENGTH || ')'"
    " WHEN c.DATA_TYPE = 'NUMBER' AND c.DATA_PRECISION IS NOT NULL"
    "  THEN '(' || c.DATA_PRECISION || NVL2(c.DATA_SCALE, ',' || c.DATA_SCALE, '') || ')'"
    " ELSE '' END"
)

Schema = Annotated[str | None, Field(description="Schema (owner). Defaults to the connected user.")]


class ToolFailure(ToolError):
    """Error whose message is shown to the model as-is."""


@contextmanager
def tool_errors() -> Iterator[None]:
    """Turn expected database/driver/SQL errors into messages the model can act on."""
    try:
        yield
    except (DatabaseError, DriverError, SqlRejected) as e:
        raise ToolFailure(str(e)) from None


def _name(value: str) -> str:
    """Normalise an identifier the way Tibero does: quoted stays, bare goes upper-case."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value.upper()


def _split_table(table: str, schema: str | None, default: str) -> tuple[str, str]:
    if schema is None and "." in table and not table.strip().startswith('"'):
        schema, table = table.split(".", 1)
    return (_name(schema) if schema else default), _name(table)


def _like(text: str) -> str:
    return f"%{text.strip().upper()}%"


def build_server(settings: Settings) -> MCPServer:
    db = Database(settings)
    writes = not settings.read_only
    mode = "read-write" if writes else "read-only"
    mcp = MCPServer(
        name="tibero",
        title="Tibero",
        version=__version__,
        instructions=(
            f"Tibero database at {settings.host}:{settings.port}/{settings.dbname}, connected as "
            f"{settings.user.upper()} ({mode}). Tibero speaks an Oracle-compatible SQL dialect: use ROWNUM "
            "for row limits, DUAL for scalar selects, and the ALL_* dictionary views. Unqualified names "
            "resolve to the connected user's schema. Start with list_tables or search_columns to find "
            "objects (comments are often in Korean), then describe_table before writing queries."
        ),
    )

    def owner(schema: str | None) -> str:
        if schema:
            return _name(schema)
        if db.current_user is None:
            with tool_errors():
                db.query("SELECT 1 FROM DUAL")
        return db.current_user or settings.user.upper()

    @mcp.tool(annotations=READ)
    def list_schemas(
        include_system: Annotated[bool, Field(description="Include Tibero system schemas such as SYS.")] = False,
    ) -> dict[str, Any]:
        """List schemas that own tables or views visible to the connected user, with object counts."""
        where = "" if include_system else f"WHERE OWNER NOT IN ({', '.join('?' * len(SYSTEM_SCHEMAS))})"
        params: list[Param] = [] if include_system else list(SYSTEM_SCHEMAS)
        sql = (
            "SELECT OWNER AS SCHEMA, SUM(T) AS TABLES, SUM(V) AS VIEWS FROM ("
            " SELECT OWNER, 1 AS T, 0 AS V FROM ALL_TABLES"
            " UNION ALL SELECT OWNER, 0, 1 FROM ALL_VIEWS"
            f") {where} GROUP BY OWNER ORDER BY OWNER"
        )
        with tool_errors():
            result = db.query(sql, params)
        return {"current_user": owner(None), **result.as_dict()}

    @mcp.tool(annotations=READ)
    def list_tables(
        schema: Schema = None,
        search: Annotated[
            str | None, Field(description="Case-insensitive text matched against table names and comments.")
        ] = None,
        include_views: bool = True,
    ) -> dict[str, Any]:
        """List tables (and views) in a schema with their comments and approximate row counts."""
        own = owner(schema)
        views = (
            " UNION ALL SELECT v.OWNER, v.VIEW_NAME, 'VIEW', c.COMMENTS, NULL FROM ALL_VIEWS v"
            " LEFT JOIN ALL_TAB_COMMENTS c ON c.OWNER = v.OWNER AND c.TABLE_NAME = v.VIEW_NAME"
            " WHERE v.OWNER = ?"
            if include_views
            else ""
        )
        sql = (
            "SELECT NAME, TYPE, COMMENTS, NUM_ROWS FROM ("
            " SELECT t.OWNER, t.TABLE_NAME AS NAME, 'TABLE' AS TYPE, c.COMMENTS, t.NUM_ROWS FROM ALL_TABLES t"
            " LEFT JOIN ALL_TAB_COMMENTS c ON c.OWNER = t.OWNER AND c.TABLE_NAME = t.TABLE_NAME"
            f" WHERE t.OWNER = ?{views})"
        )
        params: list[Param] = [own, own] if include_views else [own]
        if search:
            sql += " WHERE UPPER(NAME) LIKE ? OR UPPER(COMMENTS) LIKE ?"
            params += [_like(search), _like(search)]
        sql += " ORDER BY NAME"
        with tool_errors():
            result = db.query(sql, params)
        return {"schema": own, **result.as_dict()}

    @mcp.tool(annotations=READ)
    def describe_table(
        table: Annotated[str, Field(description="Table or view name, optionally qualified as SCHEMA.TABLE.")],
        schema: Schema = None,
    ) -> dict[str, Any]:
        """Show a table's columns (type, nullability, default, comment), keys, foreign keys and indexes."""
        own, name = _split_table(table, schema, owner(None))
        with tool_errors():
            info = db.query(
                "SELECT o.OBJECT_TYPE, c.COMMENTS FROM ALL_OBJECTS o"
                " LEFT JOIN ALL_TAB_COMMENTS c ON c.OWNER = o.OWNER AND c.TABLE_NAME = o.OBJECT_NAME"
                " WHERE o.OWNER = ? AND o.OBJECT_NAME = ? AND o.OBJECT_TYPE IN ('TABLE', 'VIEW')",
                [own, name],
            )
            if not info.rows:
                hint = db.query(
                    "SELECT OWNER FROM ALL_OBJECTS WHERE OBJECT_NAME = ?"
                    " AND OBJECT_TYPE IN ('TABLE', 'VIEW', 'SYNONYM')",
                    [name],
                )
                others = sorted({r[0] for r in hint.rows})
                extra = f" It exists in: {', '.join(others)}." if others else " Try list_tables with a search term."
                raise ToolFailure(f"No table or view {own}.{name} visible to {owner(None)}.{extra}")

            columns = db.query(
                f"SELECT c.COLUMN_NAME AS NAME, {TYPE_EXPR} AS TYPE,"
                " c.NULLABLE, c.DATA_DEFAULT AS DEFAULT_VALUE, m.COMMENTS"
                " FROM ALL_TAB_COLUMNS c"
                " LEFT JOIN ALL_COL_COMMENTS m ON m.OWNER = c.OWNER AND m.TABLE_NAME = c.TABLE_NAME"
                "  AND m.COLUMN_NAME = c.COLUMN_NAME"
                " WHERE c.OWNER = ? AND c.TABLE_NAME = ? ORDER BY c.COLUMN_ID",
                [own, name],
                100_000,
            )
            constraints = db.query(
                "SELECT k.CONSTRAINT_NAME, k.CONSTRAINT_TYPE, cc.COLUMN_NAME, cc.POSITION,"
                " r.OWNER AS REF_OWNER, r.TABLE_NAME AS REF_TABLE, rc.COLUMN_NAME AS REF_COLUMN"
                " FROM ALL_CONSTRAINTS k"
                " JOIN ALL_CONS_COLUMNS cc ON cc.OWNER = k.OWNER AND cc.CONSTRAINT_NAME = k.CONSTRAINT_NAME"
                " LEFT JOIN ALL_CONSTRAINTS r ON r.OWNER = k.R_OWNER AND r.CONSTRAINT_NAME = k.R_CONSTRAINT_NAME"
                " LEFT JOIN ALL_CONS_COLUMNS rc ON rc.OWNER = r.OWNER AND rc.CONSTRAINT_NAME = r.CONSTRAINT_NAME"
                "  AND rc.POSITION = cc.POSITION"
                " WHERE k.OWNER = ? AND k.TABLE_NAME = ? AND k.CONSTRAINT_TYPE IN ('P', 'U', 'R')"
                " ORDER BY k.CONSTRAINT_TYPE, k.CONSTRAINT_NAME, cc.POSITION",
                [own, name],
                100_000,
            )
            indexes = db.query(
                "SELECT i.INDEX_NAME, i.UNIQUENESS, ic.COLUMN_NAME, ic.DESCEND"
                " FROM ALL_INDEXES i JOIN ALL_IND_COLUMNS ic"
                "  ON ic.INDEX_OWNER = i.OWNER AND ic.INDEX_NAME = i.INDEX_NAME"
                " WHERE i.TABLE_OWNER = ? AND i.TABLE_NAME = ? ORDER BY i.INDEX_NAME, ic.COLUMN_POSITION",
                [own, name],
                100_000,
            )

        primary_key: list[str] = []
        unique: dict[str, list[str]] = {}
        foreign: dict[str, dict[str, Any]] = {}
        for cname, ctype, col, _pos, rown, rtab, rcol in constraints.rows:
            if ctype == "P":
                primary_key.append(col)
            elif ctype == "U":
                unique.setdefault(cname, []).append(col)
            else:
                fk = foreign.setdefault(cname, {"columns": [], "references": f"{rown}.{rtab}", "ref_columns": []})
                fk["columns"].append(col)
                fk["ref_columns"].append(rcol)
        index_map: dict[str, dict[str, Any]] = {}
        for iname, uniq, col, desc in indexes.rows:
            entry = index_map.setdefault(iname, {"name": iname, "unique": uniq == "UNIQUE", "columns": []})
            entry["columns"].append(f"{col} DESC" if desc == "DESC" else col)

        keys = ["name", "type", "nullable", "default", "comment"]
        cols = []
        for name_, type_, nullable, default, comment in columns.rows:
            default = default.strip() if isinstance(default, str) else default
            cols.append(dict(zip(keys, [name_, type_, nullable == "Y", default, comment], strict=True)))
        return {
            "schema": own,
            "name": name,
            "type": info.rows[0][0],
            "comment": info.rows[0][1],
            "columns": cols,
            "primary_key": primary_key,
            "unique_keys": [{"name": k, "columns": v} for k, v in unique.items()],
            "foreign_keys": [{"name": k, **v} for k, v in foreign.items()],
            "indexes": list(index_map.values()),
        }

    @mcp.tool(annotations=READ)
    def search_columns(
        keyword: Annotated[str, Field(description="Text matched against column names and column comments.")],
        schema: Schema = None,
    ) -> dict[str, Any]:
        """Find columns across all tables of a schema by name or comment, e.g. '주문' or 'ORDER_DT'."""
        own = owner(schema)
        sql = (
            "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE,"
            " m.COMMENTS AS COLUMN_COMMENT, t.COMMENTS AS TABLE_COMMENT"
            " FROM ALL_TAB_COLUMNS c"
            " LEFT JOIN ALL_COL_COMMENTS m ON m.OWNER = c.OWNER AND m.TABLE_NAME = c.TABLE_NAME"
            "  AND m.COLUMN_NAME = c.COLUMN_NAME"
            " LEFT JOIN ALL_TAB_COMMENTS t ON t.OWNER = c.OWNER AND t.TABLE_NAME = c.TABLE_NAME"
            " WHERE c.OWNER = ? AND (UPPER(c.COLUMN_NAME) LIKE ? OR UPPER(m.COMMENTS) LIKE ?)"
        )
        params: list[Param] = [own, _like(keyword), _like(keyword)]
        sql += " ORDER BY c.TABLE_NAME, c.COLUMN_ID"
        with tool_errors():
            result = db.query(sql, params)
        return {"schema": own, **result.as_dict()}

    @mcp.tool(annotations=READ)
    def query(
        sql: Annotated[str, Field(description="One SELECT or WITH statement. Use ? placeholders for values.")],
        params: Annotated[list[Param] | None, Field(description="Values bound to ? placeholders, in order.")] = None,
        max_rows: Annotated[
            int | None, Field(ge=1, description=f"Row cap for this call (server maximum {settings.max_rows}).")
        ] = None,
    ) -> dict[str, Any]:
        """Run a read-only query and return columns and rows. `truncated` is true when more rows exist."""
        with tool_errors():
            stmt = require_read(sql)
            return db.query(stmt.sql, params, max_rows).as_dict()

    @mcp.tool(annotations=READ)
    def explain(
        sql: Annotated[str, Field(description="One SELECT or WITH statement to plan without running it.")],
    ) -> dict[str, Any]:
        """Show the optimizer's execution plan for a query, as an indented tree plus per-step details."""
        with tool_errors():
            stmt = require_read(sql)
            steps = db.explain(stmt.sql)
        lines = []
        for s in steps:
            target = f" {s['object_owner']}.{s['object_name']}" if s.get("object_name") else ""
            cost = f"  (cost={s['cost']}, rows={s['cardinality']})" if s.get("cost") is not None else ""
            lines.append(f"{'  ' * max((s.get('depth') or 1) - 1, 0)}{s['operation']}{target}{cost}")
        return {"plan": "\n".join(lines), "steps": steps}

    if writes:

        @mcp.tool(annotations=WRITE)
        def execute(
            sql: Annotated[str, Field(description="One DML/DDL statement or PL/SQL block. Committed on success.")],
            params: Annotated[
                list[Param] | None, Field(description="Values bound to ? placeholders, in order.")
            ] = None,
        ) -> dict[str, Any]:
            """Run a statement that changes data or schema and commit it. Rolled back on error."""
            with tool_errors():
                stmt = parse_for_execute(sql)
                return db.execute(stmt.sql, params)

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tibero-mcp", description="MCP server for Tibero databases")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="bind address for streamable-http")
    parser.add_argument("--port", type=int, default=8000, help="port for streamable-http")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as e:
        missing = [".".join(str(p) for p in err["loc"]) for err in e.errors() if err["type"] == "missing"]
        if missing:
            names = ", ".join(f"TIBERO_{m.upper()}" for m in missing)
            sys.exit(f"tibero-mcp: missing required settings: {names} (environment or .env)")
        sys.exit(f"tibero-mcp: invalid settings:\n{e}")

    server = build_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
