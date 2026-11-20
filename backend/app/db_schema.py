import json
import logging
from typing import Any, Dict

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

-- Extend snmp_devices with Vendor MIB matching & hardware telemetry columns
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS vendor_name TEXT;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS device_category TEXT;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS matched_mibs JSONB DEFAULT '[]'::jsonb;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS hardware_model TEXT;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS serial_number TEXT;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS firmware_version TEXT;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS sys_contact TEXT;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS temp_celsius DOUBLE PRECISION;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS extended_metrics JSONB DEFAULT '{}'::jsonb;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS mac_address TEXT;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS subnet_cidr TEXT;
ALTER TABLE snmp_devices ADD COLUMN IF NOT EXISTS first_polled TIMESTAMPTZ DEFAULT NOW();

-- Extend unifi_devices with serial_number & lan_ip for accurate L3 correlation
ALTER TABLE unifi_devices ADD COLUMN IF NOT EXISTS serial_number TEXT;
ALTER TABLE unifi_devices ADD COLUMN IF NOT EXISTS lan_ip TEXT;

CREATE TABLE IF NOT EXISTS custom_mibs (
    id             SERIAL PRIMARY KEY,
    module_name    TEXT NOT NULL UNIQUE,
    filename       TEXT NOT NULL,
    organization   TEXT DEFAULT '',
    description    TEXT DEFAULT '',
    enterprise_pen TEXT DEFAULT '',
    object_count   INT DEFAULT 0,
    objects        JSONB DEFAULT '[]'::jsonb,
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS snmp_poll_metrics (
    time                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    device_ip            TEXT NOT NULL,
    success              BOOLEAN NOT NULL DEFAULT TRUE,
    poll_duration_ms     DOUBLE PRECISION NOT NULL DEFAULT 0,
    oid_walks            INT NOT NULL DEFAULT 0,
    oids_collected       INT NOT NULL DEFAULT 0,
    interfaces_ingested  INT NOT NULL DEFAULT 0,
    responding_mib_count INT NOT NULL DEFAULT 0,
    snmp_version         TEXT DEFAULT 'v2c',
    error_message        TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_snmp_poll_metrics_time
    ON snmp_poll_metrics (time DESC);
CREATE INDEX IF NOT EXISTS idx_snmp_poll_metrics_dev_time
    ON snmp_poll_metrics (device_ip, time DESC);

CREATE TABLE IF NOT EXISTS ups_metrics_raw (
    time                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    device_ip             TEXT NOT NULL,
    battery_charge_pct    DOUBLE PRECISION,
    output_load_pct       DOUBLE PRECISION,
    runtime_remaining_sec INT,
    input_voltage_v       DOUBLE PRECISION,
    output_voltage_v      DOUBLE PRECISION,
    output_watts          DOUBLE PRECISION,
    battery_temp_c        DOUBLE PRECISION,
    output_status         TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ups_metrics_raw_dev_time
    ON ups_metrics_raw (device_ip, time DESC);
"""


async def ensure_dynamic_config_tables(pool: asyncpg.Pool) -> None:
    """Ensures dynamic configuration tables and default settings exist."""
    async with pool.acquire() as conn:
        await conn.execute(DYNAMIC_CONFIG_DDL)

        for tbl, ret_days in (("snmp_poll_metrics", 30), ("ups_metrics_raw", 180)):
            try:
                await conn.execute(
                    f"SELECT create_hypertable('{tbl}', 'time', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE)"
                )
            except Exception as exc:
                logger.debug("create_hypertable(%s) note: %s", tbl, exc)
            try:
                await conn.execute(
                    f"SELECT add_retention_policy('{tbl}', INTERVAL '{ret_days} days', if_not_exists => TRUE)"
                )
            except Exception as exc:
                logger.debug("add_retention_policy(%s) note: %s", tbl, exc)

        await conn.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES ('onboarding', $1::jsonb)
            ON CONFLICT (key) DO NOTHING
            """,
            json.dumps({"completed": False, "skipped": False}),
        )

        await conn.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES ('isp_settings', $1::jsonb)
            ON CONFLICT (key) DO NOTHING
            """,
            json.dumps({"isp_name": "", "download_mbps": 1000, "upload_mbps": 1000}),
        )

        count = await conn.fetchval("SELECT COUNT(*) FROM snmp_profiles")
        if not count:
            await conn.execute(
                """
                INSERT INTO snmp_profiles (name, version, port, community, poll_interval)
                VALUES ('Default v2c (public)', 'v2c', 161, 'public', 60)
                ON CONFLICT (name) DO NOTHING
                """
            )

    await repair_implausible_snmp_spikes(pool)


async def repair_implausible_snmp_spikes(
    pool: asyncpg.Pool, force_refresh: bool = False
) -> Dict[str, Any]:
    """Zeros out historical SNMP counter-drop/zero-baseline/rate-mismatch spike artifacts across all devices."""
    updated_rows = 0
    repaired_by_device: Dict[str, int] = {}
    try:
        async with pool.acquire() as conn:
            marker_exists = bool(
                await conn.fetchval(
                    "SELECT 1 FROM app_settings WHERE key = 'snmp_spike_cleanup_v2'"
                )
            )
            repaired_rows = await conn.fetch(
                """
                WITH if_caps AS (
                    SELECT device_ip,
                           if_index,
                           COALESCE(MAX(NULLIF(if_speed_bps, 0)), 0) AS known_if_speed
                    FROM snmp_metrics_raw
                    WHERE time > NOW() - INTERVAL '7 days'
                    GROUP BY device_ip, if_index
                ),
                dev_caps AS (
                    SELECT m.device_ip,
                           COALESCE(MAX(NULLIF(m.if_speed_bps, 0)), 0) AS known_dev_speed,
                           COALESCE(
                               BOOL_OR(
                                   d.device_category ILIKE '%ups%'
                                   OR d.device_category ILIKE '%printer%'
                                   OR (d.extended_metrics ? 'ups_telemetry')
                               ),
                               FALSE
                           ) AS is_low_bw
                    FROM snmp_metrics_raw m
                    LEFT JOIN snmp_devices d ON d.device_ip = m.device_ip
                    WHERE m.time > NOW() - INTERVAL '7 days'
                    GROUP BY m.device_ip
                ),
                windowed AS (
                    SELECT time,
                           device_ip,
                           if_index,
                           if_speed_bps,
                           in_octets,
                           out_octets,
                           in_bps,
                           out_bps,
                           sys_uptime,
                           LAG(time) OVER (PARTITION BY device_ip, if_index ORDER BY time ASC) AS prev_time,
                           LAG(time, 2) OVER (PARTITION BY device_ip, if_index ORDER BY time ASC) AS prev2_time,
                           LAG(in_octets) OVER (PARTITION BY device_ip, if_index ORDER BY time ASC) AS prev_in_oct,
                           LAG(in_octets, 2) OVER (PARTITION BY device_ip, if_index ORDER BY time ASC) AS prev2_in_oct,
                           LAG(out_octets) OVER (PARTITION BY device_ip, if_index ORDER BY time ASC) AS prev_out_oct,
                           LAG(out_octets, 2) OVER (PARTITION BY device_ip, if_index ORDER BY time ASC) AS prev2_out_oct,
                           LAG(sys_uptime) OVER (PARTITION BY device_ip, if_index ORDER BY time ASC) AS prev_uptime,
                           MAX(in_octets) OVER (
                               PARTITION BY device_ip, if_index
                               ORDER BY time ASC
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                           ) AS prior_max_in_oct,
                           MAX(out_octets) OVER (
                               PARTITION BY device_ip, if_index
                               ORDER BY time ASC
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                           ) AS prior_max_out_oct
                    FROM snmp_metrics_raw
                    WHERE time > NOW() - INTERVAL '7 days'
                ),
                scored AS (
                    SELECT r.*,
                           CASE
                               WHEN COALESCE(NULLIF(r.if_speed_bps, 0), NULLIF(ic.known_if_speed, 0), NULLIF(dc.known_dev_speed, 0), 0) > 0
                                   THEN LEAST(
                                       COALESCE(NULLIF(r.if_speed_bps, 0), NULLIF(ic.known_if_speed, 0), NULLIF(dc.known_dev_speed, 0), 0)::double precision * 1.15,
                                       40000000000.0
                                   )
                               WHEN dc.is_low_bw THEN 115000000.0
                               ELSE 10000000000.0
                           END AS row_cap_bps,
                           COALESCE(EXTRACT(EPOCH FROM (r.time - r.prev_time)), 0.0) AS dt_sec,
                           COALESCE(EXTRACT(EPOCH FROM (r.time - r.prev2_time)), 0.0) AS dt2_sec
                    FROM windowed r
                    JOIN if_caps ic ON ic.device_ip = r.device_ip AND ic.if_index = r.if_index
                    JOIN dev_caps dc ON dc.device_ip = r.device_ip
                    WHERE r.time > NOW() - INTERVAL '6 days'
                ),
                flagged AS (
                    SELECT s.time,
                           s.device_ip,
                           s.if_index,
                           (
                               s.in_bps > s.row_cap_bps
                               OR (s.prev_in_oct IS NULL AND s.in_bps > 0)
                               OR (s.prev_in_oct = 0 AND s.in_bps > 0 AND (COALESCE(s.prior_max_in_oct, 0) > 0 OR s.in_bps > 5000000.0))
                               OR (s.prev_in_oct IS NOT NULL AND s.in_octets <= s.prev_in_oct AND s.in_bps > 0)
                               OR (COALESCE(s.prev_uptime, 0) > 0 AND COALESCE(s.sys_uptime, 0) > 0 AND s.sys_uptime < s.prev_uptime AND s.in_bps > 0)
                               OR (
                                   s.dt_sec >= 5.0
                                   AND s.prev_in_oct > 0
                                   AND s.in_octets > s.prev_in_oct
                                   AND s.in_bps > 1000000.0
                                   AND s.in_bps > GREATEST(
                                       ((s.in_octets::double precision - s.prev_in_oct::double precision) * 8.0 / GREATEST(s.dt_sec, 5.0)) * 3.0,
                                       1000000.0
                                   )
                               )
                               OR (
                                   s.dt_sec > 0.0
                                   AND s.dt_sec < 5.0
                                   AND s.prev2_time IS NOT NULL
                                   AND s.dt2_sec >= 5.0
                                   AND s.prev2_in_oct > 0
                                   AND s.in_octets >= s.prev2_in_oct
                                   AND s.in_bps > 10000000.0
                                   AND s.in_bps > GREATEST(
                                       ((s.in_octets::double precision - s.prev2_in_oct::double precision) * 8.0 / GREATEST(s.dt2_sec, 5.0)) * 4.0,
                                       1000000.0
                                   )
                               )
                           ) AS bad_in,
                           (
                               s.out_bps > s.row_cap_bps
                               OR (s.prev_out_oct IS NULL AND s.out_bps > 0)
                               OR (s.prev_out_oct = 0 AND s.out_bps > 0 AND (COALESCE(s.prior_max_out_oct, 0) > 0 OR s.out_bps > 5000000.0))
                               OR (s.prev_out_oct IS NOT NULL AND s.out_octets <= s.prev_out_oct AND s.out_bps > 0)
                               OR (COALESCE(s.prev_uptime, 0) > 0 AND COALESCE(s.sys_uptime, 0) > 0 AND s.sys_uptime < s.prev_uptime AND s.out_bps > 0)
                               OR (
                                   s.dt_sec >= 5.0
                                   AND s.prev_out_oct > 0
                                   AND s.out_octets > s.prev_out_oct
                                   AND s.out_bps > 1000000.0
                                   AND s.out_bps > GREATEST(
                                       ((s.out_octets::double precision - s.prev_out_oct::double precision) * 8.0 / GREATEST(s.dt_sec, 5.0)) * 3.0,
                                       1000000.0
                                   )
                               )
                               OR (
                                   s.dt_sec > 0.0
                                   AND s.dt_sec < 5.0
                                   AND s.prev2_time IS NOT NULL
                                   AND s.dt2_sec >= 5.0
                                   AND s.prev2_out_oct > 0
                                   AND s.out_octets >= s.prev2_out_oct
                                   AND s.out_bps > 10000000.0
                                   AND s.out_bps > GREATEST(
                                       ((s.out_octets::double precision - s.prev2_out_oct::double precision) * 8.0 / GREATEST(s.dt2_sec, 5.0)) * 4.0,
                                       1000000.0
                                   )
                               )
                           ) AS bad_out
                    FROM scored s
                )
                UPDATE snmp_metrics_raw t
                SET in_bps = CASE WHEN f.bad_in THEN 0 ELSE t.in_bps END,
                    out_bps = CASE WHEN f.bad_out THEN 0 ELSE t.out_bps END
                FROM flagged f
                WHERE t.time = f.time
                  AND t.device_ip = f.device_ip
                  AND t.if_index = f.if_index
                  AND (f.bad_in OR f.bad_out)
                RETURNING t.device_ip
                """
            )
            for row in repaired_rows:
                dip = str(row["device_ip"])
                repaired_by_device[dip] = repaired_by_device.get(dip, 0) + 1
            updated_rows = len(repaired_rows)

            if updated_rows > 0:
                logger.info(
                    "Repaired %d implausible SNMP spike row(s) across %d device(s): %s",
                    updated_rows,
                    len(repaired_by_device),
                    repaired_by_device,
                )

            if updated_rows > 0 or not marker_exists or force_refresh:
                for view_name in ("snmp_metrics_hourly", "snmp_metrics_daily"):
                    try:
                        await conn.execute(
                            f"CALL refresh_continuous_aggregate('{view_name}', NOW() - INTERVAL '7 days', NOW())"
                        )
                    except Exception as exc:
                        logger.debug("Skipping refresh of %s during spike cleanup: %s", view_name, exc)

                await conn.execute(
                    """
                    INSERT INTO app_settings (key, value)
                    VALUES ('snmp_spike_cleanup_v2', $1::jsonb)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """,
                    json.dumps({"repaired_rows": updated_rows, "repaired_devices": repaired_by_device}),
                )
    except Exception as exc:
        logger.warning("SNMP spike cleanup skipped: %s", exc)
    return {"repaired_rows": updated_rows, "repaired_devices": repaired_by_device}
