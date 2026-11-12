# Security policy

## Supported versions

Security fixes go into the latest release on `main`.

## Reporting a vulnerability

Report privately through [GitHub security advisories](https://github.com/jdk1021/tibero-mcp/security/advisories/new). Please include the version, your configuration (without secrets) and steps to reproduce. You should get a first response within a week.

Examples of what we want to hear about:

- A statement that gets past the read-only check and changes data while `TIBERO_READ_ONLY=true`
- Credentials or connection details leaking into tool results, logs or error messages
- A way to make the server load a driver jar other than the pinned one without `TIBERO_JDBC_JAR`

## Deployment advice

- Connect with a database account that has only the grants the agent needs, ideally `SELECT` only. The server's read-only checks are a guard rail, not a substitute for database permissions.
- The Streamable HTTP transport has no authentication. Keep it bound to `127.0.0.1` or behind an authenticating proxy.
- Keep `.env` out of version control and readable only by the user that runs the server.
