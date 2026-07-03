"""fm — a thin, read-only CLI over every First Motive repo.

One discoverable, machine-readable surface for developers and AI agents: which
fm-* repos exist (``list``), their cross-repo git state (``status``), and
environment health (``doctor``). The CLI owns cross-repo verbs only — bootstrap
logic stays in each repo's own scripts, so v1 shells out to ``git`` and to
declared health checks, nothing more.
"""
