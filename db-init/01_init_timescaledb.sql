-- ==============================================================================
-- NetDash TimescaleDB Initialization Schema
-- Hypertables, Columnar Compression Policies & Indefinite Continuous Aggregates
-- ==============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ==============================================================================
-- 1. UniFi Dream Machine Pro (UDM Pro) Telemetry Tables
-- ==============================================================================

CREATE TABLE IF NOT EXISTS unifi_gateway_metrics (
    time                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    site_name           TEXT NOT NULL DEFAULT 'default',
    gateway_mac         TEXT,
    gateway_name        TEXT,
    gateway_model       TEXT,
    gateway_version     TEXT,
    wan_ip              TEXT,
    wan_isp             TEXT,
    wan_status          TEXT DEFAULT 'online',
    wan_rx_bps          DOUBLE PRECISION DEFAULT 0,
    wan_tx_bps          DOUBLE PRECISION DEFAULT 0,
    wan_latency_ms      DOUBLE PRECISION DEFAULT 0,
    wan_packet_loss_pct DOUBLE PRECISION DEFAULT 0,
    wan_uptime_sec      BIGINT DEFAULT 0,
    cpu_usage_pct       DOUBLE PRECISION DEFAULT 0,
    mem_usage_pct       DOUBLE PRECISION DEFAULT 0,
    temp_celsius        DOUBLE PRECISION,
    total_poe_watts     DOUBLE PRECISION DEFAULT 0,
    active_wan_clients  INT DEFAULT 0,
    active_lan_clients  INT DEFAULT 0,
    active_wlan_clients INT DEFAULT 0,
    total_devices       INT DEFAULT 0,
    online_devices      INT DEFAULT 0,
    raw_health          JSONB DEFAULT '{}'::jsonb
);

