"""The `hdss-site` dashboard template: one approved layout, many HDSS sites.

A dashboard is a small spec (spec.py) validated against the scoped bindings file (bindings.yaml,
read by registry.py). The page asks views/hdss.py for per-card aggregates (engine.py); person- and
household-level rows never leave the server.
"""
