"""Entry point: ``python -m brigada.mcp_server`` serves the tools over stdio."""

from brigada.mcp_server.server import main
from brigada.observability.tracing import setup_tracing

if __name__ == "__main__":
    # Before serving, so every tool call has a real span (console spans go to stderr: stdout
    # carries the protocol).
    setup_tracing(service_name="brigada-mcp")
    main()
