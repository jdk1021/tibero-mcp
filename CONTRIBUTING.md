# Contributing

Thanks for helping. Issues and pull requests are welcome in English or Korean.

## Set up

```bash
git clone https://github.com/jdk1021/tibero-mcp.git
cd tibero-mcp
uv sync
```

## Before you open a pull request

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Unit tests run without a database or a JVM. If your change touches SQL against the data dictionary, JDBC type handling or the driver download, also try it against a real Tibero instance and say in the pull request which Tibero version you used.

## Guidelines

- Keep the server read-only by default. Anything that can change data must stay behind `TIBERO_READ_ONLY=false`.
- Tool descriptions and error messages are read by models. Make them say what happened and what to do next.
- Never commit credentials, `.env` files, connection strings for real systems, table or column names from real systems, or the Tibero JDBC jar. Use generic examples such as `SALES_ORDER`.
- One topic per pull request, with tests for new behaviour.

## Reporting security issues

Please do not open a public issue. See [SECURITY.md](SECURITY.md).
