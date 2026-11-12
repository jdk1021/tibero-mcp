# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-12

### Added

- Tools: `list_schemas`, `list_tables`, `describe_table`, `search_columns`, `query`, `explain`, and `execute` when writes are enabled.
- `offset` paging for `list_tables`, `search_columns` and `query`; truncated results carry `next_offset`.
- `search_columns` shows full column types such as `VARCHAR(20)` and takes `table_search` to narrow by table name or comment.
- Read-only mode by default: statement classification, one statement per call, `FOR UPDATE` refused, rollback after every read.
- Official Tibero JDBC driver (`com.tmaxtibero:tbjdbc8:7.2.6`) fetched from Maven Central, verified by SHA-1 and cached.
- stdio and Streamable HTTP transports, automatic reconnect, clean shutdown.
