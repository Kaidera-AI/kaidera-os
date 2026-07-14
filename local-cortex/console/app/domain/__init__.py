"""Functional core (the domain) — imports NOTHING outward.

Modules here hold pure interfaces + DTOs (dataclasses, Protocols, value logic)
for the console's vertical modules. The arrows-point-inward rule (ratified design
§3): domain code must not import httpx / fastapi / subprocess / psycopg2 /
asyncpg — adapters depend on the domain, never the reverse. A guard test
enforces this for each domain module.
"""
