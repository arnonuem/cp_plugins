"""Code Puppy as a Discord bot — one independent agent run per channel.

The plugin is split by layer: ``concurrency`` (session attribution + per-session
approval locks), ``gateway``/``register_callbacks`` (transport), ``bindings``/
``authz`` (identity), ``approvals`` (gates) and ``output`` (routing).
"""
