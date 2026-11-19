import json
import logging
import asyncpg

logger = logging.getLogger("netdash.schema")

DYNAMIC_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS snmp_profiles (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    version       TEXT NOT NULL DEFAULT 'v2c',
    port          INT NOT NULL DEFAULT 161,
    community     TEXT DEFAULT 'public',
    v3_user       TEXT DEFAULT '',
    v3_auth_proto TEXT DEFAULT 'SHA',
    v3_auth_key   TEXT DEFAULT '',
    v3_priv_proto TEXT DEFAULT 'AES',
    v3_priv_key   TEXT DEFAULT '',
    poll_interval INT NOT NULL DEFAULT 60,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS snmp_targets_config (
    id         SERIAL PRIMARY KEY,
    device_ip  TEXT NOT NULL UNIQUE,
    label      TEXT DEFAULT '',
    profile_id INT REFERENCES snmp_profiles(id) ON DELETE SET NULL,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scan_subnets_config (
    id          SERIAL PRIMARY KEY,
    subnet_cidr TEXT NOT NULL UNIQUE,
    label       TEXT DEFAULT '',
    vlan_id     INT,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS unifi_controllers_config (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT 'UDM Pro',
    host          TEXT NOT NULL UNIQUE,
    api_key       TEXT NOT NULL DEFAULT '',
    site_name     TEXT NOT NULL DEFAULT 'default',
    verify_ssl    BOOLEAN NOT NULL DEFAULT FALSE,
    poll_interval INT NOT NULL DEFAULT 30,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def ensure_dynamic_config_tables(pool: asyncpg.Pool) -> None:
    """Ensures dynamic configuration tables and default settings exist."""
    async with pool.acquire() as conn:
        await conn.execute(DYNAMIC_CONFIG_DDL)

        # Ensure default onboarding setting exists
        await conn.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES ('onboarding', $1::jsonb)
            ON CONFLICT (key) DO NOTHING
            """,
            json.dumps({"completed": False, "skipped": False}),
        )

        # Seed a default 'Default v2c (public)' SNMP profile if table is empty
        count = await conn.fetchval("SELECT COUNT(*) FROM snmp_profiles")
        if not count:
            await conn.execute(
                """
                INSERT INTO snmp_profiles (name, version, port, community, poll_interval)
                VALUES ('Default v2c (public)', 'v2c', 161, 'public', 60)
                ON CONFLICT (name) DO NOTHING
                """
            )
