"""Discord terminal bridge — running terminal sessions announce themselves.

The terminal stays the primary interface; Discord is a side channel that lets
you watch a session from your phone and answer its approval gates.

Split by layer: ``register_callbacks`` (lifecycle + configuration),
``bindings``/``authz`` (identity), ``approvals``/``approvals_ui`` (gates),
``chunking``/``rendering`` (message shaping) and ``constants``/``session_ids``
(the values and formats every layer has to agree on).
"""
