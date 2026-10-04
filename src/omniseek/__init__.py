"""OmniSeek: a self-hosted perception MCP server.

The package root. The MCP tool surface lives in ``omniseek.server``, the HTTP
service in ``omniseek.serve_http``, and the retrieval engine (sources, ranking,
relation graph) under ``omniseek.core``.
"""

__version__ = "0.2.1"

# Every process that runs eye code imports this package first, so this is the one place that cannot
# be skipped: log records and uncaught tracebacks get their credentials masked (see omniseek.redact).
from omniseek import redact as _redact  # noqa: E402

_redact.install()
