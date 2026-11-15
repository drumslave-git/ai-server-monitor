"""Read-only inspection of the host for the chat: a sandboxed command runner, disk usage with
owner attribution, container summaries and metric history.

Nothing here writes to the host. Commands run chrooted into the read-only host root as an
unprivileged user (see `sandbox.py`); everything that comes back is size-capped and scrubbed.
"""
