"""Light SQL inspection: statement splitting and read-only classification.

This is a guard rail in front of the database, not a parser. The read-only
connection and a read-only database account remain the real protection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

READ_KEYWORDS = {"SELECT", "WITH"}
_LOCKING = re.compile(r"\bFOR\s+UPDATE\b", re.IGNORECASE)
_FIRST_WORD = re.compile(r"[A-Za-z]+")
_PLSQL_UNIT = re.compile(
    r"^\s*(BEGIN|DECLARE|CREATE\s+(OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE)\b)",
    re.IGNORECASE,
)


class SqlRejected(ValueError):
    pass


@dataclass(frozen=True)
class Statement:
    sql: str  # original text, trailing semicolon removed
    code: str  # text with comments and literals blanked out
    keyword: str

    @property
    def is_read(self) -> bool:
        return self.keyword in READ_KEYWORDS and not _LOCKING.search(self.code)


def _scan(sql: str) -> tuple[str, list[int]]:
    """Blank out comments and quoted text; return masked text and top-level ``;`` positions."""
    out: list[str] = []
    semis: list[int] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(" " * (end - i))
            i = end
        elif ch in "'\"":
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:  # doubled quote escape
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            out.append(ch + " " * (end - i - 2) + ch if end - i >= 2 else ch)
            i = end
        else:
            if ch == ";":
                semis.append(i)
            out.append(ch)
            i += 1
    return "".join(out), semis


def _trim_terminator(code: str, end: int) -> int:
    """Drop a trailing tbsql ``/`` line terminator. ``code`` and the original SQL share offsets."""
    if code[:end].endswith("/"):
        end = len(code[: end - 1].rstrip())
    return end


def parse_single(sql: str) -> Statement:
    """Validate that ``sql`` is exactly one statement and classify it."""
    if not sql or not sql.strip():
        raise SqlRejected("SQL is empty.")
    code, semis = _scan(sql)
    end = len(code.rstrip())
    if any(p < end - 1 for p in semis):
        raise SqlRejected("Only one statement per call is allowed. Split the script into separate calls.")
    if semis and semis[-1] == end - 1:
        end = len(code[: semis[-1]].rstrip())
    end = _trim_terminator(code, end)
    m = _FIRST_WORD.search(code[:end].lstrip("( \t\r\n"))
    if not m:
        raise SqlRejected("Could not find a SQL keyword.")
    return Statement(sql=sql[:end].strip(), code=code[:end].strip(), keyword=m.group(0).upper())


def parse_for_execute(sql: str) -> Statement:
    """Like :func:`parse_single`, but lets a PL/SQL unit keep its inner semicolons."""
    if not sql or not sql.strip():
        raise SqlRejected("SQL is empty.")
    code, _ = _scan(sql)
    m = _PLSQL_UNIT.match(code)
    if m:
        end = _trim_terminator(code, len(code.rstrip()))
        return Statement(sql=sql[:end].strip(), code=code[:end].strip(), keyword=m.group(1).split()[0].upper())
    return parse_single(sql)


def require_read(sql: str) -> Statement:
    stmt = parse_single(sql)
    if not stmt.is_read:
        reason = (
            "SELECT ... FOR UPDATE takes row locks"
            if stmt.keyword in READ_KEYWORDS
            else f"{stmt.keyword} is not a query"
        )
        raise SqlRejected(
            f"Read-only: {reason}. Only SELECT and WITH queries run here; "
            "use the execute tool for changes (available when TIBERO_READ_ONLY=false)."
        )
    return stmt