SELECT create_hypertable(
    'unifi_gateway_metrics',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_unifi_gw_time ON unifi_gateway_metrics (time DESC);

ALTER TABLE unifi_gateway_metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'site_name',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('unifi_gateway_metrics', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('unifi_gateway_metrics', INTERVAL '90 days', if_not_exists => TRUE);

-- Current state of adopted UniFi infrastructure (Gateway, Switches, Access Points)
CREATE TABLE IF NOT EXISTS unifi_devices (
    mac              TEXT PRIMARY KEY,
    ip_address       TEXT,
    name             TEXT,
    model            TEXT,
    device_type      TEXT,
    firmware_version TEXT,
    status           TEXT DEFAULT 'online',
    uptime_sec       BIGINT DEFAULT 0,
    cpu_usage_pct    DOUBLE PRECISION DEFAULT 0,
    mem_usage_pct    DOUBLE PRECISION DEFAULT 0,
    temp_celsius     DOUBLE PRECISION,
    poe_total_watts  DOUBLE PRECISION DEFAULT 0,
    poe_max_watts    DOUBLE PRECISION DEFAULT 0,
    num_sta          INT DEFAULT 0,
    rx_bps           DOUBLE PRECISION DEFAULT 0,
    tx_bps           DOUBLE PRECISION DEFAULT 0,
    port_table       JSONB DEFAULT '[]'::jsonb,
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Current state of active UniFi wired/wireless clients
CREATE TABLE IF NOT EXISTS unifi_clients (
    mac          TEXT PRIMARY KEY,
    ip_address   TEXT,
    hostname     TEXT,
    oui_vendor   TEXT,
    is_wired     BOOLEAN DEFAULT TRUE,
    network_name TEXT,
    vlan_id      INT,
    sw_mac       TEXT,
    sw_port      INT,
    ap_mac       TEXT,
    essid        TEXT,
    channel      INT,
    rssi         INT,
    signal_dbm   INT,
    rx_bps       DOUBLE PRECISION DEFAULT 0,
    tx_bps       DOUBLE PRECISION DEFAULT 0,
    rx_bytes     BIGINT DEFAULT 0,
    tx_bytes     BIGINT DEFAULT 0,
    uptime_sec   BIGINT DEFAULT 0,
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_unifi_clients_ip ON unifi_clients (ip_address);

-- ==============================================================================
-- 2. Asynchronous SNMP Polling & Indefinite Trend Retention
-- ==============================================================================

CREATE TABLE IF NOT EXISTS snmp_devices (
    device_ip             TEXT PRIMARY KEY,
    sys_name              TEXT,
    sys_descr             TEXT,
    sys_object_id         TEXT,
    sys_location          TEXT,
    sys_contact           TEXT,
    sys_uptime_ticks      BIGINT DEFAULT 0,
    cpu_usage_pct         DOUBLE PRECISION DEFAULT 0,
    mem_usage_pct         DOUBLE PRECISION DEFAULT 0,
    temp_celsius          DOUBLE PRECISION,
    interface_count       INT DEFAULT 0,
    vendor_name           TEXT,
    device_category       TEXT,
    matched_mibs          JSONB DEFAULT '[]'::jsonb,
    hardware_model        TEXT,
    serial_number         TEXT,
    firmware_version      TEXT,
    extended_metrics      JSONB DEFAULT '{}'::jsonb,
    is_reachable          BOOLEAN DEFAULT TRUE,
    last_poll_duration_ms DOUBLE PRECISION DEFAULT 0,
    last_polled           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS snmp_metrics_raw (
    time           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    device_ip      TEXT NOT NULL,
    hostname       TEXT,
    if_index       INT NOT NULL,
    if_name        TEXT,
    if_descr       TEXT,
    if_alias       TEXT,
    if_speed_bps   BIGINT DEFAULT 0,
    if_oper_status INT DEFAULT 1,
    in_octets      BIGINT DEFAULT 0,
    out_octets     BIGINT DEFAULT 0,
    in_bps         DOUBLE PRECISION DEFAULT 0,
    out_bps        DOUBLE PRECISION DEFAULT 0,
    in_errors      BIGINT DEFAULT 0,
    out_errors     BIGINT DEFAULT 0,
    in_discards    BIGINT DEFAULT 0,
    out_discards   BIGINT DEFAULT 0,
    cpu_usage      DOUBLE PRECISION DEFAULT 0,
    mem_usage      DOUBLE PRECISION DEFAULT 0,
    sys_uptime     BIGINT DEFAULT 0
);

SELECT create_hypertable(
    'snmp_metrics_raw',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_snmp_raw_device_if_time
    ON snmp_metrics_raw (device_ip, if_index, time DESC);

ALTER TABLE snmp_metrics_raw SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_ip, if_index',
    timescaledb.compress_orderby = 'time DESC'
);

-- Compress raw 1-minute SNMP chunks older than 7 days; drop raw chunks older than 180 days
SELECT add_compression_policy('snmp_metrics_raw', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('snmp_metrics_raw', INTERVAL '180 days', if_not_exists => TRUE);

-- Continuous Aggregate: Hourly Rollup (Retained Indefinitely)
CREATE MATERIALIZED VIEW IF NOT EXISTS snmp_metrics_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    device_ip,
    MAX(hostname) AS hostname,
    if_index,
    MAX(if_name) AS if_name,
    MAX(if_descr) AS if_descr,
    MAX(if_alias) AS if_alias,
    MAX(if_speed_bps) AS if_speed_bps,
    MIN(in_bps) AS min_in_bps,
    AVG(in_bps) AS avg_in_bps,
    MAX(in_bps) AS max_in_bps,
    LEAST(MAX(in_bps), AVG(in_bps) + 1.645 * COALESCE(STDDEV(in_bps), 0)) AS p95_in_bps,
    MIN(out_bps) AS min_out_bps,
    AVG(out_bps) AS avg_out_bps,
    MAX(out_bps) AS max_out_bps,
    LEAST(MAX(out_bps), AVG(out_bps) + 1.645 * COALESCE(STDDEV(out_bps), 0)) AS p95_out_bps,
    GREATEST(0, MAX(in_errors) - MIN(in_errors)) AS delta_in_errors,
    GREATEST(0, MAX(out_errors) - MIN(out_errors)) AS delta_out_errors,
    AVG(cpu_usage) AS avg_cpu_usage,
    MAX(cpu_usage) AS max_cpu_usage,
    AVG(mem_usage) AS avg_mem_usage,
    MAX(mem_usage) AS max_mem_usage,
    COUNT(*) AS sample_count
FROM snmp_metrics_raw
GROUP BY bucket, device_ip, if_index
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'snmp_metrics_hourly',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '15 minutes',
    if_not_exists => TRUE
);

-- Continuous Aggregate: Daily Rollup (Retained Indefinitely for Multi-Year Trend Graphing)
CREATE MATERIALIZED VIEW IF NOT EXISTS snmp_metrics_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time) AS bucket,
    device_ip,
    MAX(hostname) AS hostname,
    if_index,
    MAX(if_name) AS if_name,
    MAX(if_descr) AS if_descr,
    MAX(if_alias) AS if_alias,
    MAX(if_speed_bps) AS if_speed_bps,
    MIN(in_bps) AS min_in_bps,
    AVG(in_bps) AS avg_in_bps,
    MAX(in_bps) AS max_in_bps,
    LEAST(MAX(in_bps), AVG(in_bps) + 1.645 * COALESCE(STDDEV(in_bps), 0)) AS p95_in_bps,
    MIN(out_bps) AS min_out_bps,
    AVG(out_bps) AS avg_out_bps,
    MAX(out_bps) AS max_out_bps,
    LEAST(MAX(out_bps), AVG(out_bps) + 1.645 * COALESCE(STDDEV(out_bps), 0)) AS p95_out_bps,
    GREATEST(0, MAX(in_errors) - MIN(in_errors)) AS delta_in_errors,
    GREATEST(0, MAX(out_errors) - MIN(out_errors)) AS delta_out_errors,
    AVG(cpu_usage) AS avg_cpu_usage,
    MAX(cpu_usage) AS max_cpu_usage,
    AVG(mem_usage) AS avg_mem_usage,
    MAX(mem_usage) AS max_mem_usage,
    COUNT(*) AS sample_count
FROM snmp_metrics_raw
GROUP BY bucket, device_ip, if_index
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'snmp_metrics_daily',
    start_offset => INTERVAL '14 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- ==============================================================================
-- 3. Allowed-Subnet Network Scanner (<10 Subnets)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS discovered_hosts (
    ip_address       INET PRIMARY KEY,
    subnet_cidr      TEXT NOT NULL,
    mac_address      TEXT,
    hostname         TEXT,
    vendor           TEXT,
    vlan_id          INT,
    is_online        BOOLEAN NOT NULL DEFAULT TRUE,
    latency_ms       DOUBLE PRECISION,
    discovery_source TEXT DEFAULT 'nmap',
    switch_mac       TEXT,
    switch_port      INT,
    open_ports       JSONB DEFAULT '[]'::jsonb,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovered_hosts_subnet ON discovered_hosts (subnet_cidr, ip_address);
CREATE INDEX IF NOT EXISTS idx_discovered_hosts_mac ON discovered_hosts (mac_address);

CREATE TABLE IF NOT EXISTS subnet_scan_runs (
    scan_time         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    subnet_cidr       TEXT NOT NULL,
    total_ips         INT NOT NULL DEFAULT 0,
    hosts_up          INT NOT NULL DEFAULT 0,
    new_hosts         INT NOT NULL DEFAULT 0,
    scan_duration_sec DOUBLE PRECISION NOT NULL DEFAULT 0
);

SELECT create_hypertable(
    'subnet_scan_runs',
    'scan_time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

SELECT add_retention_policy('subnet_scan_runs', INTERVAL '180 days', if_not_exists => TRUE);

-- ==============================================================================
-- 4. Remote Syslog Ingestion (Vector -> device_logs Hypertable)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS device_logs (
    time         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_ip    TEXT NOT NULL,
    hostname     TEXT,
    facility     TEXT DEFAULT 'user',
    severity     TEXT DEFAULT 'info',
    severity_num INT DEFAULT 6,
    app_name     TEXT,
    proc_id      TEXT,
    message      TEXT NOT NULL,
    raw_log      TEXT
);

SELECT create_hypertable(
    'device_logs',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_device_logs_time ON device_logs (time DESC);
CREATE INDEX IF NOT EXISTS idx_device_logs_source_time ON device_logs (source_ip, time DESC);
CREATE INDEX IF NOT EXISTS idx_device_logs_severity_time ON device_logs (severity, time DESC);
CREATE INDEX IF NOT EXISTS idx_device_logs_message_fts ON device_logs USING GIN (to_tsvector('english', message));

ALTER TABLE device_logs SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source_ip, severity',
    timescaledb.compress_orderby = 'time DESC'
);

-- Compress log chunks older than 3 days; drop chunks older than 30 days
SELECT add_compression_policy('device_logs', INTERVAL '3 days', if_not_exists => TRUE);
SELECT add_retention_policy('device_logs', INTERVAL '30 days', if_not_exists => TRUE);

-- ==============================================================================
-- 5. Dynamic Dashboard Configuration (Onboarding, SNMP Profiles, Hosts, Subnets)
-- ==============================================================================

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

