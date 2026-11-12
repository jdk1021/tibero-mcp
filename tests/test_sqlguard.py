import pytest

from tibero_mcp.sqlguard import SqlRejected, parse_for_execute, parse_single, require_read


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1 FROM DUAL", "SELECT 1 FROM DUAL"),
        ("SELECT 1 FROM DUAL;", "SELECT 1 FROM DUAL"),
        ("SELECT 1 FROM DUAL\n/", "SELECT 1 FROM DUAL"),
        ("SELECT 1 FROM DUAL; -- trailing note", "SELECT 1 FROM DUAL"),
        ("SELECT ';' AS S FROM DUAL /* ; */", "SELECT ';' AS S FROM DUAL"),
        ("/*+ FULL(T) */ SELECT * FROM T;", "/*+ FULL(T) */ SELECT * FROM T"),
        ("(SELECT 1 FROM DUAL)", "(SELECT 1 FROM DUAL)"),
        ("WITH a AS (SELECT 1 x FROM DUAL) SELECT * FROM a", "WITH a AS (SELECT 1 x FROM DUAL) SELECT * FROM a"),
        ("SELECT 'it''s; fine' FROM DUAL", "SELECT 'it''s; fine' FROM DUAL"),
        ('SELECT "odd;name" FROM T', 'SELECT "odd;name" FROM T'),
    ],
)
def test_reads_are_accepted_and_normalised(sql, expected):
    assert require_read(sql).sql == expected


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE T SET A = 1",
        "delete from t",
        "INSERT INTO T VALUES (1)",
        "MERGE INTO T USING S ON (1=1) WHEN MATCHED THEN UPDATE SET A = 1",
        "DROP TABLE T",
        "TRUNCATE TABLE T",
        "BEGIN NULL; END;",
        "SELECT * FROM T FOR UPDATE",
        "SELECT * FROM T for  update nowait",
    ],
)
def test_writes_and_locks_are_rejected(sql):
    with pytest.raises(SqlRejected):
        require_read(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM DUAL; DELETE FROM T",
        "SELECT 'x' FROM DUAL; SELECT 2 FROM DUAL",
        "SELECT 1 FROM DUAL;;",
    ],
)
def test_multiple_statements_are_rejected(sql):
    with pytest.raises(SqlRejected, match="one statement"):
        parse_single(sql)


@pytest.mark.parametrize("sql", ["", "   ", "-- only a comment", "/* nothing */"])
def test_empty_input_is_rejected(sql):
    with pytest.raises(SqlRejected):
        parse_single(sql)


def test_comment_text_is_not_mistaken_for_a_keyword():
    assert parse_single("/* UPDATE */ SELECT 1 FROM DUAL").keyword == "SELECT"


@pytest.mark.parametrize(
    ("sql", "keyword", "expected"),
    [
        ("BEGIN\n  NULL;\nEND;\n/", "BEGIN", "BEGIN\n  NULL;\nEND;"),
        ("DECLARE x NUMBER; BEGIN x := 1; END;", "DECLARE", "DECLARE x NUMBER; BEGIN x := 1; END;"),
        (
            "CREATE OR REPLACE PROCEDURE p IS BEGIN NULL; END;",
            "CREATE",
            "CREATE OR REPLACE PROCEDURE p IS BEGIN NULL; END;",
        ),
        ("DELETE FROM T WHERE ID = 1;", "DELETE", "DELETE FROM T WHERE ID = 1"),
    ],
)
def test_execute_keeps_plsql_units_intact(sql, keyword, expected):
    stmt = parse_for_execute(sql)
    assert (stmt.keyword, stmt.sql) == (keyword, expected)


def test_execute_still_rejects_scripts_of_plain_statements():
    with pytest.raises(SqlRejected):
        parse_for_execute("DELETE FROM A; DELETE FROM B")


def test_read_only_rejection_does_not_point_to_a_missing_tool():
    with pytest.raises(SqlRejected) as err:
        require_read("UPDATE T SET A = 1")
    assert "execute" not in str(err.value)
    assert "read-only" in str(err.value)


def test_rejection_points_to_execute_when_writes_are_enabled():
    with pytest.raises(SqlRejected, match="execute tool"):
        require_read("UPDATE T SET A = 1", writes_enabled=True)
