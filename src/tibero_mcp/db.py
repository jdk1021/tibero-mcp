"""JDBC access through JPype, with one lazily opened, self-healing connection."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

import jpype

from .config import Settings
from .driver import start_jvm

log = logging.getLogger(__name__)

T = TypeVar("T")
Param = str | int | float | bool | None

MAX_TEXT = 4000  # characters kept from a single LOB/long text value


class DatabaseError(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
        }


class _Types:
    """java.sql.Types constants, loaded after the JVM starts."""

    def __init__(self) -> None:
        t = jpype.JClass("java.sql.Types")
        self.integral = {int(t.TINYINT), int(t.SMALLINT), int(t.INTEGER), int(t.BIGINT)}
        self.decimal = {int(t.NUMERIC), int(t.DECIMAL)}
        self.floating = {int(t.FLOAT), int(t.REAL), int(t.DOUBLE)}
        self.temporal = {int(t.DATE), int(t.TIME), int(t.TIMESTAMP)}
        self.temporal_tz = {int(t.TIMESTAMP_WITH_TIMEZONE), int(t.TIME_WITH_TIMEZONE)}
        self.clob = {int(t.CLOB), int(t.NCLOB), int(t.LONGVARCHAR), int(t.LONGNVARCHAR)}
        self.blob = {int(t.BLOB)}
        self.binary = {int(t.BINARY), int(t.VARBINARY), int(t.LONGVARBINARY)}
        self.boolean = {int(t.BOOLEAN), int(t.BIT)}


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return f"{text[:MAX_TEXT]}… [{len(text) - MAX_TEXT} more characters]"


def _number(bd: Any) -> int | float | str:
    plain = str(bd.stripTrailingZeros().toPlainString())
    if "." not in plain:
        return int(plain)
    return float(plain) if len(plain.replace("-", "").replace(".", "")) <= 15 else plain


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._conn: Any = None
        self._types: _Types | None = None
        self.current_user: str | None = None

    # ── connection ──────────────────────────────────────────────
    def _connect(self) -> Any:
        start_jvm(self.settings)
        if self._types is None:
            self._types = _Types()
            system = jpype.JClass("java.lang.System")
            system.setOut(system.err)  # keep stdout clean for the stdio transport
        props = jpype.JClass("java.util.Properties")()
        props.setProperty("user", self.settings.user)
        props.setProperty("password", self.settings.password.get_secret_value())
        dm = jpype.JClass("java.sql.DriverManager")
        dm.setLoginTimeout(15)
        try:
            conn = dm.getConnection(self.settings.jdbc_url, props)
        except jpype.JException as e:
            raise DatabaseError(
                f"Could not connect to {self.settings.host}:{self.settings.port}/{self.settings.dbname} "
                f"as {self.settings.user}: {e.getMessage()}"
            ) from None
        conn.setAutoCommit(False)
        conn.setReadOnly(self.settings.read_only)
        stmt = conn.createStatement()
        try:
            rs = stmt.executeQuery("SELECT USER FROM DUAL")
            rs.next()
            self.current_user = str(rs.getString(1))
        finally:
            stmt.close()
        log.info(
            "Connected to %s as %s (read_only=%s)",
            self.settings.jdbc_url,
            self.current_user,
            self.settings.read_only,
        )
        return conn

    def _healthy(self) -> bool:
        try:
            return self._conn is not None and bool(self._conn.isValid(3))
        except jpype.JException:
            return False

    def _drop(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(jpype.JException):
                self._conn.close()
        self._conn = None

    def run(self, work: Callable[[Any], T], *, commit: bool = False) -> T:
        """Run ``work(connection)`` under the lock; reconnect once if the link dropped."""
        with self._lock:
            for attempt in (1, 2):
                if self._conn is None:
                    self._conn = self._connect()
                try:
                    result = work(self._conn)
                    if commit:
                        self._conn.commit()
                    else:
                        self._conn.rollback()
                    return result
                except jpype.JException as e:
                    message = str(e.getMessage() or e)
                    if attempt == 1 and not commit and not self._healthy():
                        log.warning("Connection lost (%s); reconnecting", message)
                        self._drop()
                        continue
                    try:
                        self._conn.rollback()
                    except jpype.JException:
                        self._drop()
                    raise DatabaseError(message) from None
            raise AssertionError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._drop()

    # ── statements ──────────────────────────────────────────────
    def _prepare(self, conn: Any, sql: str, params: Sequence[Param] | None) -> Any:
        stmt = conn.prepareStatement(sql)
        if self.settings.query_timeout:
            stmt.setQueryTimeout(self.settings.query_timeout)
        for i, value in enumerate(params or (), start=1):
            if value is None:
                stmt.setObject(i, None)
            elif isinstance(value, bool):
                stmt.setBoolean(i, value)
            elif isinstance(value, int):
                stmt.setLong(i, value)
            elif isinstance(value, float):
                stmt.setDouble(i, value)
            else:
                stmt.setString(i, str(value))
        return stmt

    def _value(self, rs: Any, i: int, sql_type: int) -> Any:
        t = self._types
        assert t is not None
        if sql_type in t.integral:
            v = rs.getLong(i)
            return None if rs.wasNull() else int(v)
        if sql_type in t.decimal:
            bd = rs.getBigDecimal(i)
            return None if bd is None else _number(bd)
        if sql_type in t.floating:
            v = rs.getDouble(i)
            return None if rs.wasNull() else float(v)
        if sql_type in t.temporal:
            ts = rs.getTimestamp(i)
            if ts is None:
                return None
            return str(ts.toLocalDateTime().toString()).replace("T", " ")
        if sql_type in t.boolean:
            v = rs.getBoolean(i)
            return None if rs.wasNull() else bool(v)
        if sql_type in t.blob:
            blob = rs.getBlob(i)
            return None if blob is None else f"<BLOB {int(blob.length())} bytes>"
        if sql_type in t.binary:
            data = rs.getBytes(i)
            if data is None:
                return None
            raw = bytes(data)
            return raw.hex() if len(raw) <= 64 else f"<binary {len(raw)} bytes>"
        v = rs.getString(i)
        return None if v is None else _clip(str(v))

    def _read(self, rs: Any, limit: int) -> QueryResult:
        meta = rs.getMetaData()
        count = int(meta.getColumnCount())
        columns = [str(meta.getColumnLabel(i)) for i in range(1, count + 1)]
        types = [int(meta.getColumnType(i)) for i in range(1, count + 1)]
        result = QueryResult(columns=columns)
        while rs.next():
            if len(result.rows) >= limit:
                result.truncated = True
                break
            result.rows.append([self._value(rs, i, types[i - 1]) for i in range(1, count + 1)])
        return result

    def query(self, sql: str, params: Sequence[Param] | None = None, max_rows: int | None = None) -> QueryResult:
        limit = min(max_rows or self.settings.max_rows, self.settings.max_rows)

        def work(conn: Any) -> QueryResult:
            started = time.perf_counter()
            stmt = self._prepare(conn, sql, params)
            try:
                stmt.setMaxRows(limit + 1)
                stmt.setFetchSize(min(limit + 1, 500))
                rs = stmt.executeQuery()
                result = self._read(rs, limit)
            finally:
                stmt.close()
            result.elapsed_ms = round((time.perf_counter() - started) * 1000)
            return result

        return self.run(work)

    def execute(self, sql: str, params: Sequence[Param] | None = None) -> dict[str, Any]:
        def work(conn: Any) -> dict[str, Any]:
            started = time.perf_counter()
            stmt = self._prepare(conn, sql, params)
            try:
                has_rows = bool(stmt.execute())
                if has_rows:
                    out = self._read(stmt.getResultSet(), self.settings.max_rows).as_dict()
                else:
                    count = int(stmt.getUpdateCount())
                    out = {"affected_rows": count if count >= 0 else None}  # -1 for DDL / PL/SQL
            finally:
                stmt.close()
            out["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            out["committed"] = True
            return out

        return self.run(work, commit=True)

    def explain(self, sql: str) -> list[dict[str, Any]]:
        statement_id = f"MCP{int(time.time() * 1000) % 10**12}"

        def work(conn: Any) -> list[dict[str, Any]]:
            stmt = conn.createStatement()
            try:
                stmt.execute(f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' FOR {sql}")
            finally:
                stmt.close()
            ps = self._prepare(
                conn,
                "SELECT ID, PARENT_ID, DEPTH, OPERATION, OBJECT_OWNER, OBJECT_NAME, COST, CARDINALITY, "
                "ACCESS_PREDICATES, FILTER_PREDICATES FROM PLAN_TABLE WHERE STATEMENT_ID = ? ORDER BY ID",
                [statement_id],
            )
            try:
                rows = self._read(ps.executeQuery(), 1000)
            finally:
                ps.close()
            return [dict(zip([c.lower() for c in rows.columns], r, strict=True)) for r in rows.rows]

        # run() rolls back afterwards, which also removes the PLAN_TABLE rows
        return self.run(work)
