import React, { useEffect, useId, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowDownRight,
  ArrowLeft,
  ArrowUp,
  ArrowUpDown,
  ArrowUpRight,
  Battery,
  BatteryCharging,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock,
  Cpu,
  Database,
  DownloadCloud,
  Edit3,
  Eye,
  EyeOff,
  HardDrive,
  KeyRound,
  Menu,
  Monitor,
  Network,
  Play,
  Plus,
  Printer,
  RefreshCw,
  Router,
  Search,
  Server,
  Settings,
  ShieldCheck,
  Square,
  Terminal,
  Thermometer,
  Trash2,
  X,
  Zap,
} from "lucide-react";

type TabKey = "pulse" | "devices" | "snmp" | "scanner" | "syslog" | "targets" | "settings";

const TAB_PATHS: Record<TabKey, string> = {
  pulse: "/",
  devices: "/devices",
  snmp: "/snmp",
  scanner: "/scanner",
  syslog: "/syslog",
  targets: "/targets",
  settings: "/settings",
};

function parseAppRoute(pathname: string): { tab: TabKey; deviceRef: string | null } {
  const clean = (pathname || "/").replace(/\/+$/, "") || "/";
  if (clean === "/" || clean === "/overview" || clean === "/pulse") {
    return { tab: "pulse", deviceRef: null };
  }
  if (clean === "/devices") {
    return { tab: "devices", deviceRef: null };
  }
  if (clean.startsWith("/devices/")) {
    const rawRef = clean.slice("/devices/".length).split("/")[0] || "";
    try {
      const decoded = decodeURIComponent(rawRef).trim();
      return { tab: "devices", deviceRef: decoded || null };
    } catch {
      return { tab: "devices", deviceRef: rawRef.trim() || null };
    }
  }
  if (clean === "/snmp") return { tab: "snmp", deviceRef: null };
  if (clean === "/scanner") return { tab: "scanner", deviceRef: null };
  if (clean === "/syslog") return { tab: "syslog", deviceRef: null };
  if (clean === "/targets") return { tab: "targets", deviceRef: null };
  if (clean === "/settings") return { tab: "settings", deviceRef: null };
  return { tab: "pulse", deviceRef: null };
}

function isModifiedClick(e: React.MouseEvent): boolean {
  return e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0;
}

type DeviceSortKey =
  | "device_id"
  | "status"
  | "ip_address"
  | "hostname"
  | "mac_address"
  | "vendor"
  | "subnet"
  | "snmp"
  | "sources";
type SortDirection = "asc" | "desc";

interface UpdateProgressComponent {
  id: string;
  name: string;
  status: "pending" | "running" | "completed" | "error" | string;
  pct: number;
  detail: string;
}

interface UpdateProgressState {
  status: "idle" | "updating" | "completed" | "error";
  update_id?: string;
  overall_pct: number;
  phase: string;
  message: string;
  from_sha?: string;
  target_sha?: string;
  from_version?: string;
  target_version?: string;
  started_at?: string;
  updated_at?: string;
  components: UpdateProgressComponent[];
}

interface BackgroundSubService {
  key: string;
  name: string;
  status: string;
  interval_sec: number | null;
  last_run: string | null;
  detail: string;
}

interface BackgroundServiceInfo {
  id: string;
  name: string;
  container: string;
  status: "running" | "stopped" | "degraded" | string;
  version: string;
  uptime_sec: number | null;
  latency_ms: number | null;
  detail: string;
  docker_state?: string | null;
  exit_code?: number | null;
  restart_count?: number | null;
  can_control?: boolean;
  critical?: boolean;
  sub_services?: BackgroundSubService[];
}

interface ServerSystemInfo {
  server: {
    hostname: string;
    os_name: string;
    kernel: string;
    architecture: string;
    python_version: string;
    cpu_cores: number;
    load_avg: number[] | null;
    memory: {
      total_bytes: number | null;
      used_bytes: number | null;
      available_bytes: number | null;
      usage_pct: number | null;
    };
    disk: {
      total_bytes: number | null;
      used_bytes: number | null;
      free_bytes: number | null;
      usage_pct: number | null;
    };
    db_size_bytes: number | null;
    api_uptime_sec: number;
    server_time_utc: string;
    docker_control_available?: boolean;
    git: {
      current_sha: string;
      current_version?: string;
      branch: string;
      last_commit_subject: string;
      last_commit_date: string | null;
      remote_url: string;
    };
    config: {
      web_port: number;
      syslog_port: number;
      snmp_poll_interval_sec: number;
      subnet_scan_minutes: number;
      snmp_raw_retention_days: number;
      log_retention_days: number;
      loaded_mib_files: number;
    };
  };
  services: BackgroundServiceInfo[];
}

interface GitCommitInfo {
  version?: string;
  sha: string;
  subject: string;
  summary_bullets?: string[];
  author: string;
  date: string;
  commit_date?: string;
}

interface SystemUpdateStatus {
  update_available: boolean;
  current_sha: string;
  remote_sha: string;
  current_version?: string;
  remote_version?: string;
  commits_behind: number;
  commits: GitCommitInfo[];
  updates?: GitCommitInfo[];
  summary_bullets?: string[];
  status: "idle" | "updating";
  checked_at: string;
}

interface SNMPProfile {
  id: number;
  name: string;
  version: string;
  port: number;
  community: string;
  v3_user: string;
  v3_auth_proto: string;
  v3_auth_key: string;
  v3_priv_proto: string;
  v3_priv_key: string;
  poll_interval: number;
}

interface SNMPTargetConfig {
  id: number;
  device_id?: number | null;
  device_ip: string;
  label: string;
  profile_id: number | null;
  profile_name?: string;
  profile_version?: string;
  enabled: boolean;
}

interface SubnetConfig {
  id: number;
  subnet_cidr: string;
  label: string;
  vlan_id: number | null;
  enabled: boolean;
}

interface UniFiControllerConfig {
  id: number;
  name: string;
  host: string;
  api_key: string;
  site_name: string;
  verify_ssl: boolean;
  poll_interval: number;
  enabled: boolean;
}

interface ISPSettings {
  isp_name: string;
  download_mbps: number;
  upload_mbps: number;
}

interface OverviewGraphSelection {
  device_ip: string;
  if_indices: number[];
}

interface OverviewGraphSettings {
  mode: "auto" | "custom";
  selections: OverviewGraphSelection[];
}

interface OverviewGraphSourceMeta {
  mode: "auto" | "custom";
  source: string;
  device_count: number;
  interface_count: number;
  has_all_interfaces_device: boolean;
  summary: string;
}

interface OverviewGraphOptionInterface {
  if_index: number;
  if_name: string;
  if_descr: string;
  if_alias: string;
  if_speed_bps: number;
  if_oper_status: number;
  in_bps: number;
  out_bps: number;
}

interface OverviewGraphOptionDevice {
  device_id?: number | null;
  device_ip: string;
  hostname: string;
  vendor: string | null;
  device_category: string | null;
  device_profile_type: string;
  is_online: boolean;
  snmp_enabled: boolean;
  snmp_reachable: boolean | null;
  subnet_cidr: string | null;
  interfaces: OverviewGraphOptionInterface[];
}

type OverviewRangeKey = "live" | "1h" | "12h" | "1d" | "7d" | "1m";

interface AllConfigPayload {
  onboarding: {
    completed: boolean;
    skipped: boolean;
  };
  isp_settings?: ISPSettings;
  overview_graph_settings?: OverviewGraphSettings;
  snmp_profiles: SNMPProfile[];
  snmp_targets: SNMPTargetConfig[];
  subnets: SubnetConfig[];
  unifi_controllers: UniFiControllerConfig[];
}

interface UnifiedDeviceItem {
  device_id?: number | null;
  ip_address: string;
  hostname: string;
  mac_address: string | null;
  vendor: string | null;
  device_category?: string | null;
  device_profile_type?: string;
  matched_mibs?: string[];
  subnet_cidr: string | null;
  vlan_id: number | null;
  is_online: boolean;
  snmp_enabled: boolean;
  snmp_reachable: boolean | null;
  snmp_profile_name: string | null;
  snmp_profile_id: number | null;
  snmp_target_id: number | null;
  sys_descr: string | null;
  sys_location: string | null;
  cpu_usage_pct: number | null;
  interface_count: number;
  uptime_sec: number | null;
  sources: string[];
  log_count_24h: number;
  last_seen: string | null;
}

interface UPSTelemetry {
  hardware_model?: string;
  firmware_version?: string;
  serial_number?: string;
  sku?: string;
  manufacture_date?: string;
  output_status?: string;
  output_status_code?: number;
  on_battery?: boolean;
  battery_status?: string;
  battery_replace_needed?: boolean;
  battery_charge_pct?: number | null;
  battery_temp_c?: number | null;
  runtime_remaining_sec?: number | null;
  time_on_battery_sec?: number;
  battery_voltage_v?: number | null;
  battery_nominal_voltage_v?: number | null;
  last_replace_date?: string;
  external_battery_packs?: number | null;
  bad_battery_packs?: number | null;
  input_voltage_v?: number | null;
  input_max_voltage_v?: number | null;
  input_min_voltage_v?: number | null;
  input_frequency_hz?: number | null;
  last_transfer_reason?: string;
  output_voltage_v?: number | null;
  output_frequency_hz?: number | null;
  output_load_pct?: number | null;
  output_current_a?: number | null;
  output_watts?: number | null;
  output_va?: number | null;
  energy_kwh?: number | null;
  self_test_result?: string;
  self_test_date?: string;
  calibration_result?: string;
  calibration_date?: string;
  alarms_present?: number;
}

interface UPSHistoryPoint {
  time: string;
  battery_charge_pct: number | null;
  output_load_pct: number | null;
  runtime_remaining_sec: number | null;
  input_voltage_v: number | null;
  output_voltage_v: number | null;
  output_watts: number | null;
  battery_temp_c: number | null;
}

interface PrinterSupply {
  name: string;
  level: number;
  max_capacity: number;
  pct: number | null;
}

interface SNMPTelemetrySeriesPoint {
  time: string;
  oids_collected: number;
  interface_samples: number;
  oid_walks: number;
  avg_latency_ms: number;
  max_latency_ms: number;
  polls_ok: number;
  polls_failed: number;
  devices_polled: number;
}

interface SNMPTelemetryTarget {
  device_id?: number | null;
  device_ip: string;
  sys_name: string;
  vendor_name: string;
  device_category: string;
  device_profile_type: string;
  enabled: boolean;
  is_reachable: boolean | null;
  snmp_version: string;
  profile_name: string;
  interface_count: number;
  responding_mib_count: number;
  responding_mibs: string[];
  last_poll_duration_ms: number;
  avg_latency_ms: number;
  oids_per_poll: number;
  walks_per_poll: number;
  success_rate_pct: number;
  failed_polls: number;
  last_polled: string | null;
}

interface SNMPTelemetryPayload {
  range: string;
  summary: {
    enabled_targets: number;
    reachable_targets: number;
    unreachable_targets: number;
    total_polls: number;
    successful_polls: number;
    failed_polls: number;
    success_rate_pct: number;
    total_walks: number;
    total_oids_ingested: number;
    total_interface_samples: number;
    oids_per_min: number;
    avg_latency_ms: number;
    p95_latency_ms: number;
    max_latency_ms: number;
    total_interfaces_tracked: number;
    raw_table_rows_24h: number;
    active_mibs_fleet: string[];
    version_breakdown: Record<string, number>;
  };
  series: SNMPTelemetrySeriesPoint[];
  targets: SNMPTelemetryTarget[];
}

interface DeviceDetailPayload {
  device_id?: number | null;
  ip_address: string;
  discovered_host: {
    ip_address: string;
    subnet_cidr: string;
    mac_address: string | null;
    hostname: string | null;
    vendor: string | null;
    vlan_id: number | null;
    is_online: boolean;
    discovery_source: string;
    switch_mac: string | null;
    switch_port: number | null;
    first_seen: string | null;
    last_seen: string | null;
  } | null;
  snmp_config: {
    target_id: number;
    device_ip: string;
    label: string;
    profile_id: number | null;
    enabled: boolean;
    created_at?: string | null;
    profile_name: string | null;
    profile_version: string | null;
    profile_port: number | null;
  } | null;
  snmp_system: {
    device_ip: string;
    sys_name: string | null;
    sys_descr: string | null;
    sys_object_id: string | null;
    sys_location: string | null;
    sys_contact: string | null;
    sys_uptime_ticks: number | null;
    cpu_usage_pct: number | null;
    mem_usage_pct: number | null;
    temp_celsius: number | null;
    interface_count: number;
    vendor_name: string | null;
    device_category: string | null;
    matched_mibs: string[] | null;
    hardware_model: string | null;
    serial_number: string | null;
    firmware_version: string | null;
    mac_address?: string | null;
    subnet_cidr?: string | null;
    first_polled?: string | null;
    extended_metrics: {
      cpu_details?: {
        core_count?: number;
        load_1m?: number;
        load_5m?: number;
        load_15m?: number;
        user_pct?: number;
        system_pct?: number;
        idle_pct?: number;
      };
      memory_details?: {
        total_bytes?: number;
        used_bytes?: number;
        avail_bytes?: number;
        cached_bytes?: number;
        swap_total_bytes?: number;
        swap_used_bytes?: number;
        usage_pct?: number;
      };
      storage_volumes?: Array<{
        mount: string;
        total_bytes: number;
        used_bytes: number;
        usage_pct: number;
      }>;
      sensors?: Array<{
        name: string;
        category: string;
        value: string;
        status: string;
      }>;
      lldp_neighbors?: Array<{
        local_port: string;
        neighbor_name: string;
        neighbor_port: string;
        neighbor_descr: string;
      }>;
      wireless_radios?: Array<{
        ssid: string;
        radio: string;
        channel: string;
        stations: number;
        tx_power: string;
      }>;
      responding_mibs?: string[];
      ip_addresses?: Array<{
        ip: string;
        netmask: string;
        cidr: string;
        if_index: number;
        if_name: string;
        mac_address: string;
      }>;
      ups_telemetry?: UPSTelemetry | null;
      printer_supplies?: PrinterSupply[];
      vendor_scalars?: Record<string, string | number>;
    } | null;
    is_reachable: boolean;
    last_poll_duration_ms: number;
    last_polled: string | null;
  } | null;
  device_profile_type?: "ups" | "nas" | "printer" | "network" | "server" | string;
  matched_mib_profile?: {
    vendor_name?: string;
    vendor?: string;
    category: string;
    mibs: string[];
  };
  resolved_identity?: {
    hostname: string;
    mac_address: string | null;
    vendor: string | null;
    subnet_cidr: string | null;
    vlan_id: number | null;
    discovery_source: string;
    first_seen: string | null;
    last_seen: string | null;
  };
  snmp_interfaces: Array<{
    if_index: number;
    if_name: string;
    if_descr: string;
    if_alias: string;
    if_speed_bps: number;
    if_oper_status: number;
    in_octets: number;
    out_octets: number;
    in_bps: number;
    out_bps: number;
    in_errors: number;
    out_errors: number;
    in_discards: number;
    out_discards: number;
    cpu_usage: number;
    sys_uptime: number;
    time: string;
  }>;
  unifi_device: {
    mac: string;
    ip_address: string;
    lan_ip?: string | null;
    name: string;
    model: string;
    device_type: string;
    serial_number?: string | null;
    firmware_version: string;
    status: string;
    uptime_sec: number;
    cpu_usage_pct: number;
    mem_usage_pct: number;
    temp_celsius: number | null;
    poe_total_watts: number;
    num_sta: number;
    last_seen: string;
  } | null;
  unifi_client: {
    mac: string;
    ip_address: string;
    hostname: string;
    oui_vendor: string;
    is_wired: boolean;
    network_name: string;
    vlan_id: number | null;
    sw_mac: string | null;
    sw_port: number | null;
    essid: string | null;
    channel: number | null;
    signal_dbm: number | null;
    rx_bps: number;
    tx_bps: number;
  } | null;
  recent_logs: SyslogEntry[];
}

interface GatewayMetrics {
  time: string;
  site_name: string;
  gateway_name: string;
  gateway_model: string;
  gateway_version: string;
  wan_ip: string;
  wan_isp: string;
  wan_status: string;
  wan_rx_bps: number;
  wan_tx_bps: number;
  wan_latency_ms: number;
  wan_packet_loss_pct: number;
  wan_uptime_sec: number;
  cpu_usage_pct: number;
  mem_usage_pct: number;
  temp_celsius: number | null;
  total_poe_watts: number;
  active_wan_clients: number;
  active_lan_clients: number;
  active_wlan_clients: number;
  total_devices: number;
  online_devices: number;
}

interface LiveTrafficSample {
  time: string;
  rx_bps: number;
  tx_bps: number;
  latency_ms?: number;
  source?: string;
  device_count?: number;
}

interface OverviewPayload {
  gateway: GatewayMetrics | null;
  wan_series: Array<{
    time: string;
    rx_bps: number;
    tx_bps: number;
    latency_ms: number;
  }>;
  series_range?: string;
  isp_settings?: ISPSettings;
  overview_graph_settings?: OverviewGraphSettings;
  overview_graph_source?: OverviewGraphSourceMeta;
  live_series?: LiveTrafficSample[];
  unifi_devices: Array<{
    device_id?: number | null;
    mac: string;
    ip_address: string;
    lan_ip?: string | null;
    name: string;
    model: string;
    device_type: string;
    firmware_version: string;
    status: string;
    uptime_sec: number;
    cpu_usage_pct: number;
    mem_usage_pct: number;
    temp_celsius: number | null;
    poe_total_watts: number;
    poe_max_watts: number;
    num_sta: number;
    rx_bps: number;
    tx_bps: number;
  }>;
  snmp_devices: Array<{
    device_id?: number | null;
    device_ip: string;
    sys_name: string;
    sys_descr: string;
    interface_count: number;
    cpu_usage_pct: number;
    is_reachable: boolean;
    last_poll_duration_ms: number;
    last_polled: string;
  }>;
  scanner_summary: {
    online_hosts: number;
    total_discovered: number;
  };
  syslog_summary: {
    total_24h: number;
    errors_24h: number;
    warnings_24h: number;
  };
}

interface SNMPInterface {
  device_id?: number | null;
  device_ip: string;
  hostname: string;
  if_index: number;
  if_name: string;
  if_descr: string;
  if_alias: string;
  if_speed_bps: number;
  if_oper_status: number;
  in_bps: number;
  out_bps: number;
  in_errors: number;
  out_errors: number;
  cpu_usage: number;
  time: string;
}

interface SNMPTrendPoint {
  time: string;
  avg_in_bps: number;
  max_in_bps: number;
  p95_in_bps: number;
  avg_out_bps: number;
  max_out_bps: number;
  p95_out_bps: number;
  avg_cpu_usage: number;
}

interface DiscoveredHost {
  device_id?: number | null;
  ip_address: string;
  subnet_cidr: string;
  mac_address: string | null;
  hostname: string | null;
  vendor: string | null;
  vlan_id: number | null;
  is_online: boolean;
  discovery_source: string;
  switch_port: number | null;
  last_seen: string;
}

interface SyslogEntry {
  time: string;
  device_id?: number | null;
  source_ip: string;
  hostname: string;
  facility: string;
  severity: string;
  severity_num: number;
  app_name: string;
  proc_id: string;
  message: string;
}

function formatBps(bps: number | undefined | null): string {
  const val = Number(bps || 0);
  if (val >= 1_000_000_000) return `${(val / 1_000_000_000).toFixed(2)} Gbps`;
  if (val >= 1_000_000) return `${(val / 1_000_000).toFixed(2)} Mbps`;
  if (val >= 1_000) return `${(val / 1_000).toFixed(1)} Kbps`;
  return `${val.toFixed(0)} bps`;
}

function formatAxisMbps(bps: number | undefined | null): string {
  const mbps = Number(bps || 0) / 1_000_000;
  if (mbps <= 0) return "0";
  if (mbps >= 100) return Math.round(mbps).toString();
  if (mbps >= 1) return parseFloat(mbps.toFixed(1)).toString();
  return parseFloat(mbps.toFixed(2)).toString();
}

function formatBytes(bytes: number | undefined | null): string {
  const val = Number(bytes || 0);
  if (val >= 1_099_511_627_776) return `${(val / 1_099_511_627_776).toFixed(2)} TB`;
  if (val >= 1_073_741_824) return `${(val / 1_073_741_824).toFixed(2)} GB`;
  if (val >= 1_048_576) return `${(val / 1_048_576).toFixed(1)} MB`;
  if (val >= 1024) return `${(val / 1024).toFixed(1)} KB`;
  return `${val} B`;
}

function formatUptime(sec: number | undefined | null): string {
  const s = Number(sec || 0);
  if (s <= 0) return "—";
  if (s < 60) return `${Math.floor(s)}s`;
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatDurationCompact(sec: number | undefined | null): string {
  if (sec == null || Number.isNaN(Number(sec))) return "—";
  const s = Math.floor(Number(sec));
  if (s <= 0) return "0s";
  if (s < 60) return `${s}s`;
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function ipv4ToSortNumber(ip: string): number {
  const parts = (ip || "").split(".");
  if (parts.length !== 4) return Number.MAX_SAFE_INTEGER;
  let num = 0;
  for (const p of parts) {
    const octet = Number(p);
    if (!Number.isInteger(octet) || octet < 0 || octet > 255) return Number.MAX_SAFE_INTEGER;
    num = num * 256 + octet;
  }
  return num;
}

function formatChartAxisTimestamp(rawLabel: string, spanMs: number): string {
  if (!rawLabel) return "";
  const dt = new Date(rawLabel);
  if (Number.isNaN(dt.getTime())) return rawLabel;
  // Short live-streaming windows (<= 10 minutes): include seconds
  if (spanMs > 0 && spanMs <= 10 * 60 * 1000) {
    return dt.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }
  // Multi-day spans (>36 hours): include Month & Day
  if (spanMs > 36 * 3600 * 1000) {
    if (spanMs > 14 * 86400 * 1000) {
      return dt.toLocaleDateString([], { month: "short", day: "numeric" });
    }
    return dt.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatChartTooltipTimestamp(tsMs: number, fallbackLabel: string, spanMs: number): string {
  if (Number.isNaN(tsMs)) return fallbackLabel || "—";
  const dt = new Date(tsMs);
  if (spanMs > 36 * 3600 * 1000) {
    return dt.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  if (spanMs <= 2 * 3600 * 1000) {
    return dt.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }
  return dt.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function rangeKeyToWindowMs(rk: string): number | null {
  switch ((rk || "").trim().toLowerCase()) {
    case "1h":
      return 3_600_000;
    case "6h":
      return 6 * 3_600_000;
    case "12h":
      return 12 * 3_600_000;
    case "24h":
    case "1d":
      return 24 * 3_600_000;
    case "7d":
      return 7 * 86_400_000;
    case "30d":
    case "1m":
      return 30 * 86_400_000;
    case "1y":
      return 365 * 86_400_000;
    default:
      return null;
  }
}

interface SplineSegment {
  x0: number;
  y0: number;
  cp1x: number;
  cp1y: number;
  cp2x: number;
  cp2y: number;
  x1: number;
  y1: number;
}

interface SplineResult {
  linePath: string;
  areaPath: string;
  segments: SplineSegment[];
}

function buildMonotoneSpline(
  pts: Array<{ x: number; y: number }>,
  topY: number,
  baselineY: number,
): SplineResult {
  const n = pts.length;
  if (n === 0) return { linePath: "", areaPath: "", segments: [] };
  if (n === 1) {
    return {
      linePath: `M ${pts[0].x.toFixed(2)},${pts[0].y.toFixed(2)}`,
      areaPath: "",
      segments: [],
    };
  }

  const dx: number[] = [];
  const d: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    const deltaX = pts[i + 1].x - pts[i].x;
    dx.push(deltaX);
    d.push(deltaX !== 0 ? (pts[i + 1].y - pts[i].y) / deltaX : 0);
  }

  const m: number[] = new Array(n).fill(0);
  m[0] = d[0];
  m[n - 1] = d[n - 2];
  for (let i = 1; i < n - 1; i++) {
    if (d[i - 1] * d[i] <= 0) {
      m[i] = 0;
    } else {
      const w1 = 2 * dx[i] + dx[i - 1];
      const w2 = dx[i] + 2 * dx[i - 1];
      m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i]);
    }
  }

  for (let i = 0; i < n - 1; i++) {
    if (Math.abs(d[i]) < 1e-9) {
      m[i] = 0;
      m[i + 1] = 0;
    } else {
      const a = m[i] / d[i];
      const b = m[i + 1] / d[i];
      const h = Math.hypot(a, b);
      if (h > 3) {
        const tau = 3 / h;
        m[i] = tau * a * d[i];
        m[i + 1] = tau * b * d[i];
      }
    }
  }

  const clampY = (val: number) => Math.min(baselineY, Math.max(topY, val));
  const segments: SplineSegment[] = [];
  let linePath = `M ${pts[0].x.toFixed(2)},${pts[0].y.toFixed(2)}`;

  for (let i = 0; i < n - 1; i++) {
    const hdx = (pts[i + 1].x - pts[i].x) / 3;
    const seg: SplineSegment = {
      x0: pts[i].x,
      y0: pts[i].y,
      cp1x: pts[i].x + hdx,
      cp1y: clampY(pts[i].y + m[i] * hdx),
      cp2x: pts[i + 1].x - hdx,
      cp2y: clampY(pts[i + 1].y - m[i + 1] * hdx),
      x1: pts[i + 1].x,
      y1: pts[i + 1].y,
    };
    segments.push(seg);
    linePath += ` C ${seg.cp1x.toFixed(2)},${seg.cp1y.toFixed(2)} ${seg.cp2x.toFixed(2)},${seg.cp2y.toFixed(2)} ${seg.x1.toFixed(2)},${seg.y1.toFixed(2)}`;
  }

  const areaPath = `${linePath} L ${pts[n - 1].x.toFixed(2)},${baselineY.toFixed(2)} L ${pts[0].x.toFixed(2)},${baselineY.toFixed(2)} Z`;
  return { linePath, areaPath, segments };
}

function evalSplineY(
  segments: SplineSegment[],
  pts: Array<{ x: number; y: number }>,
  ratio: number,
): number {
  if (pts.length === 0) return 0;
  if (pts.length === 1 || segments.length === 0) return pts[0].y;
  const exactIdx = Math.min(pts.length - 1, Math.max(0, ratio * (pts.length - 1)));
  const segIdx = Math.min(segments.length - 1, Math.max(0, Math.floor(exactIdx)));
  const t = Math.min(1, Math.max(0, exactIdx - segIdx));
  const mt = 1 - t;
  const s = segments[segIdx];
  return (
    mt * mt * mt * s.y0 +
    3 * mt * mt * t * s.cp1y +
    3 * mt * t * t * s.cp2y +
    t * t * t * s.y1
  );
}

function evalSplineAtX(
  segments: SplineSegment[],
  pts: Array<{ x: number; y: number }>,
  targetX: number,
): number {
  if (pts.length === 0) return 0;
  if (pts.length === 1 || segments.length === 0) return pts[0].y;
  if (targetX <= pts[0].x) return pts[0].y;
  if (targetX >= pts[pts.length - 1].x) return pts[pts.length - 1].y;

  let lo = 0;
  let hi = segments.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (segments[mid].x1 < targetX) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  const s = segments[lo];
  const dx = s.x1 - s.x0;
  const t = dx > 1e-6 ? Math.min(1, Math.max(0, (targetX - s.x0) / dx)) : 0;
  const mt = 1 - t;
  return (
    mt * mt * mt * s.y0 +
    3 * mt * mt * t * s.cp1y +
    3 * mt * t * t * s.cp2y +
    t * t * t * s.y1
  );
}

interface ParsedSparkSample {
  ts: number;
  inVal: number;
  outVal: number;
  p95In: number;
  label: string;
}

const DualAreaSparkChart: React.FC<{
  points: Array<{ label: string; inVal: number; outVal: number; p95In?: number }>;
  height?: number;
  liveMode?: boolean;
  liveWindowMs?: number;
  windowMs?: number | null;
  showYAxis?: boolean;
  fixedMaxBps?: number | null;
  primaryLabel?: string;
  secondaryLabel?: string;
  primaryShortLabel?: string;
  secondaryShortLabel?: string;
  primaryColor?: string;
  secondaryColor?: string;
  yAxisLabel?: string;
  formatValue?: (val: number) => string;
  formatAxisTick?: (val: number) => string;
  emptyMessage?: string;
  hideSecondary?: boolean;
}> = ({
  points,
  height = 210,
  liveMode = false,
  liveWindowMs = 120_000,
  windowMs = null,
  showYAxis = false,
  fixedMaxBps = null,
  primaryLabel = "Download (RX)",
  secondaryLabel = "Upload (TX)",
  primaryShortLabel = "Download",
  secondaryShortLabel = "Upload",
  primaryColor = "#10B981",
  secondaryColor = "#38BDF8",
  yAxisLabel = "Mbps",
  formatValue,
  formatAxisTick,
  emptyMessage = "Waiting for time-series samples...",
  hideSecondary = false,
}) => {
  const isDefaultBps = !formatValue;
  const formatVal = formatValue ?? formatBps;
  const formatTick = formatAxisTick ?? formatAxisMbps;
  const hideSecondaryRef = useRef<boolean>(hideSecondary);
  hideSecondaryRef.current = hideSecondary;
  const rawId = useId();
  const gradPrefix = rawId.replace(/[^a-zA-Z0-9_-]/g, "");
  const rxGradId = `rxGrad-${gradPrefix}`;
  const txGradId = `txGrad-${gradPrefix}`;
  const clipId = `plotClip-${gradPrefix}`;
  const plotRef = useRef<HTMLDivElement | null>(null);
  const enteredAtRef = useRef<number>(0);
  const pointerRatioRef = useRef<number>(0.5);
  const samplesRef = useRef<ParsedSparkSample[]>([]);
  const lastLatestTsRef = useRef<number>(0);
  const clockOffsetRef = useRef<number>(0);
  const sampleGapRef = useRef<number>(2000);
  const displayEndRef = useRef<number>(0);
  const displayMaxRef = useRef<number>(1000);
  const fixedMaxRef = useRef<number | null>(fixedMaxBps ?? null);
  fixedMaxRef.current = fixedMaxBps ?? null;
  const [isHovering, setIsHovering] = useState(false);
  const [animateMove, setAnimateMove] = useState(false);
  const [, setAnimFrame] = useState(0);
  const [, setWindowClockTick] = useState(0);
  const [hoverData, setHoverData] = useState<{
    ratio: number;
    inYFrac: number;
    outYFrac: number;
    inVal: number;
    outVal: number;
    timeText: string;
    boxX: number;
    boxY: number;
  } | null>(null);

  useEffect(() => {
    const parsed: ParsedSparkSample[] = [];
    for (const pt of points) {
      const ts = new Date(pt.label).getTime();
      if (Number.isNaN(ts)) continue;
      parsed.push({
        ts,
        inVal: Math.max(0, pt.inVal || 0),
        outVal: Math.max(0, pt.outVal || 0),
        p95In: Math.max(0, pt.p95In || 0),
        label: pt.label,
      });
    }
    parsed.sort((a, b) => a.ts - b.ts);
    samplesRef.current = parsed;

    if (!liveMode || parsed.length === 0) return;

    const latestTs = parsed[parsed.length - 1].ts;
    const prevLatestTs = lastLatestTsRef.current;
    if (latestTs > prevLatestTs || prevLatestTs === 0) {
      const measuredOffset = latestTs - Date.now();
      clockOffsetRef.current =
        prevLatestTs === 0
          ? measuredOffset
          : clockOffsetRef.current * 0.8 + measuredOffset * 0.2;

      if (prevLatestTs > 0) {
        const delta = latestTs - prevLatestTs;
        if (delta >= 500 && delta <= 15000) {
          sampleGapRef.current = sampleGapRef.current * 0.7 + delta * 0.3;
        }
      } else if (parsed.length >= 2) {
        const recentGap = parsed[parsed.length - 1].ts - parsed[parsed.length - 2].ts;
        if (recentGap >= 500 && recentGap <= 15000) {
          sampleGapRef.current = recentGap;
        }
      }
      lastLatestTsRef.current = latestTs;
    }

    if (displayEndRef.current === 0 || Math.abs(latestTs - displayEndRef.current) > 30000) {
      const lagMs = Math.min(8000, Math.max(1500, sampleGapRef.current * 1.15));
      displayEndRef.current = Math.max(parsed[0].ts, latestTs - lagMs);
    }
  }, [points, liveMode]);

  useEffect(() => {
    if (!liveMode) {
      lastLatestTsRef.current = 0;
      clockOffsetRef.current = 0;
      displayEndRef.current = 0;
      displayMaxRef.current = 1000;
      return;
    }

    const reducedMotion =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const minFrameGapMs = reducedMotion ? 1000 : 0;

    let rafId = 0;
    let lastFrameWallMs = performance.now();
    let lastDrawWallMs = 0;

    const step = (nowWallMs: number) => {
      rafId = window.requestAnimationFrame(step);
      if (typeof document !== "undefined" && document.hidden) {
        lastFrameWallMs = nowWallMs;
        return;
      }

      const dtMs = Math.max(0, nowWallMs - lastFrameWallMs);
      lastFrameWallMs = nowWallMs;

      if (minFrameGapMs > 0 && nowWallMs - lastDrawWallMs < minFrameGapMs) {
        return;
      }

      const samples = samplesRef.current;
      if (samples.length < 2) return;

      const earliestTs = samples[0].ts;
      const latestTs = samples[samples.length - 1].ts;
      const estServerNow = Date.now() + clockOffsetRef.current;
      const lagMs = Math.min(8000, Math.max(1500, sampleGapRef.current * 1.15));
      const targetEnd = Math.min(latestTs, Math.max(earliestTs, estServerNow - lagMs));

      if (
        displayEndRef.current === 0 ||
        dtMs > 2500 ||
        Math.abs(targetEnd - displayEndRef.current) > 12000
      ) {
        displayEndRef.current = targetEnd;
      } else if (targetEnd > displayEndRef.current) {
        const lagDiff = targetEnd - displayEndRef.current;
        const speed = 1 + Math.min(2.0, lagDiff / 1500);
        displayEndRef.current = Math.min(targetEnd, displayEndRef.current + dtMs * speed);
      }

      const lockedMax = fixedMaxRef.current;
      if (lockedMax && lockedMax > 0) {
        displayMaxRef.current = lockedMax;
      } else {
        const winEnd = displayEndRef.current;
        const winStart = winEnd - liveWindowMs;
        let visMax = 0;
        for (let i = 0; i < samples.length; i++) {
          const s = samples[i];
          if (s.ts < winStart - sampleGapRef.current) continue;
          if (s.ts > winEnd + sampleGapRef.current) break;
          const m = hideSecondaryRef.current ? s.inVal : Math.max(s.inVal, s.outVal, s.p95In);
          if (m > visMax) visMax = m;
        }
        const targetMax = Math.max(1000, visMax * 1.15);
        const alpha = Math.min(1, 1 - Math.exp(-dtMs / 420));
        if (displayMaxRef.current <= 1000 && targetMax > 1000) {
          displayMaxRef.current = targetMax;
        } else {
          displayMaxRef.current += (targetMax - displayMaxRef.current) * alpha;
        }
      }

      lastDrawWallMs = nowWallMs;
      setAnimFrame((n) => (n + 1) & 0x3fffffff);
    };

    rafId = window.requestAnimationFrame(step);
    return () => {
      window.cancelAnimationFrame(rafId);
    };
  }, [liveMode, liveWindowMs]);

  useEffect(() => {
    if (liveMode || !(windowMs && windowMs > 0)) return;
    const id = window.setInterval(() => {
      setWindowClockTick((n) => (n + 1) & 0x3fffffff);
    }, 30000);
    return () => window.clearInterval(id);
  }, [liveMode, windowMs]);

  const hasFixedWindow = Boolean(windowMs && windowMs > 0);
  if (!points.length && !hasFixedWindow) {
    return (
      <div
        style={{ height }}
        className="flex items-center justify-center rounded-lg border border-slate-800/80 bg-[#0B101B] text-xs text-slate-500"
      >
        {emptyMessage}
      </div>
    );
  }

  const width = 800;
  const topPad = 12;
  const baselineY = height - 18;
  const plotSpanY = Math.max(1, baselineY - topPad);

  const parsedSortedSamples: ParsedSparkSample[] =
    liveMode || hasFixedWindow
      ? points
          .map((pt) => ({
            ts: new Date(pt.label).getTime(),
            inVal: Math.max(0, pt.inVal || 0),
            outVal: Math.max(0, pt.outVal || 0),
            p95In: Math.max(0, pt.p95In || 0),
            label: pt.label,
          }))
          .filter((s) => !Number.isNaN(s.ts))
          .sort((a, b) => a.ts - b.ts)
      : [];
  const liveSamples = liveMode ? parsedSortedSamples : [];
  const useLiveRender = liveMode && liveSamples.length >= 2;
  const useFixedWindow = !useLiveRender && hasFixedWindow;

  let observedPeakBps = 0;
  let maxVal = 1000;
  let spanMs = 0;
  let inPts: Array<{ x: number; y: number }> = [];
  let outPts: Array<{ x: number; y: number }> = [];
  let tickXPositions: number[] = [];
  let axisLabels: string[] = [];
  let liveWindowStart = 0;
  let fixedWindowStart = 0;
  let fixedWindowSpanMs = 0;
  let hasVisibleSamples = points.length > 0;

  if (useLiveRender) {
    const latestTs = liveSamples[liveSamples.length - 1].ts;
    const fallbackLag = Math.min(8000, Math.max(1500, sampleGapRef.current * 1.15));
    const windowEnd =
      displayEndRef.current > 0
        ? Math.min(latestTs, displayEndRef.current)
        : Math.max(liveSamples[0].ts, latestTs - fallbackLag);
    liveWindowStart = windowEnd - liveWindowMs;
    spanMs = liveWindowMs;

    let startIdx = 0;
    for (let i = 0; i < liveSamples.length; i++) {
      if (liveSamples[i].ts < liveWindowStart) {
        startIdx = i;
      } else {
        break;
      }
    }
    const visibleSlice = liveSamples.slice(startIdx);
    hasVisibleSamples = visibleSlice.length > 0;

    let peakNow = 0;
    for (const s of visibleSlice) {
      const m = hideSecondary ? s.inVal : Math.max(s.inVal, s.outVal, s.p95In);
      if (m > peakNow) peakNow = m;
    }
    observedPeakBps = peakNow;
    maxVal =
      fixedMaxBps && fixedMaxBps > 0
        ? fixedMaxBps
        : displayMaxRef.current > 1000
          ? displayMaxRef.current
          : Math.max(1000, peakNow * 1.15);

    inPts = visibleSlice.map((s) => ({
      x: ((s.ts - liveWindowStart) / liveWindowMs) * width,
      y: baselineY - (Math.min(Math.max(0, s.inVal), maxVal) / maxVal) * plotSpanY,
    }));
    outPts = visibleSlice.map((s) => ({
      x: ((s.ts - liveWindowStart) / liveWindowMs) * width,
      y: baselineY - (Math.min(Math.max(0, s.outVal), maxVal) / maxVal) * plotSpanY,
    }));

    const liveTicks = 6;
    for (let t = 0; t < liveTicks; t++) {
      const frac = t / (liveTicks - 1);
      tickXPositions.push(frac * width);
      const tickTs = liveWindowStart + frac * liveWindowMs;
      axisLabels.push(
        new Date(tickTs).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }),
      );
    }
  } else if (useFixedWindow) {
    fixedWindowSpanMs = windowMs || 0;
    const nowMs = Date.now();
    const latestPtTs =
      parsedSortedSamples.length > 0
        ? parsedSortedSamples[parsedSortedSamples.length - 1].ts
        : nowMs;
    const fixedWindowEnd = Math.max(nowMs, latestPtTs);
    fixedWindowStart = fixedWindowEnd - fixedWindowSpanMs;
    spanMs = fixedWindowSpanMs;

    let firstInRangeIdx = parsedSortedSamples.findIndex((s) => s.ts >= fixedWindowStart);
    let windowedSlice: ParsedSparkSample[] = [];
    if (firstInRangeIdx !== -1) {
      const startSliceIdx = firstInRangeIdx > 0 ? firstInRangeIdx - 1 : firstInRangeIdx;
      windowedSlice = parsedSortedSamples.slice(startSliceIdx);
    }
    hasVisibleSamples = windowedSlice.length > 0;

    let peakNow = 0;
    for (const s of windowedSlice) {
      const m = hideSecondary ? s.inVal : Math.max(s.inVal, s.outVal, s.p95In);
      if (m > peakNow) peakNow = m;
    }
    observedPeakBps = peakNow;
    maxVal =
      fixedMaxBps && fixedMaxBps > 0
        ? fixedMaxBps
        : isDefaultBps
          ? Math.max(1000, observedPeakBps * 1.15)
          : Math.max(1, observedPeakBps * 1.1);

    inPts = windowedSlice.map((s) => ({
      x: ((s.ts - fixedWindowStart) / fixedWindowSpanMs) * width,
      y: baselineY - (Math.min(Math.max(0, s.inVal), maxVal) / maxVal) * plotSpanY,
    }));
    outPts = windowedSlice.map((s) => ({
      x: ((s.ts - fixedWindowStart) / fixedWindowSpanMs) * width,
      y: baselineY - (Math.min(Math.max(0, s.outVal), maxVal) / maxVal) * plotSpanY,
    }));

    const isApproxDaySpan =
      fixedWindowSpanMs >= 18 * 3_600_000 && fixedWindowSpanMs <= 36 * 3_600_000;
    const startDateStr = new Date(fixedWindowStart).toDateString();
    const endDateStr = new Date(fixedWindowEnd).toDateString();
    const fixedTicks = 6;
    for (let t = 0; t < fixedTicks; t++) {
      const frac = t / (fixedTicks - 1);
      tickXPositions.push(frac * width);
      const tickTs = fixedWindowStart + frac * fixedWindowSpanMs;
      if (isApproxDaySpan) {
        const dt = new Date(tickTs);
        if (t === 0 && startDateStr !== endDateStr) {
          axisLabels.push(
            dt.toLocaleString([], {
              month: "short",
              day: "numeric",
              hour: "2-digit",
              minute: "2-digit",
            }),
          );
        } else {
          axisLabels.push(dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
        }
      } else {
        axisLabels.push(
          formatChartAxisTimestamp(new Date(tickTs).toISOString(), fixedWindowSpanMs),
        );
      }
    }
  } else {
    observedPeakBps = Math.max(
      0,
      ...points.map((p) => (hideSecondary ? p.inVal : Math.max(p.inVal, p.outVal, p.p95In || 0))),
    );
    maxVal =
      fixedMaxBps && fixedMaxBps > 0
        ? fixedMaxBps
        : isDefaultBps
          ? Math.max(1000, observedPeakBps * 1.15)
          : Math.max(1, observedPeakBps * 1.1);
    const firstTs = new Date(points[0].label).getTime();
    const lastTs = new Date(points[points.length - 1].label).getTime();
    spanMs =
      !Number.isNaN(firstTs) && !Number.isNaN(lastTs) ? Math.max(0, lastTs - firstTs) : 0;

    const tickCount = Math.min(6, points.length);
    const tickIndices: number[] = [];
    if (tickCount === 1) {
      tickIndices.push(0);
    } else {
      for (let t = 0; t < tickCount; t++) {
        const idx = Math.round((t / (tickCount - 1)) * (points.length - 1));
        if (!tickIndices.includes(idx)) tickIndices.push(idx);
      }
    }
    tickXPositions = tickIndices.map((idx) =>
      points.length === 1 ? width / 2 : (idx / (points.length - 1)) * width,
    );
    axisLabels = tickIndices.map((idx) => formatChartAxisTimestamp(points[idx].label, spanMs));

    const toPts = (vals: number[]) =>
      vals.map((v, i) => ({
        x: points.length === 1 ? width / 2 : (i / (points.length - 1)) * width,
        y: baselineY - (Math.min(Math.max(0, v), maxVal) / maxVal) * plotSpanY,
      }));
    inPts = toPts(points.map((p) => p.inVal));
    outPts = toPts(points.map((p) => p.outVal));
  }

  const inSpline = buildMonotoneSpline(inPts, topPad, baselineY);
  const outSpline = buildMonotoneSpline(outPts, topPad, baselineY);
  const minCurveX = inPts.length > 0 ? Math.max(0, inPts[0].x) : 0;
  const maxCurveX = inPts.length > 0 ? Math.min(width, inPts[inPts.length - 1].x) : width;
  const showEndpointMarkers =
    inPts.length === 1 || (useFixedWindow && inPts.length > 0 && maxCurveX - minCurveX < 6);

  const buildHoverSnapshot = (ratioInput: number) => {
    const rect = plotRef.current?.getBoundingClientRect();
    const rectW = rect && rect.width > 0 ? rect.width : width;
    const rectH = rect && rect.height > 0 ? rect.height : height;

    let ratio = Math.min(1, Math.max(0, ratioInput));
    let inY: number;
    let outY: number;
    let timeText = "—";

    if (useLiveRender || useFixedWindow) {
      const winStart = useLiveRender ? liveWindowStart : fixedWindowStart;
      const winSpan = useLiveRender ? liveWindowMs : fixedWindowSpanMs;
      const rawX = ratio * width;
      const targetX =
        maxCurveX >= minCurveX
          ? Math.min(maxCurveX, Math.max(minCurveX, rawX))
          : maxCurveX;
      ratio = targetX / width;
      inY = evalSplineAtX(inSpline.segments, inPts, targetX);
      outY = evalSplineAtX(outSpline.segments, outPts, targetX);
      const tsMs = winStart + ratio * winSpan;
      timeText = formatChartTooltipTimestamp(tsMs, "", winSpan);
    } else {
      ratio = points.length === 1 ? 0.5 : ratio;
      inY = evalSplineY(inSpline.segments, inPts, ratio);
      outY = evalSplineY(outSpline.segments, outPts, ratio);

      if (points.length === 1) {
        timeText = formatChartTooltipTimestamp(
          new Date(points[0].label).getTime(),
          points[0].label,
          spanMs,
        );
      } else {
        const exactIdx = ratio * (points.length - 1);
        const i0 = Math.min(points.length - 2, Math.max(0, Math.floor(exactIdx)));
        const t = exactIdx - i0;
        const ts0 = new Date(points[i0].label).getTime();
        const ts1 = new Date(points[i0 + 1].label).getTime();
        if (!Number.isNaN(ts0) && !Number.isNaN(ts1)) {
          timeText = formatChartTooltipTimestamp(
            ts0 + t * (ts1 - ts0),
            points[i0].label,
            spanMs,
          );
        } else {
          const nearest = points[Math.min(points.length - 1, Math.max(0, Math.round(exactIdx)))];
          timeText = formatChartTooltipTimestamp(
            new Date(nearest.label).getTime(),
            nearest.label,
            spanMs,
          );
        }
      }
    }

    const inVal = Math.max(0, ((baselineY - inY) / plotSpanY) * maxVal);
    const outVal = Math.max(0, ((baselineY - outY) / plotSpanY) * maxVal);

    const tooltipW = 204;
    const tooltipH = 82;
    const cursorX = ratio * rectW;
    const rightX = cursorX + 14;
    const leftX = cursorX - tooltipW - 14;
    const rawBoxX = rightX + tooltipW <= rectW - 4 ? rightX : leftX;
    const boxX = Math.min(Math.max(4, rawBoxX), Math.max(4, rectW - tooltipW - 4));
    const topDotY = ((hideSecondary ? inY : Math.min(inY, outY)) / height) * rectH;
    const boxY = Math.min(Math.max(4, topDotY - 20), Math.max(4, rectH - tooltipH - 4));

    return {
      ratio,
      inYFrac: inY / height,
      outYFrac: outY / height,
      inVal,
      outVal,
      timeText,
      boxX,
      boxY,
    };
  };

  const updateHoverFromEvent = (e: React.PointerEvent<HTMLDivElement>, isEntry: boolean) => {
    if (inPts.length === 0) return;
    const rect = plotRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return;

    const px = Math.min(rect.width, Math.max(0, e.clientX - rect.left));
    const ratio =
      !useLiveRender && !useFixedWindow && points.length === 1 ? 0.5 : px / rect.width;
    pointerRatioRef.current = ratio;

    if (isEntry) {
      enteredAtRef.current = performance.now();
      setAnimateMove(false);
      setIsHovering(true);
    } else {
      if (!isHovering) setIsHovering(true);
      if (performance.now() - enteredAtRef.current > 24) {
        setAnimateMove(true);
      }
    }

    setHoverData(buildHoverSnapshot(ratio));
  };

  const handlePointerLeave = () => {
    setIsHovering(false);
  };

  const displayedHover =
    inPts.length === 0
      ? null
      : useLiveRender && isHovering
        ? buildHoverSnapshot(pointerRatioRef.current)
        : hoverData;
  const smoothPosTransition = !useLiveRender && animateMove;

  const useDynamicBpsAxis =
    isDefaultBps && !formatAxisTick && !(fixedMaxBps && fixedMaxBps > 0);
  const dynamicBpsScale = useDynamicBpsAxis
    ? maxVal >= 1_000_000_000
      ? { unit: "Gbps", divisor: 1_000_000_000 }
      : maxVal >= 1_000_000
        ? { unit: "Mbps", divisor: 1_000_000 }
        : maxVal >= 1_000
          ? { unit: "Kbps", divisor: 1_000 }
          : { unit: "bps", divisor: 1 }
    : null;
  const effectiveYAxisLabel = dynamicBpsScale ? dynamicBpsScale.unit : yAxisLabel;
  const formatDynamicBpsTick = (val: number): string => {
    if (!dynamicBpsScale) return formatTick(val);
    const scaled = val / dynamicBpsScale.divisor;
    if (scaled <= 0) return "0";
    if (scaled >= 100) return Math.round(scaled).toString();
    if (scaled >= 10) return parseFloat(scaled.toFixed(1)).toString();
    return parseFloat(scaled.toFixed(2)).toString();
  };

  return (
    <div className="relative w-full overflow-visible rounded-lg border border-slate-800/80 bg-[#0B101B] p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400">
        <div className="flex items-center gap-4">
          {showYAxis && (
            <span className="w-14 text-right font-mono-tabular text-[11px] font-medium uppercase tracking-wider text-slate-500">
              {effectiveYAxisLabel}
            </span>
          )}
          <span className="flex items-center gap-1.5">
            <span
              className="h-2 w-2 rounded-full"
              style={{ backgroundColor: primaryColor }}
            />
            {primaryLabel}
          </span>
          {!hideSecondary && (
            <span className="flex items-center gap-1.5">
              <span
                className="h-2 w-2 rounded-full"
                style={{ backgroundColor: secondaryColor }}
              />
              {secondaryLabel}
            </span>
          )}
        </div>
        <span className="font-mono-tabular text-slate-300">
          Peak: {hasVisibleSamples ? formatVal(observedPeakBps) : "—"}
          {fixedMaxBps && fixedMaxBps > 0
            ? isDefaultBps
              ? ` / ${formatAxisMbps(fixedMaxBps)} Mbps`
              : ` / ${formatVal(fixedMaxBps)}`
            : ""}
        </span>
      </div>

      <div className={showYAxis ? "flex items-stretch gap-2" : ""}>
        {showYAxis && (
          <div className="flex w-14 shrink-0 flex-col">
            <div className="relative h-44 w-full select-none pr-1.5 text-right font-mono-tabular text-[11px] text-slate-400">
              {[0, 0.25, 0.5, 0.75, 1].map((r) => (
                <span
                  key={r}
                  style={{ top: `${((topPad + plotSpanY * r) / height) * 100}%` }}
                  className="absolute right-1.5 -translate-y-1/2 leading-none"
                >
                  {formatDynamicBpsTick(r === 1 ? 0 : maxVal * (1 - r))}
                </span>
              ))}
            </div>
            <div className="mt-1 pt-1.5" aria-hidden="true" />
          </div>
        )}

        <div className={showYAxis ? "min-w-0 flex-1" : "w-full"}>
          <div
            ref={plotRef}
            onPointerEnter={(e) => updateHoverFromEvent(e, true)}
            onPointerMove={(e) => updateHoverFromEvent(e, false)}
            onPointerLeave={handlePointerLeave}
            className="relative h-44 w-full cursor-crosshair select-none touch-none"
          >
            <svg
              viewBox={`0 0 ${width} ${height}`}
              preserveAspectRatio="none"
              className="block h-full w-full overflow-visible"
            >
              <defs>
                <linearGradient id={rxGradId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={primaryColor} stopOpacity="0.32" />
                  <stop offset="100%" stopColor={primaryColor} stopOpacity="0.0" />
                </linearGradient>
                <linearGradient id={txGradId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={secondaryColor} stopOpacity="0.26" />
                  <stop offset="100%" stopColor={secondaryColor} stopOpacity="0.0" />
                </linearGradient>
                <clipPath id={clipId}>
                  <rect x={0} y={0} width={width} height={height} />
                </clipPath>
              </defs>
              {(showYAxis ? [0, 0.25, 0.5, 0.75] : [0.25, 0.5, 0.75]).map((r) => (
                <line
                  key={r}
                  x1={0}
                  y1={topPad + plotSpanY * r}
                  x2={width}
                  y2={topPad + plotSpanY * r}
                  stroke="#1E293B"
                  strokeDasharray="4 4"
                  strokeWidth="1"
                />
              ))}
              <line
                x1={0}
                y1={baselineY}
                x2={width}
                y2={baselineY}
                stroke="#1E293B"
                strokeWidth="1"
              />
              {tickXPositions.map((x, idx) => (
                <line
                  key={idx}
                  x1={x}
                  y1={baselineY}
                  x2={x}
                  y2={baselineY + 7}
                  stroke="#334155"
                  strokeWidth="1"
                />
              ))}
              <g clipPath={`url(#${clipId})`}>
                {inSpline.areaPath && <path d={inSpline.areaPath} fill={`url(#${rxGradId})`} />}
                {!hideSecondary && outSpline.areaPath && (
                  <path d={outSpline.areaPath} fill={`url(#${txGradId})`} />
                )}
                {inSpline.linePath && (
                  <path
                    d={inSpline.linePath}
                    fill="none"
                    stroke={primaryColor}
                    strokeWidth="2.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    vectorEffect="non-scaling-stroke"
                  />
                )}
                {!hideSecondary && outSpline.linePath && (
                  <path
                    d={outSpline.linePath}
                    fill="none"
                    stroke={secondaryColor}
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    vectorEffect="non-scaling-stroke"
                  />
                )}
                {showEndpointMarkers && (
                  <>
                    <circle
                      cx={Math.min(width - 4, Math.max(4, inPts[inPts.length - 1].x))}
                      cy={inPts[inPts.length - 1].y}
                      r="3.5"
                      fill={primaryColor}
                      stroke="#0B101B"
                      strokeWidth="1.5"
                    />
                    {!hideSecondary && outPts.length > 0 && (
                      <circle
                        cx={Math.min(width - 4, Math.max(4, outPts[outPts.length - 1].x))}
                        cy={outPts[outPts.length - 1].y}
                        r="3.5"
                        fill={secondaryColor}
                        stroke="#0B101B"
                        strokeWidth="1.5"
                      />
                    )}
                  </>
                )}
              </g>
            </svg>

            {inPts.length === 0 && (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center pb-4 text-xs text-slate-500">
                {emptyMessage}
              </div>
            )}

            {displayedHover && inPts.length > 0 && (
              <>
                {/* Vertical crosshair line */}
                <div
                  className="pointer-events-none absolute top-0 bottom-4 w-px bg-slate-400/55 motion-reduce:transition-none"
                  style={{
                    left: `${(displayedHover.ratio * 100).toFixed(3)}%`,
                    opacity: isHovering ? 1 : 0,
                    transition: smoothPosTransition
                      ? "left 85ms cubic-bezier(0.22, 1, 0.36, 1), opacity 180ms ease-out"
                      : "opacity 180ms ease-out",
                  }}
                />

                {/* Hover-only markers on primary & secondary curves */}
                <div
                  className="pointer-events-none absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-[#0B101B] motion-reduce:transition-none"
                  style={{
                    backgroundColor: primaryColor,
                    boxShadow: `0 0 8px ${primaryColor}`,
                    left: `${(displayedHover.ratio * 100).toFixed(3)}%`,
                    top: `${(displayedHover.inYFrac * 100).toFixed(3)}%`,
                    opacity: isHovering ? 1 : 0,
                    transition: smoothPosTransition
                      ? "left 85ms cubic-bezier(0.22, 1, 0.36, 1), top 85ms cubic-bezier(0.22, 1, 0.36, 1), opacity 180ms ease-out"
                      : "opacity 180ms ease-out",
                  }}
                />
                {!hideSecondary && (
                  <div
                    className="pointer-events-none absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-[#0B101B] motion-reduce:transition-none"
                    style={{
                      backgroundColor: secondaryColor,
                      boxShadow: `0 0 8px ${secondaryColor}`,
                      left: `${(displayedHover.ratio * 100).toFixed(3)}%`,
                      top: `${(displayedHover.outYFrac * 100).toFixed(3)}%`,
                      opacity: isHovering ? 1 : 0,
                      transition: smoothPosTransition
                        ? "left 85ms cubic-bezier(0.22, 1, 0.36, 1), top 85ms cubic-bezier(0.22, 1, 0.36, 1), opacity 180ms ease-out"
                        : "opacity 180ms ease-out",
                    }}
                  />
                )}

                {/* Smooth floating tooltip box */}
                <div
                  className="pointer-events-none absolute top-0 left-0 z-20 w-[212px] will-change-transform motion-reduce:transition-none"
                  style={{
                    transform: `translate3d(${displayedHover.boxX.toFixed(1)}px, ${displayedHover.boxY.toFixed(1)}px, 0)`,
                    transition: smoothPosTransition
                      ? "transform 95ms cubic-bezier(0.22, 1, 0.36, 1)"
                      : "none",
                  }}
                >
                  <div
                    className={`rounded-lg border border-slate-700/90 bg-[#090D16]/95 px-3 py-2 text-xs shadow-xl shadow-black/50 backdrop-blur-md transition-[opacity,transform] duration-200 ease-out motion-reduce:transition-none ${
                      isHovering
                        ? "translate-y-0 scale-100 opacity-100"
                        : "translate-y-1 scale-95 opacity-0"
                    }`}
                  >
                    <div className="mb-1.5 border-b border-slate-800/80 pb-1 font-mono-tabular text-[11px] text-slate-400">
                      {displayedHover.timeText}
                    </div>
                    <div className="space-y-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="flex items-center gap-1.5 text-slate-300">
                          <span
                            className="h-2 w-2 rounded-full"
                            style={{ backgroundColor: primaryColor }}
                          />
                          {primaryShortLabel}
                        </span>
                        <span
                          className="font-mono-tabular font-semibold"
                          style={{ color: primaryColor }}
                        >
                          {formatVal(displayedHover.inVal)}
                        </span>
                      </div>
                      {!hideSecondary && (
                        <div className="flex items-center justify-between gap-2">
                          <span className="flex items-center gap-1.5 text-slate-300">
                            <span
                              className="h-2 w-2 rounded-full"
                              style={{ backgroundColor: secondaryColor }}
                            />
                            {secondaryShortLabel}
                          </span>
                          <span
                            className="font-mono-tabular font-semibold"
                            style={{ color: secondaryColor }}
                          >
                            {formatVal(displayedHover.outVal)}
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>

          <div className="mt-1 flex items-center justify-between border-t border-slate-800/70 pt-1.5 font-mono-tabular text-[11px] text-slate-400">
            {axisLabels.map((lbl, idx) => (
              <span key={idx}>{lbl}</span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

export const App: React.FC = () => {
  const initialRoute = parseAppRoute(
    typeof window !== "undefined" ? window.location.pathname : "/",
  );
  const [activeTab, setActiveTab] = useState<TabKey>(initialRoute.tab);
  const [navMenuOpen, setNavMenuOpen] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [settingsFeedback, setSettingsFeedback] = useState<{
    type: "ok" | "err";
    text: string;
  } | null>(null);

  // Update Notification, Staged Install Progress & Manual Install Prompt State
  const [updateInfo, setUpdateInfo] = useState<SystemUpdateStatus | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [installingUpdate, setInstallingUpdate] = useState(false);
  const [updateProgress, setUpdateProgress] = useState<UpdateProgressState | null>(null);
  const [updateMinimized, setUpdateMinimized] = useState(false);
  const DISMISSED_UPDATE_SHA_KEY = "netdash_dismissed_update_sha";
  const [promptDismissedSha, setPromptDismissedSha] = useState<string | null>(() => {
    try {
      return localStorage.getItem(DISMISSED_UPDATE_SHA_KEY);
    } catch {
      return null;
    }
  });
  const updateDialogRef = useRef<HTMLDialogElement | null>(null);
  const updateStartTimeRef = useRef<number>(0);
  const finishingUpdateRef = useRef<boolean>(false);
  const initialBundleSrcRef = useRef<string>("");
  const sawUpdatingRef = useRef<boolean>(false);
  const installRequestInFlightRef = useRef<boolean>(false);
  const latestProgressRef = useRef<UpdateProgressState | null>(null);
  latestProgressRef.current = updateProgress;
  const highWaterRef = useRef<{
    target: string;
    overall: number;
    compPcts: Record<string, number>;
  }>({ target: "", overall: 0, compPcts: {} });

  // Server Information & Background Service Health State
  const [serverInfo, setServerInfo] = useState<ServerSystemInfo | null>(null);
  const [loadingServerInfo, setLoadingServerInfo] = useState(false);
  const [serviceActionBusy, setServiceActionBusy] = useState<
    Record<string, "start" | "stop" | "restart" | null>
  >({});
  const [serviceActionBanner, setServiceActionBanner] = useState<{
    type: "ok" | "err";
    text: string;
  } | null>(null);
  const [viewingLogsService, setViewingLogsService] = useState<{
    id: string;
    name: string;
    container: string;
  } | null>(null);
  const [serviceLogsText, setServiceLogsText] = useState("");
  const [loadingServiceLogs, setLoadingServiceLogs] = useState(false);

  // First-Time Setup Onboarding Modal & Dynamic Config State
  const [config, setConfig] = useState<AllConfigPayload | null>(null);
  const onboardingDialogRef = useRef<HTMLDialogElement | null>(null);
  const [ispNameInput, setIspNameInput] = useState("");
  const [ispDownloadInput, setIspDownloadInput] = useState("1000");
  const [ispUploadInput, setIspUploadInput] = useState("1000");
  const [savingIsp, setSavingIsp] = useState(false);
  const ispFormInitializedRef = useRef(false);

  // Overview Dashboard Graph Source Configuration State
  const [overviewGraphOptions, setOverviewGraphOptions] = useState<OverviewGraphOptionDevice[]>([]);
  const [loadingOverviewGraphOptions, setLoadingOverviewGraphOptions] = useState(false);
  const [overviewGraphModeInput, setOverviewGraphModeInput] = useState<"auto" | "custom">("auto");
  const [overviewGraphSelectionsInput, setOverviewGraphSelectionsInput] = useState<
    OverviewGraphSelection[]
  >([]);
  const overviewGraphFormInitializedRef = useRef(false);
  const [overviewGraphSearch, setOverviewGraphSearch] = useState("");
  const [overviewGraphSnmpOnly, setOverviewGraphSnmpOnly] = useState(false);
  const [expandedOverviewDeviceIps, setExpandedOverviewDeviceIps] = useState<
    Record<string, boolean>
  >({});
  const [interfaceSearchByIp, setInterfaceSearchByIp] = useState<Record<string, string>>({});
  const [savingOverviewGraph, setSavingOverviewGraph] = useState(false);

  // Onboarding Wizard Draft Inputs
  const [obSubnetInput, setObSubnetInput] = useState("10.1.3.0/24");
  const [obSubnetLabel, setObSubnetLabel] = useState("Main Network");
  const [obSubnetsList, setObSubnetsList] = useState<Array<{ subnet_cidr: string; label: string }>>(
    [],
  );
  const [obHostInput, setObHostInput] = useState("");
  const [obHostLabel, setObHostLabel] = useState("");
  const [obHostsList, setObHostsList] = useState<Array<{ device_ip: string; label: string }>>([]);
  const [obSnmpCommunity, setObSnmpCommunity] = useState("public");
  const [showObSecret, setShowObSecret] = useState(false);
  const [savingOnboarding, setSavingOnboarding] = useState(false);

  // Settings Tab Form States (Multiple SNMP Profiles with Edit & Reveal Support)
  const [editingProfileId, setEditingProfileId] = useState<number | null>(null);
  const [newProfileName, setNewProfileName] = useState("");
  const [newProfileVersion, setNewProfileVersion] = useState("v2c");
  const [newProfilePort, setNewProfilePort] = useState(161);
  const [newProfileCommunity, setNewProfileCommunity] = useState("public");
  const [newProfileV3User, setNewProfileV3User] = useState("");
  const [newProfileV3AuthProto, setNewProfileV3AuthProto] = useState("SHA");
  const [newProfileV3AuthKey, setNewProfileV3AuthKey] = useState("");
  const [newProfileV3PrivProto, setNewProfileV3PrivProto] = useState("AES");
  const [newProfileV3PrivKey, setNewProfileV3PrivKey] = useState("");
  const [showFormSecrets, setShowFormSecrets] = useState(false);
  const [revealedProfileIds, setRevealedProfileIds] = useState<Record<number, boolean>>({});

  // Monitored SNMP Host Form State (Add & Edit)
  const [editingHostId, setEditingHostId] = useState<number | null>(null);
  const [newHostIp, setNewHostIp] = useState("");
  const [newHostLabel, setNewHostLabel] = useState("");
  const [newHostProfileId, setNewHostProfileId] = useState<number>(0);

  // Subnet Form State
  const [newSubnetCidr, setNewSubnetCidr] = useState("");
  const [newSubnetLabel, setNewSubnetLabel] = useState("");
  const [newSubnetVlan, setNewSubnetVlan] = useState("");

  // Optional UniFi Controller Form State
  const [newUnifiName, setNewUnifiName] = useState("UDM Pro");
  const [newUnifiHost, setNewUnifiHost] = useState("");
  const [newUnifiApiKey, setNewUnifiApiKey] = useState("");
  const [newUnifiSite, setNewUnifiSite] = useState("default");
  const [showUnifiKey, setShowUnifiKey] = useState(false);

  // Server Backend MIB Catalog & Custom MIB Upload State
  const [storedMibs, setStoredMibs] = useState<
    Array<{
      module_name: string;
      filename: string;
      organization: string;
      description: string;
      enterprise_pen: string;
      object_count: number;
    }>
  >([]);
  const [vendorCatalog, setVendorCatalog] = useState<
    Array<{
      key: string;
      pens: string[];
      vendor_name: string;
      category: string;
      mibs: string[];
    }>
  >([]);
  const [customMibFilename, setCustomMibFilename] = useState("");
  const [customMibContent, setCustomMibContent] = useState("");

  // Devices Inventory & Per-Device Detail Page State
  const [allDevices, setAllDevices] = useState<UnifiedDeviceItem[]>([]);
  const [deviceSearch, setDeviceSearch] = useState("");
  const [deviceFilter, setDeviceFilter] = useState<"all" | "online" | "snmp">("all");
  const [deviceSortKey, setDeviceSortKey] = useState<DeviceSortKey>("ip_address");
  const [deviceSortDir, setDeviceSortDir] = useState<SortDirection>("asc");
  const [selectedDeviceRef, setSelectedDeviceRef] = useState<string | null>(initialRoute.deviceRef);
  const [selectedDeviceDetailIp, setSelectedDeviceDetailIp] = useState<string | null>(
    initialRoute.deviceRef && !/^\d+$/.test(initialRoute.deviceRef) ? initialRoute.deviceRef : null,
  );
  const [deviceDetail, setDeviceDetail] = useState<DeviceDetailPayload | null>(null);
  const [loadingDeviceDetail, setLoadingDeviceDetail] = useState(false);
  const [deviceDetailError, setDeviceDetailError] = useState<string | null>(null);
  const deviceDetailReqIdRef = useRef<number>(0);
  const lastLoadedDeviceRef = useRef<string | null>(null);
  const [detailIfIndex, setDetailIfIndex] = useState<number>(0);
  const [detailTrendRange, setDetailTrendRange] = useState<string>("24h");
  const [detailTrendSeries, setDetailTrendSeries] = useState<SNMPTrendPoint[]>([]);
  const [pollingDeviceNow, setPollingDeviceNow] = useState(false);
  const [rematchingMibs, setRematchingMibs] = useState(false);

  // Domain Telemetry State
  const [overview, setOverview] = useState<OverviewPayload | null>(null);
  const [overviewGraphMode, setOverviewGraphMode] = useState<OverviewRangeKey>("live");
  const [overviewScaleMode, setOverviewScaleMode] = useState<"auto" | "isp">(() => {
    try {
      return localStorage.getItem("netdash_overview_scale_mode") === "isp" ? "isp" : "auto";
    } catch {
      return "auto";
    }
  });
  const [liveTrafficSeries, setLiveTrafficSeries] = useState<LiveTrafficSample[]>([]);
  const [overviewHistorySeries, setOverviewHistorySeries] = useState<
    Array<{ time: string; rx_bps: number; tx_bps: number; latency_ms: number }>
  >([]);
  const [loadingOverviewRange, setLoadingOverviewRange] = useState(false);
  const [interfaces, setInterfaces] = useState<SNMPInterface[]>([]);
  const [snmpTelemetry, setSnmpTelemetry] = useState<SNMPTelemetryPayload | null>(null);
  const [snmpTelemetryRange, setSnmpTelemetryRange] = useState<"1h" | "6h" | "24h" | "7d" | "30d">(
    "24h",
  );
  const snmpTelemetryRangeRef = useRef<"1h" | "6h" | "24h" | "7d" | "30d">("24h");
  snmpTelemetryRangeRef.current = snmpTelemetryRange;
  const [snmpChartMode, setSnmpChartMode] = useState<"ingestion" | "latency" | "polls">(
    "ingestion",
  );
  const [snmpPortFilterIp, setSnmpPortFilterIp] = useState<string>("");
  const [pollingAllSnmp, setPollingAllSnmp] = useState(false);
  const [upsHistory, setUpsHistory] = useState<UPSHistoryPoint[]>([]);
  const [upsHistoryRange, setUpsHistoryRange] = useState<"1h" | "6h" | "24h" | "7d" | "30d">(
    "24h",
  );
  const [upsChartMode, setUpsChartMode] = useState<"battery_load" | "voltage" | "runtime" | "watts">(
    "battery_load",
  );

  const [subnets, setSubnets] = useState<string[]>([]);
  const [selectedSubnet, setSelectedSubnet] = useState<string>("");
  const [hosts, setHosts] = useState<DiscoveredHost[]>([]);
  const [scanningNow, setScanningNow] = useState(false);

  const [logs, setLogs] = useState<SyslogEntry[]>([]);
  const [logSources, setLogSources] = useState<Array<{ source_ip: string; hostname: string }>>([]);
  const [logSeverity, setLogSeverity] = useState<string>("all");
  const [logSourceIp, setLogSourceIp] = useState<string>("all");
  const [logSearch, setLogSearch] = useState<string>("");
  const [logsPaused, setLogsPaused] = useState(false);

  const flashFeedback = (type: "ok" | "err", text: string) => {
    setSettingsFeedback({ type, text });
    setTimeout(() => setSettingsFeedback(null), 5000);
  };

  const persistDismissedUpdateSha = (sha: string | null) => {
    setPromptDismissedSha(sha);
    try {
      if (sha) {
        localStorage.setItem(DISMISSED_UPDATE_SHA_KEY, sha);
      } else {
        localStorage.removeItem(DISMISSED_UPDATE_SHA_KEY);
      }
    } catch {
      // ignore storage errors
    }
  };

  const openUpdateModal = () => {
    if (activeTab !== "settings") return;
    const dlg = updateDialogRef.current;
    if (dlg && !dlg.open) dlg.showModal();
  };

  const closeUpdateModal = () => {
    const dlg = updateDialogRef.current;
    if (dlg && dlg.open) dlg.close();
    if (updateInfo?.remote_sha) persistDismissedUpdateSha(updateInfo.remote_sha);
  };

  const openOnboardingModal = () => {
    const dlg = onboardingDialogRef.current;
    if (dlg && !dlg.open) dlg.showModal();
  };

  const closeOnboardingModal = () => {
    const dlg = onboardingDialogRef.current;
    if (dlg && dlg.open) dlg.close();
  };

  const syncConfigState = (data: AllConfigPayload) => {
    setConfig(data);
    if (!ispFormInitializedRef.current && data.isp_settings) {
      setIspNameInput(data.isp_settings.isp_name || "");
      setIspDownloadInput(String(data.isp_settings.download_mbps ?? 1000));
      setIspUploadInput(String(data.isp_settings.upload_mbps ?? 1000));
      ispFormInitializedRef.current = true;
    }
    if (!overviewGraphFormInitializedRef.current && data.overview_graph_settings) {
      const modeVal = data.overview_graph_settings.mode === "custom" ? "custom" : "auto";
      const sels = Array.isArray(data.overview_graph_settings.selections)
        ? data.overview_graph_settings.selections
        : [];
      setOverviewGraphModeInput(modeVal);
      setOverviewGraphSelectionsInput(sels);
      const expandedInit: Record<string, boolean> = {};
      for (const s of sels) {
        expandedInit[s.device_ip] = true;
      }
      setExpandedOverviewDeviceIps((prev) => ({ ...expandedInit, ...prev }));
      overviewGraphFormInitializedRef.current = true;
    }
    if (data.snmp_profiles.length > 0) {
      const exists = data.snmp_profiles.some((p) => p.id === newHostProfileId);
      if (!exists) {
        setNewHostProfileId(data.snmp_profiles[0].id);
      }
    } else {
      setNewHostProfileId(0);
    }
  };

  const fetchConfig = async () => {
    try {
      const r = await fetch("/api/config");
      if (r.ok) {
        const data: AllConfigPayload = await r.json();
        syncConfigState(data);
        if (!data.onboarding.completed) {
          openOnboardingModal();
        }
      }
    } catch {
      // ignore
    }
  };

  const handleSaveIspSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    const dl = Number(ispDownloadInput || 0);
    const ul = Number(ispUploadInput || 0);
    if (
      Number.isNaN(dl) ||
      Number.isNaN(ul) ||
      dl < 0 ||
      ul < 0 ||
      dl > 1_000_000 ||
      ul > 1_000_000
    ) {
      flashFeedback("err", "Internet speeds must be between 0 and 1,000,000 Mbps.");
      return;
    }
    setSavingIsp(true);
    try {
      const r = await fetch("/api/config/isp", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          isp_name: ispNameInput.trim(),
          download_mbps: dl,
          upload_mbps: ul,
        }),
      });
      if (r.ok) {
        const updated: AllConfigPayload = await r.json();
        syncConfigState(updated);
        if (updated.isp_settings) {
          setIspNameInput(updated.isp_settings.isp_name || "");
          setIspDownloadInput(String(updated.isp_settings.download_mbps ?? dl));
          setIspUploadInput(String(updated.isp_settings.upload_mbps ?? ul));
        }
        fetchOverview();
        flashFeedback("ok", "Saved ISP internet speed settings.");
      } else {
        const errBody = await r.json().catch(() => null);
        flashFeedback("err", errBody?.detail || "Failed to save ISP settings.");
      }
    } catch {
      flashFeedback("err", "Network error saving ISP settings.");
    } finally {
      setSavingIsp(false);
    }
  };

  const fetchOverviewGraphOptions = async () => {
    setLoadingOverviewGraphOptions(true);
    try {
      const r = await fetch("/api/config/overview-graph/options");
      if (r.ok) {
        const data = await r.json();
        setOverviewGraphOptions(data.devices || []);
      }
    } catch {
      // ignore
    } finally {
      setLoadingOverviewGraphOptions(false);
    }
  };

  const toggleOverviewDevice = (ip: string) => {
    const exists = overviewGraphSelectionsInput.some((s) => s.device_ip === ip);
    if (exists) {
      setOverviewGraphSelectionsInput((prev) => prev.filter((s) => s.device_ip !== ip));
    } else {
      setOverviewGraphSelectionsInput((prev) => [...prev, { device_ip: ip, if_indices: [] }]);
      setExpandedOverviewDeviceIps((prev) => ({ ...prev, [ip]: true }));
    }
  };

  const setOverviewDeviceAllInterfaces = (ip: string) => {
    setOverviewGraphSelectionsInput((prev) => {
      const exists = prev.some((s) => s.device_ip === ip);
      if (!exists) return [...prev, { device_ip: ip, if_indices: [] }];
      return prev.map((s) => (s.device_ip === ip ? { ...s, if_indices: [] } : s));
    });
  };

  const toggleOverviewDeviceInterface = (ip: string, ifIndex: number) => {
    setOverviewGraphSelectionsInput((prev) => {
      const existing = prev.find((s) => s.device_ip === ip);
      if (!existing) {
        return [...prev, { device_ip: ip, if_indices: [ifIndex] }];
      }
      const nextIdxs = existing.if_indices.includes(ifIndex)
        ? existing.if_indices.filter((idx) => idx !== ifIndex)
        : [...existing.if_indices, ifIndex].sort((a, b) => a - b);
      return prev.map((s) => (s.device_ip === ip ? { ...s, if_indices: nextIdxs } : s));
    });
  };

  const removeOverviewSelectionChip = (ip: string, ifIndex?: number) => {
    if (ifIndex == null) {
      setOverviewGraphSelectionsInput((prev) => prev.filter((s) => s.device_ip !== ip));
      return;
    }
    setOverviewGraphSelectionsInput((prev) =>
      prev.flatMap((s) => {
        if (s.device_ip !== ip) return [s];
        const nextIdxs = s.if_indices.filter((idx) => idx !== ifIndex);
        if (nextIdxs.length === 0) return [];
        return [{ ...s, if_indices: nextIdxs }];
      }),
    );
  };

  const handleSaveOverviewGraphSettings = async () => {
    if (overviewGraphModeInput === "custom" && overviewGraphSelectionsInput.length === 0) {
      flashFeedback("err", "Select at least one device when using Custom mode.");
      return;
    }
    setSavingOverviewGraph(true);
    try {
      const r = await fetch("/api/config/overview-graph", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: overviewGraphModeInput,
          selections: overviewGraphModeInput === "custom" ? overviewGraphSelectionsInput : [],
        }),
      });
      if (r.ok) {
        const updated: AllConfigPayload = await r.json();
        syncConfigState(updated);
        if (updated.overview_graph_settings) {
          setOverviewGraphModeInput(updated.overview_graph_settings.mode);
          setOverviewGraphSelectionsInput(updated.overview_graph_settings.selections);
        }
        setLiveTrafficSeries([]);
        fetchOverview();
        if (overviewGraphMode !== "live") {
          fetchOverviewTraffic(overviewGraphMode, true);
        }
        flashFeedback("ok", "Overview Dashboard graph source saved.");
      } else {
        const errBody = await r.json().catch(() => null);
        flashFeedback("err", errBody?.detail || "Failed to save Overview graph source.");
      }
    } catch {
      flashFeedback("err", "Network error saving Overview graph source.");
    } finally {
      setSavingOverviewGraph(false);
    }
  };

  const fetchAllDevices = async () => {
    try {
      const r = await fetch("/api/devices");
      if (r.ok) {
        const data = await r.json();
        setAllDevices(data.devices || []);
      }
    } catch {
      // ignore
    }
  };

  const fetchMibs = async () => {
    try {
      const r = await fetch("/api/mibs");
      if (r.ok) {
        const data = await r.json();
        setStoredMibs(data.stored_mibs || []);
        setVendorCatalog(data.vendor_catalog || []);
      }
    } catch {
      // ignore
    }
  };

  const handleUploadMib = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!customMibContent.trim()) return;
    const r = await fetch("/api/mibs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: customMibFilename.trim() || "custom.mib",
        content: customMibContent,
      }),
    });
    if (r.ok) {
      const data = await r.json();
      setStoredMibs(data.stored_mibs || []);
      setCustomMibFilename("");
      setCustomMibContent("");
      flashFeedback("ok", "Saved MIB file to server backend (/app/mibs) and indexed OIDs.");
    } else {
      flashFeedback("err", "Failed to save custom MIB file.");
    }
  };

  const fetchDeviceDetail = async (deviceRef: string, resetToOverview = false) => {
    const reqId = ++deviceDetailReqIdRef.current;
    setLoadingDeviceDetail(true);
    try {
      const r = await fetch(`/api/devices/${encodeURIComponent(deviceRef)}`);
      if (reqId !== deviceDetailReqIdRef.current) return;
      if (r.status === 404) {
        setDeviceDetail(null);
        setDeviceDetailError(`Device "${deviceRef}" was not found.`);
        return;
      }
      if (r.ok) {
        const data: DeviceDetailPayload = await r.json();
        if (reqId !== deviceDetailReqIdRef.current) return;
        setDeviceDetailError(null);
        setDeviceDetail(data);
        setSelectedDeviceDetailIp(data.ip_address);

        if (data.device_id != null) {
          const canonicalRef = String(data.device_id);
          const canonicalPath = `/devices/${canonicalRef}`;
          lastLoadedDeviceRef.current = canonicalRef;
          if (typeof window !== "undefined" && window.location.pathname !== canonicalPath) {
            window.history.replaceState(null, "", canonicalPath);
          }
          setSelectedDeviceRef(canonicalRef);
        } else {
          lastLoadedDeviceRef.current = deviceRef;
        }

        if (data.snmp_interfaces && data.snmp_interfaces.length > 0) {
          const targetIdx = resetToOverview ? 0 : detailIfIndex;
          if (resetToOverview) setDetailIfIndex(0);
          fetchDetailTrend(data.ip_address, targetIdx, detailTrendRange);
        } else {
          setDetailTrendSeries([]);
        }
        if (
          data.device_profile_type === "ups" ||
          Boolean(data.snmp_system?.extended_metrics?.ups_telemetry)
        ) {
          fetchUpsHistory(data.ip_address, upsHistoryRange);
        } else {
          setUpsHistory([]);
        }
      } else {
        setDeviceDetail(null);
        setDeviceDetailError(
          `Failed to load telemetry for device "${deviceRef}" (HTTP ${r.status}).`,
        );
      }
    } catch {
      if (reqId === deviceDetailReqIdRef.current) {
        setDeviceDetailError(`Unable to load telemetry for device "${deviceRef}".`);
      }
    } finally {
      if (reqId === deviceDetailReqIdRef.current) {
        setLoadingDeviceDetail(false);
      }
    }
  };

  const fetchDetailTrend = async (ip: string, ifIdx: number, rk: string) => {
    try {
      const r = await fetch(
        `/api/snmp/trends?device_ip=${encodeURIComponent(ip)}&if_index=${ifIdx}&range=${rk}`,
      );
      if (r.ok) {
        const data = await r.json();
        setDetailTrendSeries(data.series || []);
      }
    } catch {
      // ignore
    }
  };

  const navigateToPath = (path: string, replace = false) => {
    const targetPath = (path || "/").replace(/\/+$/, "") || "/";
    if (typeof window !== "undefined" && window.location.pathname !== targetPath) {
      if (replace) {
        window.history.replaceState(null, "", targetPath);
      } else {
        window.history.pushState(null, "", targetPath);
      }
    }
    const nextRoute = parseAppRoute(targetPath);
    setActiveTab(nextRoute.tab);
    setSelectedDeviceRef(nextRoute.deviceRef);
    setNavMenuOpen(false);
    if (!nextRoute.deviceRef) {
      lastLoadedDeviceRef.current = null;
      setSelectedDeviceDetailIp(null);
      setDeviceDetail(null);
      setDeviceDetailError(null);
    }
  };

  const navigateToTab = (tab: TabKey) => {
    navigateToPath(TAB_PATHS[tab]);
  };

  const toggleOverviewScaleMode = () => {
    setOverviewScaleMode((prev) => {
      const next = prev === "auto" ? "isp" : "auto";
      try {
        localStorage.setItem("netdash_overview_scale_mode", next);
      } catch {
        // ignore
      }
      return next;
    });
  };

  const openDevicePage = (ip: string, deviceId?: number | null) => {
    const resolvedId = deviceId ?? deviceIdByIp[ip] ?? null;
    const nextRef = resolvedId != null ? String(resolvedId) : ip;
    setSelectedDeviceDetailIp(ip);
    setDeviceDetail(null);
    setDeviceDetailError(null);
    setDetailIfIndex(0);
    setDetailTrendRange("24h");
    setUpsHistoryRange("24h");
    setUpsHistory([]);
    setUpsChartMode("battery_load");
    navigateToPath(`/devices/${encodeURIComponent(nextRef)}`);
  };

  const handleRematchAllMibs = async () => {
    setRematchingMibs(true);
    try {
      await fetch("/api/devices/rematch-mibs", { method: "POST" });
      setTimeout(() => {
        fetchAllDevices();
        fetchConfig();
        fetchOverview();
        fetchInterfaces();
        if (selectedDeviceRef) {
          fetchDeviceDetail(selectedDeviceRef, false);
        }
        setRematchingMibs(false);
      }, 4500);
    } catch {
      setRematchingMibs(false);
    }
  };

  const handlePollSingleDeviceNow = async (ip: string) => {
    setPollingDeviceNow(true);
    try {
      const r = await fetch(`/api/devices/${encodeURIComponent(ip)}/poll`, { method: "POST" });
      let status = "triggered";
      if (r.ok) {
        try {
          const body = await r.json();
          status = body?.status || "triggered";
        } catch {
          status = "triggered";
        }
      }
      await Promise.all([fetchDeviceDetail(ip), fetchAllDevices()]);
      if (status !== "completed") {
        setTimeout(() => {
          fetchDeviceDetail(ip);
          fetchAllDevices();
        }, 2000);
      }
    } finally {
      setPollingDeviceNow(false);
    }
  };

  const handleQuickEnableSnmpOnDevice = async (ip: string, hostname: string) => {
    const profileId = config?.snmp_profiles?.[0]?.id || null;
    const r = await fetch("/api/config/snmp-targets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_ip: ip,
        label: hostname !== ip ? hostname : "",
        profile_id: profileId,
        enabled: true,
      }),
    });
    if (r.ok) {
      syncConfigState(await r.json());
      handlePollSingleDeviceNow(ip);
    }
  };

  const handleCompleteOnboarding = async (skip: boolean) => {
    setSavingOnboarding(true);
    try {
      const pendingSubnets = [...obSubnetsList];
      if (!skip && obSubnetInput.trim()) {
        pendingSubnets.push({
          subnet_cidr: obSubnetInput.trim(),
          label: obSubnetLabel.trim(),
        });
      }
      const pendingHosts = [...obHostsList];
      if (!skip && obHostInput.trim()) {
        pendingHosts.push({
          device_ip: obHostInput.trim(),
          label: obHostLabel.trim(),
        });
      }

      const payload = {
        skipped: skip,
        subnets: skip ? [] : pendingSubnets,
        snmp_profiles:
          !skip && obSnmpCommunity.trim()
            ? [
                {
                  name: "Default v2c (public)",
                  version: "v2c",
                  port: 161,
                  community: obSnmpCommunity.trim(),
                  poll_interval: 60,
                },
              ]
            : [],
        snmp_targets: skip ? [] : pendingHosts,
        unifi_controllers: [],
      };

      const r = await fetch("/api/config/onboarding", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        const data: AllConfigPayload = await r.json();
        syncConfigState(data);
        closeOnboardingModal();
        fetchOverview();
        fetchScanner();
        fetchInterfaces();
        fetchAllDevices();
      }
    } finally {
      setSavingOnboarding(false);
    }
  };

  const resetProfileForm = () => {
    setEditingProfileId(null);
    setNewProfileName("");
    setNewProfileVersion("v2c");
    setNewProfilePort(161);
    setNewProfileCommunity("public");
    setNewProfileV3User("");
    setNewProfileV3AuthProto("SHA");
    setNewProfileV3AuthKey("");
    setNewProfileV3PrivProto("AES");
    setNewProfileV3PrivKey("");
  };

  const startEditingProfile = (p: SNMPProfile) => {
    setEditingProfileId(p.id);
    setNewProfileName(p.name);
    setNewProfileVersion(p.version);
    setNewProfilePort(p.port);
    setNewProfileCommunity(p.community || "");
    setNewProfileV3User(p.v3_user || "");
    setNewProfileV3AuthProto(p.v3_auth_proto || "SHA");
    setNewProfileV3AuthKey(p.v3_auth_key || "");
    setNewProfileV3PrivProto(p.v3_priv_proto || "AES");
    setNewProfileV3PrivKey(p.v3_priv_key || "");
    setShowFormSecrets(true);
  };

  const handleSaveProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProfileName.trim()) return;
    const url = editingProfileId
      ? `/api/config/snmp-profiles/${editingProfileId}`
      : "/api/config/snmp-profiles";
    const method = editingProfileId ? "PUT" : "POST";

    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: newProfileName.trim(),
        version: newProfileVersion,
        port: Number(newProfilePort) || 161,
        community: newProfileCommunity,
        v3_user: newProfileV3User,
        v3_auth_proto: newProfileV3AuthProto,
        v3_auth_key: newProfileV3AuthKey,
        v3_priv_proto: newProfileV3PrivProto,
        v3_priv_key: newProfileV3PrivKey,
        poll_interval: 60,
      }),
    });
    if (r.ok) {
      syncConfigState(await r.json());
      flashFeedback(
        "ok",
        editingProfileId
          ? `Updated SNMP profile "${newProfileName.trim()}".`
          : `Created SNMP profile "${newProfileName.trim()}".`,
      );
      resetProfileForm();
    } else {
      flashFeedback("err", "Failed to save SNMP profile.");
    }
  };

  const handleDeleteProfile = async (id: number) => {
    const r = await fetch(`/api/config/snmp-profiles/${id}`, { method: "DELETE" });
    if (r.ok) {
      syncConfigState(await r.json());
      if (editingProfileId === id) resetProfileForm();
      flashFeedback("ok", "Deleted SNMP profile.");
    }
  };

  const startEditingHost = (t: SNMPTargetConfig) => {
    setEditingHostId(t.id);
    setNewHostIp(t.device_ip);
    setNewHostLabel(t.label || "");
    setNewHostProfileId(t.profile_id || config?.snmp_profiles?.[0]?.id || 0);
  };

  const resetHostForm = () => {
    setEditingHostId(null);
    setNewHostIp("");
    setNewHostLabel("");
  };

  const handleSaveHost = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newHostIp.trim()) return;
    const url = editingHostId
      ? `/api/config/snmp-targets/${editingHostId}`
      : "/api/config/snmp-targets";
    const method = editingHostId ? "PUT" : "POST";
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_ip: newHostIp.trim(),
        label: newHostLabel.trim(),
        profile_id: newHostProfileId > 0 ? newHostProfileId : null,
        enabled: true,
      }),
    });
    if (r.ok) {
      syncConfigState(await r.json());
      flashFeedback(
        "ok",
        editingHostId
          ? `Updated monitored SNMP host ${newHostIp.trim()}.`
          : `Added monitored SNMP host ${newHostIp.trim()} and queued SNMP poll.`,
      );
      resetHostForm();
      fetchOverview();
      fetchAllDevices();
    } else {
      const errData = await r.json().catch(() => ({}));
      flashFeedback("err", errData.detail || "Unable to save monitored SNMP host.");
    }
  };

  const handleDeleteHost = async (id: number) => {
    const r = await fetch(`/api/config/snmp-targets/${id}`, { method: "DELETE" });
    if (r.ok) {
      syncConfigState(await r.json());
      if (editingHostId === id) resetHostForm();
      fetchOverview();
      fetchAllDevices();
      flashFeedback("ok", "Removed monitored SNMP host.");
    }
  };

  const handleAddSubnet = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newSubnetCidr.trim()) return;
    const r = await fetch("/api/config/subnets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subnet_cidr: newSubnetCidr.trim(),
        label: newSubnetLabel.trim(),
        vlan_id: newSubnetVlan.trim() ? Number(newSubnetVlan) : null,
        enabled: true,
      }),
    });
    if (r.ok) {
      syncConfigState(await r.json());
      flashFeedback("ok", `Added subnet ${newSubnetCidr.trim()} and queued Nmap discovery sweep.`);
      setNewSubnetCidr("");
      setNewSubnetLabel("");
      setNewSubnetVlan("");
      fetchScanner();
    } else {
      const errData = await r.json().catch(() => ({}));
      flashFeedback("err", errData.detail || "Invalid IPv4 subnet CIDR.");
    }
  };

  const handleDeleteSubnet = async (id: number) => {
    const r = await fetch(`/api/config/subnets/${id}`, { method: "DELETE" });
    if (r.ok) {
      syncConfigState(await r.json());
      fetchScanner();
      fetchAllDevices();
      flashFeedback("ok", "Removed subnet.");
    }
  };

  const handleAddUnifi = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newUnifiHost.trim()) return;
    const r = await fetch("/api/config/unifi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: newUnifiName.trim() || "UDM Pro",
        host: newUnifiHost.trim(),
        api_key: newUnifiApiKey.trim(),
        site_name: newUnifiSite.trim() || "default",
        verify_ssl: false,
        poll_interval: 30,
        enabled: true,
      }),
    });
    if (r.ok) {
      syncConfigState(await r.json());
      setNewUnifiHost("");
      setNewUnifiApiKey("");
      flashFeedback("ok", "Saved UniFi Controller configuration.");
      setTimeout(fetchOverview, 2000);
    }
  };

  const handleDeleteUnifi = async (id: number) => {
    const r = await fetch(`/api/config/unifi/${id}`, { method: "DELETE" });
    if (r.ok) syncConfigState(await r.json());
  };

  const fetchUpdateStatus = async (forceFetch = false) => {
    if (forceFetch) setCheckingUpdate(true);
    try {
      const resp = await fetch(
        forceFetch ? "/api/system/update/check" : "/api/system/update",
        { method: forceFetch ? "POST" : "GET" },
      );
      if (resp.ok) {
        const data: SystemUpdateStatus = await resp.json();
        setUpdateInfo(data);
        if (!data.update_available) {
          persistDismissedUpdateSha(null);
        }
      }
    } catch {
      // ignore
    } finally {
      if (forceFetch) setCheckingUpdate(false);
    }
  };

  const getCurrentBundleSrc = (): string => {
    if (typeof document === "undefined") return "";
    return document.querySelector('script[type="module"]')?.getAttribute("src") || "";
  };

  const fetchWithTimeout = async (
    url: string,
    options: RequestInit = {},
    timeoutMs = 2500,
  ): Promise<Response> => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, { ...options, signal: controller.signal });
    } finally {
      window.clearTimeout(timer);
    }
  };

  const fetchUpdateProgress = async (): Promise<UpdateProgressState | null> => {
    // 1. Try API endpoint first so backend _reconcile_update_progress() handles legacy scripts & completed states
    try {
      const r = await fetchWithTimeout(
        `/api/system/update/progress?t=${Date.now()}`,
        { cache: "no-store" },
        2500,
      );
      if (r.ok) {
        const d: UpdateProgressState = await r.json();
        if (d && d.status) return d;
      }
    } catch {
      // fall through to static nginx file while API container restarts
    }
    // 2. Fallback to static Nginx /update-progress.json while backend API is restarting
    try {
      const r2 = await fetchWithTimeout(
        `/update-progress.json?t=${Date.now()}`,
        { cache: "no-store" },
        2500,
      );
      if (r2.ok) {
        const d2: UpdateProgressState = await r2.json();
        if (d2 && d2.status) {
          if (d2.status === "updating" && d2.updated_at) {
            const ageMs = Date.now() - new Date(d2.updated_at).getTime();
            if (!Number.isNaN(ageMs) && ageMs > 15 * 60 * 1000) {
              return { ...d2, status: "idle", overall_pct: 0 };
            }
          }
          return d2;
        }
      }
    } catch {
      // ignore if edge proxy is reloading
    }
    return null;
  };

  const mergeMonotonicProgress = (incoming: UpdateProgressState): UpdateProgressState => {
    const targetKey = incoming.update_id || incoming.target_sha || "";
    if (targetKey && highWaterRef.current.target !== targetKey) {
      highWaterRef.current = { target: targetKey, overall: 0, compPcts: {} };
    }
    if (incoming.status === "completed") {
      highWaterRef.current.overall = 100;
      const doneComps = (incoming.components || []).map((c) => {
        highWaterRef.current.compPcts[c.id] = 100;
        return { ...c, pct: 100, status: "completed" };
      });
      return { ...incoming, overall_pct: 100, components: doneComps };
    }
    if (incoming.status === "updating") {
      const clampedOverall = Math.max(highWaterRef.current.overall, incoming.overall_pct || 0);
      highWaterRef.current.overall = clampedOverall;
      const clampedComps = (incoming.components || []).map((c) => {
        const prevPct = highWaterRef.current.compPcts[c.id] || 0;
        const nextPct = Math.max(prevPct, c.pct || 0);
        highWaterRef.current.compPcts[c.id] = nextPct;
        return { ...c, pct: nextPct };
      });
      return { ...incoming, overall_pct: clampedOverall, components: clampedComps };
    }
    return incoming;
  };

  const handleDismissUpdateProgress = async () => {
    const wasCompleted = latestProgressRef.current?.status === "completed";
    finishingUpdateRef.current = false;
    sawUpdatingRef.current = false;
    installRequestInFlightRef.current = false;
    highWaterRef.current = { target: "", overall: 0, compPcts: {} };
    setInstallingUpdate(false);
    setUpdateProgress(null);
    setUpdateMinimized(false);
    try {
      sessionStorage.removeItem("netdash_update_minimized");
    } catch {
      // ignore
    }
    try {
      await fetchWithTimeout("/api/system/update/dismiss", { method: "POST" }, 2000);
    } catch {
      // ignore
    }
    if (wasCompleted) {
      window.location.reload();
    }
  };

  const handleInstallUpdate = async () => {
    const dlg = updateDialogRef.current;
    if (dlg && dlg.open) dlg.close();
    const fromSha = updateInfo?.current_sha || serverInfo?.server?.git?.current_sha || "current";
    const targetSha = updateInfo?.remote_sha || "latest";
    const fromVer =
      updateInfo?.current_version || serverInfo?.server?.git?.current_version || fromSha;
    const targetVer = updateInfo?.remote_version || targetSha;
    updateStartTimeRef.current = Date.now();
    finishingUpdateRef.current = false;
    initialBundleSrcRef.current = getCurrentBundleSrc();
    sawUpdatingRef.current = false;
    installRequestInFlightRef.current = true;
    highWaterRef.current = { target: targetSha, overall: 1, compPcts: { git: 10 } };

    setUpdateMinimized(false);
    setInstallingUpdate(true);
    setUpdateProgress({
      status: "updating",
      overall_pct: 1,
      phase: "queued",
      message: `Queued update (${fromVer} → ${targetVer})...`,
      from_sha: fromSha,
      target_sha: targetSha,
      from_version: fromVer,
      target_version: targetVer,
      components: [
        {
          id: "git",
          name: "Source Code (GitHub)",
          status: "running",
          pct: 10,
          detail: "Queuing host updater...",
        },
        {
          id: "frontend",
          name: "Web Frontend (React + Nginx)",
          status: "pending",
          pct: 0,
          detail: "Waiting for source update...",
        },
        {
          id: "api",
          name: "Backend API (FastAPI)",
          status: "pending",
          pct: 0,
          detail: "Waiting for frontend...",
        },
        {
          id: "worker",
          name: "Telemetry Worker (SNMP / Nmap)",
          status: "pending",
          pct: 0,
          detail: "Waiting for backend build...",
        },
        {
          id: "services",
          name: "Core Services & Health Check",
          status: "pending",
          pct: 0,
          detail: "Waiting for services...",
        },
      ],
    });

    try {
      const resp = await fetch("/api/system/update/install", { method: "POST" });
      if (resp.ok) {
        const body = await resp.json().catch(() => null);
        if (body?.progress) {
          if (body.progress.status === "updating") {
            sawUpdatingRef.current = true;
          }
          setUpdateProgress(mergeMonotonicProgress(body.progress));
        }
      }
    } catch {
      // Progress poller continues watching update status
    } finally {
      installRequestInFlightRef.current = false;
    }
  };

  const fetchServerInfo = async (showSpinner = false) => {
    if (showSpinner) setLoadingServerInfo(true);
    try {
      const resp = await fetch("/api/system/info");
      if (resp.ok) {
        setServerInfo(await resp.json());
      }
    } catch {
      // ignore
    } finally {
      if (showSpinner) setLoadingServerInfo(false);
    }
  };

  const handleViewServiceLogs = async (svc: BackgroundServiceInfo) => {
    setViewingLogsService({ id: svc.id, name: svc.name, container: svc.container });
    setLoadingServiceLogs(true);
    try {
      const resp = await fetch(
        `/api/system/services/${encodeURIComponent(svc.id)}/logs?tail=100`,
        { cache: "no-store" },
      );
      if (resp.ok) {
        const data = await resp.json();
        setServiceLogsText(data.logs || "(No recent container output)");
      } else {
        const errBody = await resp.json().catch(() => null);
        setServiceLogsText(
          `Failed to load logs (HTTP ${resp.status}): ${errBody?.detail || "Unknown error"}`,
        );
      }
    } catch {
      setServiceLogsText("Network error loading container logs.");
    } finally {
      setLoadingServiceLogs(false);
    }
  };

  const handleControlService = async (
    svc: BackgroundServiceInfo,
    action: "start" | "stop" | "restart",
  ) => {
    if (svc.critical && action === "stop") {
      const confirmed = window.confirm(
        `Stopping "${svc.name}" (${svc.container}) will interrupt NetDash UI or API access until restarted from the host CLI. Continue?`,
      );
      if (!confirmed) return;
    }

    setServiceActionBusy((prev) => ({ ...prev, [svc.id]: action }));
    try {
      const resp = await fetch(
        `/api/system/services/${encodeURIComponent(svc.id)}/${action}`,
        { method: "POST" },
      );
      const body = await resp.json().catch(() => null);
      if (resp.ok) {
        setServiceActionBanner({
          type: "ok",
          text:
            body?.message ||
            `${action.toUpperCase()} completed on ${svc.name} (${svc.container}).`,
        });
        await fetchServerInfo(false);
        const followUpMs = svc.id === "api" || svc.id === "proxy" ? 4000 : 2500;
        window.setTimeout(() => {
          fetchServerInfo(false);
          if (viewingLogsService?.id === svc.id) {
            handleViewServiceLogs(svc);
          }
        }, followUpMs);
      } else {
        setServiceActionBanner({
          type: "err",
          text:
            body?.detail ||
            `Failed to ${action} ${svc.name} (${svc.container}) (HTTP ${resp.status}).`,
        });
      }
    } catch {
      setServiceActionBanner({
        type: "err",
        text: `Network error executing ${action} on ${svc.name}.`,
      });
    } finally {
      setServiceActionBusy((prev) => ({ ...prev, [svc.id]: null }));
    }
  };

  const mergeLiveTrafficSamples = (
    prev: LiveTrafficSample[],
    incoming: LiveTrafficSample[],
  ): LiveTrafficSample[] => {
    if (!incoming || incoming.length === 0) return prev;
    const byTime = new Map<string, LiveTrafficSample>();
    for (const s of prev) {
      if (s && s.time) byTime.set(s.time, s);
    }
    for (const s of incoming) {
      if (s && s.time) byTime.set(s.time, s);
    }
    const merged = Array.from(byTime.values()).sort(
      (a, b) => new Date(a.time).getTime() - new Date(b.time).getTime(),
    );
    return merged.slice(-180);
  };

  const fetchOverview = async () => {
    try {
      const r = await fetch("/api/overview");
      if (r.ok) {
        const data: OverviewPayload = await r.json();
        setOverview(data);
        if (overviewGraphMode === "1d" || overviewHistorySeries.length === 0) {
          setOverviewHistorySeries(data.wan_series || []);
        }
        if (data.live_series && data.live_series.length > 0) {
          setLiveTrafficSeries((prev) => mergeLiveTrafficSamples(prev, data.live_series!));
        }
      }
    } catch {
      // ignore
    }
  };

  const fetchOverviewTraffic = async (
    rk: Exclude<OverviewRangeKey, "live">,
    showLoading = false,
  ) => {
    if (showLoading) setLoadingOverviewRange(true);
    try {
      const r = await fetch(`/api/overview/traffic?range=${encodeURIComponent(rk)}`);
      if (r.ok) {
        const data = await r.json();
        setOverviewHistorySeries(data.series || []);
      }
    } catch {
      // ignore
    } finally {
      if (showLoading) setLoadingOverviewRange(false);
    }
  };

  const fetchInterfaces = async () => {
    try {
      const r = await fetch("/api/snmp/interfaces");
      if (r.ok) {
        const data = await r.json();
        const list: SNMPInterface[] = data.interfaces || [];
        setInterfaces(list);
      }
    } catch {
      // ignore
    }
  };

  const fetchSnmpTelemetry = async (rk?: string) => {
    const targetRange = rk ?? snmpTelemetryRangeRef.current;
    try {
      const r = await fetch(`/api/snmp/telemetry?range=${encodeURIComponent(targetRange)}`);
      if (r.ok) {
        const data: SNMPTelemetryPayload = await r.json();
        setSnmpTelemetry(data);
      }
    } catch {
      // ignore
    }
  };

  const handlePollAllSnmp = async () => {
    setPollingAllSnmp(true);
    try {
      await fetch("/api/snmp/poll-all", { method: "POST" });
      setTimeout(() => {
        fetchSnmpTelemetry();
        fetchInterfaces();
        fetchAllDevices();
        setPollingAllSnmp(false);
      }, 4000);
    } catch {
      setPollingAllSnmp(false);
    }
  };

  const fetchUpsHistory = async (ip: string, rk = upsHistoryRange) => {
    if (!ip) return;
    try {
      const r = await fetch(
        `/api/devices/${encodeURIComponent(ip)}/ups-history?range=${encodeURIComponent(rk)}`,
      );
      if (r.ok) {
        const data = await r.json();
        setUpsHistory(data.series || []);
      }
    } catch {
      // ignore
    }
  };

  const fetchScanner = async () => {
    try {
      const r = await fetch("/api/scanner/hosts");
      if (r.ok) {
        const data = await r.json();
        const cfg: string[] = data.configured_subnets || [];
        setSubnets(cfg);
        if ((!selectedSubnet || !cfg.includes(selectedSubnet)) && cfg.length > 0) {
          setSelectedSubnet(cfg[0]);
        }
        setHosts(data.hosts || []);
      }
    } catch {
      // ignore
    }
  };

  const triggerScanNow = async () => {
    setScanningNow(true);
    try {
      await fetch("/api/scanner/trigger", { method: "POST" });
      setTimeout(() => {
        fetchScanner();
        fetchAllDevices();
        setScanningNow(false);
      }, 6000);
    } catch {
      setScanningNow(false);
    }
  };

  const fetchLogs = async () => {
    if (logsPaused) return;
    try {
      const params = new URLSearchParams({
        severity: logSeverity,
        source_ip: logSourceIp,
        search: logSearch,
        limit: "150",
      });
      const r = await fetch(`/api/logs?${params.toString()}`);
      if (r.ok) {
        const data = await r.json();
        setLogs(data.logs || []);
        setLogSources(data.sources || []);
      }
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    fetchConfig();
    fetchMibs();
    fetchOverview();
    fetchAllDevices();
    fetchInterfaces();
    fetchSnmpTelemetry(snmpTelemetryRange);
    fetchScanner();
    fetchLogs();
    fetchUpdateStatus(false);
    fetchServerInfo(false);

    const interval = setInterval(() => {
      fetchOverview();
      if (activeTab === "pulse" && overviewGraphMode !== "live") {
        fetchOverviewTraffic(overviewGraphMode, false);
      }
      if (activeTab === "devices") {
        fetchAllDevices();
        if (selectedDeviceRef) fetchDeviceDetail(selectedDeviceRef);
      }
      if (activeTab === "snmp") {
        fetchInterfaces();
        fetchSnmpTelemetry(snmpTelemetryRange);
      }
      if (activeTab === "scanner") fetchScanner();
      if (activeTab === "syslog") fetchLogs();
      if (activeTab === "settings") fetchServerInfo(false);
    }, 10000);

    const updateTimer = setInterval(() => {
      fetchUpdateStatus(true);
    }, 120000);

    return () => {
      clearInterval(interval);
      clearInterval(updateTimer);
    };
  }, [
    activeTab,
    overviewGraphMode,
    selectedDeviceRef,
    snmpTelemetryRange,
    logsPaused,
    logSeverity,
    logSourceIp,
    logSearch,
  ]);

  useEffect(() => {
    const onPopState = () => {
      const nextRoute = parseAppRoute(window.location.pathname);
      setActiveTab(nextRoute.tab);
      setNavMenuOpen(false);
      setSelectedDeviceRef(nextRoute.deviceRef);
      if (!nextRoute.deviceRef) {
        lastLoadedDeviceRef.current = null;
        setSelectedDeviceDetailIp(null);
        setDeviceDetail(null);
        setDeviceDetailError(null);
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    if (!selectedDeviceRef) {
      lastLoadedDeviceRef.current = null;
      setDeviceDetail(null);
      setSelectedDeviceDetailIp(null);
      setDeviceDetailError(null);
      return;
    }
    if (selectedDeviceRef === lastLoadedDeviceRef.current) {
      return;
    }
    lastLoadedDeviceRef.current = selectedDeviceRef;
    setDeviceDetail(null);
    setDeviceDetailError(null);
    const knownIpForRef = !/^\d+$/.test(selectedDeviceRef)
      ? selectedDeviceRef
      : (allDevices.find((d) => String(d.device_id) === selectedDeviceRef)?.ip_address ?? null);
    setSelectedDeviceDetailIp(knownIpForRef);
    setDetailIfIndex(0);
    setDetailTrendRange("24h");
    setUpsHistoryRange("24h");
    setUpsHistory([]);
    setUpsChartMode("battery_load");
    fetchDeviceDetail(selectedDeviceRef, true);
  }, [selectedDeviceRef]);

  useEffect(() => {
    if (overviewGraphMode !== "live") {
      fetchOverviewTraffic(overviewGraphMode, true);
    }
  }, [overviewGraphMode]);

  useEffect(() => {
    fetchSnmpTelemetry(snmpTelemetryRange);
  }, [snmpTelemetryRange]);

  useEffect(() => {
    if (activeTab === "settings") {
      fetchOverviewGraphOptions();
      fetchServerInfo(false);
    }
  }, [activeTab]);

  useEffect(() => {
    const devIp = deviceDetail?.ip_address || selectedDeviceDetailIp;
    if (
      devIp &&
      (deviceDetail?.device_profile_type === "ups" ||
        Boolean(deviceDetail?.snmp_system?.extended_metrics?.ups_telemetry))
    ) {
      fetchUpsHistory(devIp, upsHistoryRange);
    }
  }, [
    deviceDetail?.ip_address,
    selectedDeviceDetailIp,
    upsHistoryRange,
    deviceDetail?.device_profile_type,
  ]);

  useEffect(() => {
    const devIp = deviceDetail?.ip_address || selectedDeviceDetailIp;
    if (devIp && detailIfIndex >= 0) {
      fetchDetailTrend(devIp, detailIfIndex, detailTrendRange);
    }
  }, [deviceDetail?.ip_address, selectedDeviceDetailIp, detailIfIndex, detailTrendRange]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    if (activeTab === "devices" && selectedDeviceRef) {
      const host =
        deviceDetail?.resolved_identity?.hostname ||
        deviceDetail?.snmp_system?.sys_name ||
        deviceDetail?.ip_address ||
        selectedDeviceDetailIp ||
        `Device #${selectedDeviceRef}`;
      const idTag = deviceDetail?.device_id != null ? ` (#${deviceDetail.device_id})` : "";
      document.title = `NetDash — ${host}${idTag}`;
      return;
    }
    const titleMap: Record<TabKey, string> = {
      pulse: "NetDash — Overview",
      devices: "NetDash — Devices",
      snmp: "NetDash — SNMP Trends & Ports",
      scanner: "NetDash — Subnet Scanner Matrix",
      syslog: "NetDash — Live Syslog Stream",
      targets: "NetDash — Targets & SNMP",
      settings: "NetDash — Settings",
    };
    document.title = titleMap[activeTab];
  }, [
    activeTab,
    selectedDeviceRef,
    selectedDeviceDetailIp,
    deviceDetail?.device_id,
    deviceDetail?.ip_address,
    deviceDetail?.resolved_identity?.hostname,
    deviceDetail?.snmp_system?.sys_name,
  ]);

  useEffect(() => {
    if (activeTab !== "settings") {
      const dlg = updateDialogRef.current;
      if (dlg && dlg.open) dlg.close();
      return;
    }
    if (
      updateInfo?.update_available &&
      Boolean(updateInfo.remote_sha) &&
      updateInfo.remote_sha !== promptDismissedSha &&
      updateInfo.status !== "updating" &&
      !installingUpdate
    ) {
      openUpdateModal();
    }
  }, [
    activeTab,
    updateInfo?.update_available,
    updateInfo?.remote_sha,
    updateInfo?.status,
    promptDismissedSha,
    installingUpdate,
  ]);

  useEffect(() => {
    if (!navMenuOpen) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavMenuOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navMenuOpen]);

  useEffect(() => {
    initialBundleSrcRef.current = getCurrentBundleSrc();
    fetchUpdateProgress().then((p) => {
      if (!p) return;
      if (p.status === "updating") {
        sawUpdatingRef.current = true;
        setUpdateProgress(mergeMonotonicProgress(p));
        setInstallingUpdate(true);
        try {
          if (sessionStorage.getItem("netdash_update_minimized") === "1") {
            setUpdateMinimized(true);
          }
        } catch {
          // ignore
        }
      } else if (p.status === "completed") {
        fetch("/api/system/update/dismiss", { method: "POST" }).catch(() => {});
      }
    });
  }, []);

  useEffect(() => {
    if (!installingUpdate) return;

    let cancelled = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let finishTimer: ReturnType<typeof setInterval> | null = null;
    let lastBundleProbeAt = 0;

    const completeAndReload = async (completedState: UpdateProgressState) => {
      if (cancelled || finishingUpdateRef.current) return;
      finishingUpdateRef.current = true;
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      setUpdateProgress(mergeMonotonicProgress(completedState));

      let reloadTriggered = false;
      let checking = false;
      const completedAt = Date.now();

      const checkReadyAndReload = async (): Promise<boolean> => {
        if (cancelled || reloadTriggered || checking) return false;
        checking = true;
        try {
          let feReady = false;
          let apiReady = false;
          try {
            const [feResp, apiResp] = await Promise.all([
              fetchWithTimeout(`/?t=${Date.now()}`, { method: "HEAD", cache: "no-store" }, 4000),
              fetchWithTimeout(`/api/health?t=${Date.now()}`, { cache: "no-store" }, 4000),
            ]);
            feReady = feResp.ok && feResp.headers.get("X-NetDash-Updating-Page") !== "1";
            apiReady = apiResp.ok;
          } catch {
            return false;
          }

          const timedWait = Date.now() - completedAt > 12000;
          if (!((feReady && apiReady) || (apiReady && timedWait))) {
            return false;
          }

          reloadTriggered = true;
          if (finishTimer) {
            clearInterval(finishTimer);
            finishTimer = null;
          }
          try {
            await fetchWithTimeout("/api/system/update/dismiss", { method: "POST" }, 2000);
          } catch {
            // ignore
          }
          try {
            sessionStorage.removeItem("netdash_update_minimized");
          } catch {
            // ignore
          }
          setTimeout(() => {
            if (!cancelled) window.location.reload();
          }, 1000);
          return true;
        } finally {
          checking = false;
        }
      };

      const readyNow = await checkReadyAndReload();
      if (!readyNow && !cancelled && !reloadTriggered) {
        finishTimer = setInterval(checkReadyAndReload, 1000);
      }
    };

    const detectCompletedViaLiveBundle = async (): Promise<boolean> => {
      try {
        const [htmlResp, updResp] = await Promise.all([
          fetchWithTimeout(`/?t=${Date.now()}`, { cache: "no-store" }, 3000),
          fetchWithTimeout(`/api/system/update?t=${Date.now()}`, { cache: "no-store" }, 3000),
        ]);
        if (!htmlResp.ok || !updResp.ok) return false;
        if (htmlResp.headers.get("X-NetDash-Updating-Page") === "1") return false;
        const [htmlText, updData] = await Promise.all([
          htmlResp.text(),
          updResp.json().catch(() => null),
        ]);
        const matchMod =
          htmlText.match(/<script[^>]*type=["']module["'][^>]*src=["']([^"']+)["']/i) ||
          htmlText.match(/<script[^>]*src=["']([^"']+)["'][^>]*type=["']module["']/i);
        const liveSrc = matchMod ? matchMod[1] : "";
        return Boolean(
          liveSrc &&
            initialBundleSrcRef.current &&
            liveSrc !== initialBundleSrcRef.current &&
            updData &&
            !updData.update_available &&
            updData.status !== "updating",
        );
      } catch {
        return false;
      }
    };

    const buildSyntheticCompleted = (base: UpdateProgressState): UpdateProgressState => {
      const doneComps = (base.components || []).map((c) => ({
        ...c,
        status: "completed",
        pct: 100,
      }));
      return {
        ...base,
        status: "completed",
        overall_pct: 100,
        phase: "completed",
        message: "Update installed — reloading dashboard...",
        components: doneComps,
      };
    };

    const tick = async () => {
      if (cancelled || finishingUpdateRef.current) return;
      const p = await fetchUpdateProgress();
      if (cancelled || finishingUpdateRef.current || !p) return;

      const elapsedMs =
        updateStartTimeRef.current > 0 ? Date.now() - updateStartTimeRef.current : 0;

      if (p.status === "completed") {
        await completeAndReload(p);
        return;
      }

      if (p.status === "updating") {
        sawUpdatingRef.current = true;
        setUpdateProgress(mergeMonotonicProgress(p));
        if (
          elapsedMs > 8000 &&
          (p.phase === "queued" || p.phase === "legacy_build" || (p.overall_pct || 0) <= 25) &&
          Date.now() - lastBundleProbeAt >= 3000
        ) {
          lastBundleProbeAt = Date.now();
          if (await detectCompletedViaLiveBundle()) {
            await completeAndReload(buildSyntheticCompleted(p));
          }
        }
        return;
      }

      if (p.status === "idle") {
        if (
          installRequestInFlightRef.current ||
          (updateStartTimeRef.current > 0 && elapsedMs < 6000)
        ) {
          return;
        }
        if (sawUpdatingRef.current) {
          await completeAndReload(buildSyntheticCompleted(latestProgressRef.current || p));
          return;
        }
        try {
          const rUpd = await fetchWithTimeout(
            `/api/system/update?t=${Date.now()}`,
            { cache: "no-store" },
            2500,
          );
          if (rUpd.ok) {
            const upd = await rUpd.json();
            if (!upd.update_available) {
              await completeAndReload(buildSyntheticCompleted(latestProgressRef.current || p));
              return;
            }
            if (elapsedMs >= 20000 && upd.status !== "updating") {
              if (pollTimer) {
                clearInterval(pollTimer);
                pollTimer = null;
              }
              setInstallingUpdate(false);
              setUpdateProgress({
                ...(latestProgressRef.current || p),
                status: "error",
                phase: "error",
                message:
                  "Host updater did not start within 20s. Run 'sudo netdash-update' on the server or check 'systemctl status netdash-updater.path'.",
              });
            }
          }
        } catch {
          // wait for API if restarting
        }
        return;
      }

      if (p.status === "error") {
        setUpdateProgress(p);
        setInstallingUpdate(false);
        if (pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
      }
    };

    tick();
    pollTimer = setInterval(tick, 1200);
    return () => {
      cancelled = true;
      if (pollTimer) clearInterval(pollTimer);
      if (finishTimer) clearInterval(finishTimer);
    };
  }, [installingUpdate]);

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const wsUrl = `${proto}://${window.location.host}/ws/live`;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      ws = new WebSocket(wsUrl);
      ws.onopen = () => setWsConnected(true);
      ws.onclose = () => {
        setWsConnected(false);
        reconnectTimer = setTimeout(connect, 5000);
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "traffic_tick" && msg.data?.time) {
            setLiveTrafficSeries((prev) => mergeLiveTrafficSamples(prev, [msg.data]));
          }
          if (msg.type === "unifi_update") {
            fetchOverview();
            fetchAllDevices();
          }
          if (msg.type === "snmp_update") {
            fetchOverview();
            fetchInterfaces();
            fetchSnmpTelemetry();
            fetchAllDevices();
          }
          if (msg.type === "scanner_update") {
            setScanningNow(false);
            fetchScanner();
            fetchOverview();
            fetchAllDevices();
          }
          if (msg.type === "syslog_update") {
            fetchOverview();
          }
        } catch {
          // ignore
        }
      };
    };

    connect();
    return () => {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, []);

  useEffect(() => {
    if (wsConnected || activeTab !== "pulse" || overviewGraphMode !== "live") return;
    let cancelled = false;
    const pollLive = async () => {
      try {
        const r = await fetch("/api/overview/live?limit=120", { cache: "no-store" });
        if (!r.ok || cancelled) return;
        const data = await r.json();
        if (Array.isArray(data.live_series) && data.live_series.length > 0) {
          setLiveTrafficSeries((prev) => mergeLiveTrafficSamples(prev, data.live_series));
        }
      } catch {
        // ignore
      }
    };
    const timer = setInterval(pollLive, 2500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [wsConnected, activeTab, overviewGraphMode]);

  const isUpdateActive = Boolean(
    installingUpdate ||
      (updateProgress &&
        (updateProgress.status === "updating" ||
          updateProgress.status === "completed" ||
          updateProgress.status === "error")),
  );
  const showFullUpdatePage = isUpdateActive && !updateMinimized;

  const setMinimizedPreference = (val: boolean) => {
    setUpdateMinimized(val);
    try {
      sessionStorage.setItem("netdash_update_minimized", val ? "1" : "0");
    } catch {
      // ignore
    }
  };

  const deviceIdByIp: Record<string, number> = {};
  for (const d of allDevices) {
    if (d.device_id != null && d.ip_address) deviceIdByIp[d.ip_address] = d.device_id;
  }
  for (const s of overview?.snmp_devices || []) {
    if (s.device_id != null && s.device_ip && deviceIdByIp[s.device_ip] == null) {
      deviceIdByIp[s.device_ip] = s.device_id;
    }
  }
  for (const u of overview?.unifi_devices || []) {
    const uIp = u.lan_ip || u.ip_address;
    if (u.device_id != null && uIp && deviceIdByIp[uIp] == null) {
      deviceIdByIp[uIp] = u.device_id;
    }
  }
  for (const h of hosts) {
    if (h.device_id != null && h.ip_address && deviceIdByIp[h.ip_address] == null) {
      deviceIdByIp[h.ip_address] = h.device_id;
    }
  }
  for (const t of snmpTelemetry?.targets || []) {
    if (t.device_id != null && t.device_ip && deviceIdByIp[t.device_ip] == null) {
      deviceIdByIp[t.device_ip] = t.device_id;
    }
  }
  for (const intf of interfaces) {
    if (intf.device_id != null && intf.device_ip && deviceIdByIp[intf.device_ip] == null) {
      deviceIdByIp[intf.device_ip] = intf.device_id;
    }
  }
  for (const t of config?.snmp_targets || []) {
    if (t.device_id != null && t.device_ip && deviceIdByIp[t.device_ip] == null) {
      deviceIdByIp[t.device_ip] = t.device_id;
    }
  }
  for (const d of overviewGraphOptions) {
    if (d.device_id != null && d.device_ip && deviceIdByIp[d.device_ip] == null) {
      deviceIdByIp[d.device_ip] = d.device_id;
    }
  }
  if (deviceDetail?.device_id != null && deviceDetail.ip_address) {
    deviceIdByIp[deviceDetail.ip_address] = deviceDetail.device_id;
  }

  const getDeviceHref = (ip: string, explicitId?: number | null): string => {
    const id = explicitId ?? deviceIdByIp[ip] ?? null;
    return `/devices/${id != null ? String(id) : encodeURIComponent(ip)}`;
  };

  const resolvedDetailDeviceId =
    deviceDetail?.device_id ??
    (selectedDeviceDetailIp ? (deviceIdByIp[selectedDeviceDetailIp] ?? null) : null) ??
    (selectedDeviceRef && /^\d+$/.test(selectedDeviceRef) ? Number(selectedDeviceRef) : null);

  const activeDetailIp =
    deviceDetail?.ip_address ||
    selectedDeviceDetailIp ||
    (selectedDeviceRef && !/^\d+$/.test(selectedDeviceRef) ? selectedDeviceRef : null);

  const gw = overview?.gateway;
  const overviewGraphSource = overview?.overview_graph_source;
  const isCustomOverviewSource = overviewGraphSource?.mode === "custom";
  const ispConfig = overview?.isp_settings || config?.isp_settings;
  const ispCeilingMbps = Math.max(
    Number(ispConfig?.download_mbps ?? 1000),
    Number(ispConfig?.upload_mbps ?? 0),
  );
  const ispCeilingBps = ispCeilingMbps > 0 ? ispCeilingMbps * 1_000_000 : null;
  const overviewFixedMaxBps = overviewScaleMode === "isp" ? ispCeilingBps : null;
  const isLiveOverview = overviewGraphMode === "live" && liveTrafficSeries.length >= 2;
  const activeOverviewSeries =
    overviewGraphMode === "live"
      ? liveTrafficSeries.length >= 2
        ? liveTrafficSeries
        : overviewHistorySeries.length > 0
          ? overviewHistorySeries
          : overview?.wan_series || []
      : overviewHistorySeries;
  const latestLiveSample =
    liveTrafficSeries.length > 0 ? liveTrafficSeries[liveTrafficSeries.length - 1] : null;
  const latestWanSample =
    overview?.wan_series && overview.wan_series.length > 0
      ? overview.wan_series[overview.wan_series.length - 1]
      : null;
  const currentRxBps = isCustomOverviewSource
    ? (latestLiveSample?.rx_bps ?? latestWanSample?.rx_bps ?? null)
    : (latestLiveSample?.rx_bps ?? gw?.wan_rx_bps ?? latestWanSample?.rx_bps ?? null);
  const currentTxBps = isCustomOverviewSource
    ? (latestLiveSample?.tx_bps ?? latestWanSample?.tx_bps ?? null)
    : (latestLiveSample?.tx_bps ?? gw?.wan_tx_bps ?? latestWanSample?.tx_bps ?? null);
  const currentLatencyMs = isCustomOverviewSource
    ? null
    : (latestLiveSample?.latency_ms ?? gw?.wan_latency_ms ?? null);
  const filteredHosts = selectedSubnet
    ? hosts.filter((h) => h.subnet_cidr === selectedSubnet)
    : hosts;

  const handleToggleDeviceSort = (key: DeviceSortKey) => {
    if (deviceSortKey === key) {
      setDeviceSortDir((prev) => (prev === "asc" ? "desc" : "asc"));
    } else {
      setDeviceSortKey(key);
      setDeviceSortDir(key === "snmp" || key === "sources" ? "desc" : "asc");
    }
  };

  const filteredDevices = allDevices
    .filter((d) => {
      if (deviceFilter === "online" && !d.is_online) return false;
      if (deviceFilter === "snmp" && !d.sources.includes("SNMP")) return false;
      if (deviceSearch.trim()) {
        const q = deviceSearch.trim().toLowerCase();
        const qNum = q.replace(/^#/, "");
        return (
          (d.device_id != null &&
            (String(d.device_id).includes(qNum) || `#${d.device_id}`.includes(q))) ||
          d.ip_address.toLowerCase().includes(q) ||
          (d.hostname || "").toLowerCase().includes(q) ||
          (d.mac_address || "").toLowerCase().includes(q) ||
          (d.vendor || "").toLowerCase().includes(q) ||
          (d.sys_descr || "").toLowerCase().includes(q)
        );
      }
      return true;
    })
    .slice()
    .sort((a, b) => {
      // Keep Online devices pinned above Offline devices when viewing All
      if (deviceFilter === "all" && a.is_online !== b.is_online) {
        return a.is_online ? -1 : 1;
      }

      const dir = deviceSortDir === "asc" ? 1 : -1;
      const compareOptionalText = (va: string, vb: string) => {
        if (!va && !vb) return 0;
        if (!va) return 1;
        if (!vb) return -1;
        return va.localeCompare(vb, undefined, { numeric: true, sensitivity: "base" }) * dir;
      };

      let cmp = 0;
      switch (deviceSortKey) {
        case "device_id":
          cmp =
            ((a.device_id ?? Number.MAX_SAFE_INTEGER) -
              (b.device_id ?? Number.MAX_SAFE_INTEGER)) *
            dir;
          break;
        case "status":
          if (a.is_online !== b.is_online) {
            cmp = (a.is_online ? -1 : 1) * dir;
          } else {
            cmp = (ipv4ToSortNumber(a.ip_address) - ipv4ToSortNumber(b.ip_address)) * dir;
          }
          break;
        case "ip_address": {
          const na = ipv4ToSortNumber(a.ip_address);
          const nb = ipv4ToSortNumber(b.ip_address);
          cmp =
            na !== nb
              ? (na - nb) * dir
              : a.ip_address.localeCompare(b.ip_address, undefined, { numeric: true }) * dir;
          break;
        }
        case "hostname":
          cmp = compareOptionalText(
            (a.hostname || a.ip_address || "").trim(),
            (b.hostname || b.ip_address || "").trim(),
          );
          break;
        case "mac_address":
          cmp = compareOptionalText((a.mac_address || "").trim(), (b.mac_address || "").trim());
          break;
        case "vendor":
          cmp = compareOptionalText(
            (a.vendor || a.sys_descr || "").trim(),
            (b.vendor || b.sys_descr || "").trim(),
          );
          break;
        case "subnet": {
          cmp = compareOptionalText((a.subnet_cidr || "").trim(), (b.subnet_cidr || "").trim());
          if (cmp === 0) {
            cmp = ((a.vlan_id ?? 99999) - (b.vlan_id ?? 99999)) * dir;
          }
          break;
        }
        case "snmp": {
          const sa = a.sources.includes("SNMP") ? 1 : 0;
          const sb = b.sources.includes("SNMP") ? 1 : 0;
          cmp = (sa - sb) * dir;
          if (cmp === 0) cmp = ((a.interface_count || 0) - (b.interface_count || 0)) * dir;
          if (cmp === 0) {
            cmp = compareOptionalText(
              (a.snmp_profile_name || "").trim(),
              (b.snmp_profile_name || "").trim(),
            );
          }
          break;
        }
        case "sources": {
          cmp = (a.sources.length - b.sources.length) * dir;
          if (cmp === 0) {
            cmp = compareOptionalText(a.sources.join(","), b.sources.join(","));
          }
          break;
        }
      }

      if (cmp !== 0) return cmp;
      const ipCmp = ipv4ToSortNumber(a.ip_address) - ipv4ToSortNumber(b.ip_address);
      return deviceSortKey === "status" ? ipCmp * dir : ipCmp;
    });

  const primaryTabs: Array<{
    id: Exclude<TabKey, "settings">;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
    activeClass: string;
    inactiveClass: string;
    iconActiveClass: string;
    iconInactiveClass: string;
  }> = [
    {
      id: "pulse",
      label: "Overview",
      icon: Activity,
      activeClass: "border-emerald-500/60 bg-emerald-500/15 text-emerald-100 shadow-xs",
      inactiveClass:
        "border-slate-800 bg-[#111827] text-slate-400 hover:border-emerald-500/50 hover:bg-emerald-500/10 hover:text-emerald-200",
      iconActiveClass: "text-emerald-400",
      iconInactiveClass: "text-slate-500 group-hover:text-emerald-400",
    },
    {
      id: "devices",
      label: `Devices (${allDevices.length})`,
      icon: Monitor,
      activeClass: "border-sky-500/60 bg-sky-500/15 text-sky-100 shadow-xs",
      inactiveClass:
        "border-slate-800 bg-[#111827] text-slate-400 hover:border-sky-500/50 hover:bg-sky-500/10 hover:text-sky-200",
      iconActiveClass: "text-sky-400",
      iconInactiveClass: "text-slate-500 group-hover:text-sky-400",
    },
    {
      id: "snmp",
      label: "SNMP Trends & Ports",
      icon: Server,
      activeClass: "border-violet-500/60 bg-violet-500/15 text-violet-100 shadow-xs",
      inactiveClass:
        "border-slate-800 bg-[#111827] text-slate-400 hover:border-violet-500/50 hover:bg-violet-500/10 hover:text-violet-200",
      iconActiveClass: "text-violet-400",
      iconInactiveClass: "text-slate-500 group-hover:text-violet-400",
    },
    {
      id: "scanner",
      label: "Subnet Scanner Matrix",
      icon: Network,
      activeClass: "border-amber-500/60 bg-amber-500/15 text-amber-100 shadow-xs",
      inactiveClass:
        "border-slate-800 bg-[#111827] text-slate-400 hover:border-amber-500/50 hover:bg-amber-500/10 hover:text-amber-200",
      iconActiveClass: "text-amber-400",
      iconInactiveClass: "text-slate-500 group-hover:text-amber-400",
    },
    {
      id: "syslog",
      label: "Live Syslog Stream",
      icon: Terminal,
      activeClass: "border-rose-500/60 bg-rose-500/15 text-rose-100 shadow-xs",
      inactiveClass:
        "border-slate-800 bg-[#111827] text-slate-400 hover:border-rose-500/50 hover:bg-rose-500/10 hover:text-rose-200",
      iconActiveClass: "text-rose-400",
      iconInactiveClass: "text-slate-500 group-hover:text-rose-400",
    },
    {
      id: "targets",
      label: "Targets & SNMP",
      icon: KeyRound,
      activeClass: "border-cyan-500/60 bg-cyan-500/15 text-cyan-100 shadow-xs",
      inactiveClass:
        "border-slate-800 bg-[#111827] text-slate-400 hover:border-cyan-500/50 hover:bg-cyan-500/10 hover:text-cyan-200",
      iconActiveClass: "text-cyan-400",
      iconInactiveClass: "text-slate-500 group-hover:text-cyan-400",
    },
  ];

  const activeNavMeta =
    activeTab === "settings"
      ? { label: "Settings", icon: Settings, iconActiveClass: "text-indigo-400" }
      : primaryTabs.find((t) => t.id === activeTab) || primaryTabs[0];
  const ActiveNavIcon = activeNavMeta.icon;

  const handleGoHome = (e?: React.MouseEvent) => {
    if (e && isModifiedClick(e)) return;
    if (e) e.preventDefault();
    navigateToPath("/");
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const handleSelectNavTab = (
    tabId: Exclude<TabKey, "settings">,
    e?: React.MouseEvent,
  ) => {
    if (e && isModifiedClick(e)) return;
    if (e) e.preventDefault();
    navigateToTab(tabId);
  };

  const detailProfileType =
    deviceDetail?.device_profile_type ||
    (deviceDetail?.snmp_system?.extended_metrics?.ups_telemetry ? "ups" : "network");
  const detailUps = deviceDetail?.snmp_system?.extended_metrics?.ups_telemetry || null;
  const detailPrinterSupplies =
    deviceDetail?.snmp_system?.extended_metrics?.printer_supplies || [];
  const hasUpsWatts =
    detailUps?.output_watts != null || upsHistory.some((p) => p.output_watts != null);
  const activeUpsChartMode = upsChartMode === "watts" && !hasUpsWatts ? "battery_load" : upsChartMode;

  return (
    <div className="min-h-screen bg-[#090D16] text-slate-100">
      {/* Top Bar & Sub-Header Collapsible Navigation */}
      <header className="sticky top-0 z-30 border-b border-slate-800/90 bg-[#090D16]/95 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <a
              href="/"
              onClick={handleGoHome}
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-emerald-500/30 bg-emerald-500/10 text-emerald-400 transition hover:border-emerald-400/60 hover:bg-emerald-500/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/70"
              title="Go to Overview"
              aria-label="NetDash Overview"
            >
              <Activity className="h-5 w-5" />
            </a>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h1 className="text-base font-semibold tracking-tight text-white">
                  <a
                    href="/"
                    onClick={handleGoHome}
                    className="cursor-pointer rounded-sm text-left transition hover:text-emerald-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/70"
                    title="Return to Overview"
                  >
                    NetDash
                  </a>
                </h1>
                <span className="shrink-0 rounded border border-slate-700 bg-slate-800/80 px-1.5 py-0.5 font-mono-tabular text-[11px] text-slate-300">
                  {updateInfo?.current_version ||
                    serverInfo?.server?.git?.current_version ||
                    updateInfo?.current_sha ||
                    "v1.2"}
                </span>
              </div>
              <p className="hidden truncate text-xs text-slate-400 md:block">
                Network Observability • Devices • SNMP • Nmap • Syslog
              </p>
            </div>
          </div>

          {/* Right Status & Settings Controls (always locked to top-right corner) */}
          <div className="flex shrink-0 items-center gap-2">
            <a
              href="/settings"
              onClick={(e) => {
                if (isModifiedClick(e)) return;
                e.preventDefault();
                navigateToTab("settings");
              }}
              aria-current={activeTab === "settings" ? "page" : undefined}
              className={`group relative flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition ${
                updateInfo?.update_available && !installingUpdate
                  ? activeTab === "settings"
                    ? "border-amber-400/70 bg-amber-500/25 text-amber-200"
                    : "border-amber-500/50 bg-amber-500/15 text-amber-300 hover:border-amber-400/70 hover:bg-amber-500/25 hover:text-amber-200"
                  : activeTab === "settings"
                    ? "border-indigo-500/60 bg-indigo-500/15 text-indigo-100"
                    : "border-slate-800 bg-[#111827] text-slate-300 hover:border-indigo-500/50 hover:bg-indigo-500/10 hover:text-indigo-200"
              }`}
              title={
                updateInfo?.update_available && !installingUpdate
                  ? `Update available (${updateInfo.commits_behind} commit${
                      updateInfo.commits_behind === 1 ? "" : "s"
                    }) — Open Settings`
                  : "Server Information, Updates & Background Services"
              }
            >
              <Settings
                className={`h-3.5 w-3.5 transition-colors ${
                  updateInfo?.update_available && !installingUpdate
                    ? "text-amber-300"
                    : activeTab === "settings"
                      ? "text-indigo-400"
                      : "text-slate-400 group-hover:text-indigo-400"
                }`}
              />
              <span>Settings</span>
              {updateInfo?.update_available && !installingUpdate && (
                <DownloadCloud className="h-3.5 w-3.5 shrink-0 text-amber-300" />
              )}
            </a>

            <div className="flex items-center gap-1.5 rounded-md border border-slate-800 bg-[#111827] px-2.5 py-1.5 text-xs">
              <span
                className={`h-2 w-2 rounded-full ${
                  wsConnected ? "bg-emerald-400 animate-pulse" : "bg-amber-400"
                }`}
              />
              <span className="font-mono-tabular text-slate-300">
                {wsConnected ? "LIVE" : "POLL"}
              </span>
            </div>
          </div>
        </div>

        {/* Sub-Header Navigation Row: Separated bordered tabs on desktop, collapsible menu on narrower screens */}
        <nav
          aria-label="Primary Navigation"
          className="border-t border-slate-800/80 bg-[#0B101B]/95"
        >
          <div className="mx-auto max-w-7xl px-4 py-2 sm:px-6">
            {/* Desktop: Separated non-scrolling button bar */}
            <div className="hidden lg:flex lg:flex-wrap lg:items-center lg:gap-2">
              {primaryTabs.map((tab) => {
                const Icon = tab.icon;
                const active = activeTab === tab.id;
                return (
                  <a
                    key={tab.id}
                    href={TAB_PATHS[tab.id]}
                    onClick={(e) => handleSelectNavTab(tab.id, e)}
                    aria-current={active ? "page" : undefined}
                    className={`group flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-medium transition-all duration-150 ${
                      active ? tab.activeClass : tab.inactiveClass
                    }`}
                  >
                    <Icon
                      className={`h-3.5 w-3.5 shrink-0 transition-colors ${
                        active ? tab.iconActiveClass : tab.iconInactiveClass
                      }`}
                    />
                    <span>{tab.label}</span>
                  </a>
                );
              })}
            </div>

            {/* Resized / Compact Screens: Collapsible navigation trigger & grid */}
            <div className="lg:hidden">
              <button
                type="button"
                onClick={() => setNavMenuOpen((prev) => !prev)}
                aria-expanded={navMenuOpen}
                aria-controls="netdash-collapsible-nav"
                className="flex w-full items-center justify-between rounded-lg border border-slate-800 bg-[#111827] px-3 py-2 text-xs font-medium text-slate-200 transition hover:border-slate-700 hover:bg-slate-800/70"
              >
                <span className="flex items-center gap-2">
                  <ActiveNavIcon className={`h-3.5 w-3.5 shrink-0 ${activeNavMeta.iconActiveClass}`} />
                  <span>{activeNavMeta.label}</span>
                </span>
                <span className="flex items-center gap-1.5 text-slate-400">
                  <Menu className="h-3.5 w-3.5" />
                  <span>{navMenuOpen ? "Hide Menu" : "Navigation"}</span>
                  <ChevronDown
                    className={`h-3.5 w-3.5 transition-transform duration-200 ${
                      navMenuOpen ? "rotate-180" : ""
                    }`}
                  />
                </span>
              </button>

              <div
                id="netdash-collapsible-nav"
                aria-hidden={!navMenuOpen}
                className={`grid transition-all duration-200 ease-out ${
                  navMenuOpen ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"
                }`}
              >
                <div className="overflow-hidden">
                  <div className="grid grid-cols-1 gap-2 pt-2 sm:grid-cols-2 md:grid-cols-3">
                    {primaryTabs.map((tab) => {
                      const Icon = tab.icon;
                      const active = activeTab === tab.id;
                      return (
                        <a
                          key={tab.id}
                          href={TAB_PATHS[tab.id]}
                          tabIndex={navMenuOpen ? 0 : -1}
                          onClick={(e) => handleSelectNavTab(tab.id, e)}
                          aria-current={active ? "page" : undefined}
                          className={`group flex items-center gap-2 rounded-lg border px-3 py-2 text-left text-xs font-medium transition-all duration-150 ${
                            active ? tab.activeClass : tab.inactiveClass
                          }`}
                        >
                          <Icon
                            className={`h-3.5 w-3.5 shrink-0 transition-colors ${
                              active ? tab.iconActiveClass : tab.iconInactiveClass
                            }`}
                          />
                          <span className="truncate">{tab.label}</span>
                        </a>
                      );
                    })}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </nav>
      </header>

      {/* First-Time Onboarding Modal Prompt (Skippable) */}
      <dialog
        ref={onboardingDialogRef}
        className="m-auto w-full max-w-2xl rounded-xl border border-slate-700 bg-[#111827] p-0 text-slate-100 shadow-2xl"
      >
        <div className="flex items-center justify-between border-b border-slate-800 px-6 py-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-emerald-500/40 bg-emerald-500/10 text-emerald-400">
              <Network className="h-5 w-5" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-white">
                Welcome to NetDash — Quick Discovery Setup
              </h2>
              <p className="text-xs text-slate-400">
                Add initial subnets or SNMP hosts to monitor, or skip and configure them anytime in
                Targets & SNMP.
              </p>
            </div>
          </div>
          <button
            onClick={() => handleCompleteOnboarding(true)}
            className="rounded-md p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
            title="Skip setup for now"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-5 p-6">
          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-4">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-emerald-400">
              1. Subnets to Scan (Nmap + ARP Discovery)
            </h3>
            <p className="mt-1 text-xs text-slate-400">
              Add one or more IPv4 CIDRs (e.g. <code>10.1.3.0/24</code>) to sweep for active hosts.
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <input
                type="text"
                placeholder="Subnet CIDR (e.g. 10.1.3.0/24)"
                value={obSubnetInput}
                onChange={(e) => setObSubnetInput(e.target.value)}
                className="flex-1 rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 font-mono-tabular text-xs text-white"
              />
              <input
                type="text"
                placeholder="Label (e.g. VLAN 3 Servers)"
                value={obSubnetLabel}
                onChange={(e) => setObSubnetLabel(e.target.value)}
                className="flex-1 rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 text-xs text-white"
              />
              <button
                type="button"
                onClick={() => {
                  if (!obSubnetInput.trim()) return;
                  setObSubnetsList([
                    ...obSubnetsList,
                    { subnet_cidr: obSubnetInput.trim(), label: obSubnetLabel.trim() },
                  ]);
                  setObSubnetInput("");
                  setObSubnetLabel("");
                }}
                className="flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-700"
              >
                <Plus className="h-3.5 w-3.5" /> Add Another
              </button>
            </div>
            {obSubnetsList.length > 0 && (
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {obSubnetsList.map((s, idx) => (
                  <span
                    key={idx}
                    className="inline-flex items-center gap-1.5 rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 font-mono-tabular text-xs text-emerald-300"
                  >
                    {s.subnet_cidr} {s.label ? `(${s.label})` : ""}
                  </span>
                ))}
              </div>
            )}
          </div>

          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-4">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-sky-400">
              2. SNMP Hosts & Community String (Optional)
            </h3>
            <p className="mt-1 text-xs text-slate-400">
              Add switches, routers, or servers to poll via SNMP. You can add multiple SNMPv2c/v3
              credential profiles in the Settings tab.
            </p>
            <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
              <input
                type="text"
                placeholder="Host IP (e.g. 10.1.3.253)"
                value={obHostInput}
                onChange={(e) => setObHostInput(e.target.value)}
                className="rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 font-mono-tabular text-xs text-white"
              />
              <input
                type="text"
                placeholder="Hostname / Label"
                value={obHostLabel}
                onChange={(e) => setObHostLabel(e.target.value)}
                className="rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 text-xs text-white"
              />
              <div className="flex gap-2">
                <div className="relative w-full">
                  <input
                    type={showObSecret ? "text" : "password"}
                    placeholder="SNMP Community (public)"
                    value={obSnmpCommunity}
                    onChange={(e) => setObSnmpCommunity(e.target.value)}
                    className="w-full rounded-md border border-slate-700 bg-[#111827] py-1.5 pr-8 pl-3 font-mono-tabular text-xs text-white"
                  />
                  <button
                    type="button"
                    onClick={() => setShowObSecret(!showObSecret)}
                    className="absolute top-1.5 right-2 text-slate-400 hover:text-slate-200"
                    title={showObSecret ? "Hide secret" : "Reveal secret"}
                  >
                    {showObSecret ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  </button>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    if (!obHostInput.trim()) return;
                    setObHostsList([
                      ...obHostsList,
                      { device_ip: obHostInput.trim(), label: obHostLabel.trim() },
                    ]);
                    setObHostInput("");
                    setObHostLabel("");
                  }}
                  className="shrink-0 rounded-md border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-xs text-slate-200 hover:bg-slate-700"
                >
                  + Host
                </button>
              </div>
            </div>
            {obHostsList.length > 0 && (
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {obHostsList.map((h, idx) => (
                  <span
                    key={idx}
                    className="inline-flex items-center gap-1.5 rounded border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 font-mono-tabular text-xs text-sky-300"
                  >
                    {h.device_ip} {h.label ? `(${h.label})` : ""}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center justify-between border-t border-slate-800 bg-[#0D1320] px-6 py-3.5">
          <button
            type="button"
            onClick={() => handleCompleteOnboarding(true)}
            disabled={savingOnboarding}
            className="rounded-md border border-slate-700 px-3.5 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-800"
          >
            Skip for Now
          </button>
          <button
            type="button"
            onClick={() => handleCompleteOnboarding(false)}
            disabled={savingOnboarding}
            className="rounded-md bg-emerald-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {savingOnboarding ? "Saving..." : "Save & Start Discovery"}
          </button>
        </div>
      </dialog>

      {/* Native <dialog> Modal Prompt for Available GitHub Updates */}
      <dialog
        ref={updateDialogRef}
        onCancel={(e) => {
          e.preventDefault();
          if (!installingUpdate) closeUpdateModal();
        }}
        onClick={(e) => {
          if (e.target === updateDialogRef.current && !installingUpdate) {
            closeUpdateModal();
          }
        }}
        className="m-auto w-full max-w-lg rounded-xl border border-slate-700 bg-[#111827] p-0 text-slate-100 shadow-2xl"
      >
        <div className="flex items-center justify-between border-b border-slate-800 px-5 py-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-amber-500/40 bg-amber-500/10 text-amber-400">
              <DownloadCloud className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-white">
                Server Update Available on GitHub
              </h2>
              <p className="text-xs text-slate-400">
                {updateInfo?.commits_behind || 1} pending update
                {(updateInfo?.commits_behind || 1) === 1 ? "" : "s"} ready to download & install
              </p>
            </div>
          </div>
          {!installingUpdate && (
            <button
              onClick={closeUpdateModal}
              className="rounded-md p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>

        <div className="space-y-4 p-5">
          <div className="flex items-center justify-between rounded-lg border border-slate-800 bg-[#090D16] px-4 py-2.5 text-xs">
            <div>
              <span className="text-slate-400">Installed Version: </span>
              <span className="font-mono-tabular font-semibold text-slate-200">
                {updateInfo?.current_version ||
                  serverInfo?.server?.git?.current_version ||
                  updateInfo?.current_sha}
              </span>
            </div>
            <span className="text-slate-500">→</span>
            <div>
              <span className="text-slate-400">Latest Available: </span>
              <span className="font-mono-tabular font-semibold text-emerald-400">
                {updateInfo?.remote_version || updateInfo?.remote_sha}
              </span>
            </div>
          </div>

          {(() => {
            const pendingUpdates =
              updateInfo?.updates && updateInfo.updates.length > 0
                ? updateInfo.updates
                : updateInfo?.commits || [];
            if (pendingUpdates.length === 0) return null;
            return (
              <div>
                <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-emerald-400">
                  What's New — {pendingUpdates.length} Stacked Update
                  {pendingUpdates.length === 1 ? "" : "s"}
                </p>
                <div className="max-h-72 space-y-2.5 overflow-y-auto pr-1">
                  {pendingUpdates.map((upd) => {
                    const bullets =
                      upd.summary_bullets && upd.summary_bullets.length > 0
                        ? upd.summary_bullets
                        : [upd.subject];
                    return (
                      <div
                        key={upd.sha}
                        className="rounded-lg border border-emerald-500/25 bg-[#090D16] p-3"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800/80 pb-2 text-xs">
                          <div className="flex items-center gap-2">
                            <span className="rounded border border-emerald-500/40 bg-emerald-500/15 px-2 py-0.5 font-mono-tabular font-semibold text-emerald-300">
                              {upd.version || upd.sha}
                            </span>
                            <span className="font-mono-tabular text-[11px] text-slate-500">
                              ({upd.sha})
                            </span>
                          </div>
                          <span className="text-[11px] text-slate-500">{upd.date}</span>
                        </div>
                        <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-slate-200">
                          {bullets.map((b, bIdx) => (
                            <li key={bIdx} className="leading-relaxed">
                              {b}
                            </li>
                          ))}
                        </ul>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}

          {installingUpdate ? (
            <div className="flex items-center gap-3 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-300">
              <RefreshCw className="h-4 w-4 shrink-0 animate-spin" />
              <span>
                Downloading commits from GitHub and rebuilding containers on host... This page will
                reload automatically once complete.
              </span>
            </div>
          ) : (
            <p className="text-xs text-slate-400">
              Updates are never installed automatically. Click{" "}
              <strong className="text-slate-200">Download & Install Now</strong> to pull{" "}
              <code>origin/main</code> and rebuild the NetDash containers.
            </p>
          )}
        </div>

        <div className="flex items-center justify-end gap-2.5 border-t border-slate-800 bg-[#0D1320] px-5 py-3">
          {!installingUpdate && (
            <button
              onClick={closeUpdateModal}
              className="rounded-md border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-800"
            >
              Remind Me Later
            </button>
          )}
          <button
            onClick={handleInstallUpdate}
            disabled={installingUpdate}
            className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            <DownloadCloud className="h-3.5 w-3.5" />
            <span>{installingUpdate ? "Installing Update..." : "Download & Install Now"}</span>
          </button>
        </div>
      </dialog>

      {/* Persistent Live Update Banner when viewing Dashboard during Background Service Updates */}
      {isUpdateActive && updateMinimized && (
        <div className="border-b border-sky-500/30 bg-[#0D1424]">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-2.5 sm:px-6">
            <div className="flex min-w-0 flex-1 items-center gap-3">
              {updateProgress?.status === "completed" ? (
                <Check className="h-4 w-4 shrink-0 text-emerald-400" />
              ) : (
                <RefreshCw className="h-4 w-4 shrink-0 animate-spin text-sky-400" />
              )}
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-3 text-xs">
                  <span className="truncate font-medium text-slate-200">
                    {updateProgress?.message || "Application is updating in the background..."}
                  </span>
                  <span className="shrink-0 font-mono-tabular font-semibold text-emerald-400">
                    {updateProgress?.overall_pct ?? 1}%
                  </span>
                </div>
                <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                  <div
                    style={{
                      width: `${Math.max(0, Math.min(100, updateProgress?.overall_pct ?? 1))}%`,
                    }}
                    className="h-full rounded-full bg-gradient-to-r from-emerald-500 to-sky-400 transition-[width] duration-500 ease-out"
                  />
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setMinimizedPreference(false)}
                className="rounded-md border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs font-medium text-slate-200 hover:bg-slate-700"
              >
                View Full Progress
              </button>
              {updateProgress?.status !== "updating" && (
                <button
                  onClick={handleDismissUpdateProgress}
                  className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-white"
                  title="Dismiss"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Main Content Container */}
      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6">
        {showFullUpdatePage ? (
          <div className="mx-auto max-w-3xl space-y-6 py-6">
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-6 shadow-2xl">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="flex items-center gap-3.5">
                  <div
                    className={`flex h-11 w-11 items-center justify-center rounded-xl border ${
                      updateProgress?.status === "error"
                        ? "border-rose-500/40 bg-rose-500/10 text-rose-400"
                        : updateProgress?.status === "completed"
                          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-400"
                          : "border-emerald-500/40 bg-emerald-500/10 text-emerald-400"
                    }`}
                  >
                    {updateProgress?.status === "completed" ? (
                      <Check className="h-5 w-5" />
                    ) : updateProgress?.status === "error" ? (
                      <X className="h-5 w-5" />
                    ) : (
                      <RefreshCw className="h-5 w-5 animate-spin" />
                    )}
                  </div>
                  <div>
                    <h2 className="text-base font-semibold text-white">
                      {updateProgress?.status === "completed"
                        ? "Update Complete"
                        : updateProgress?.status === "error"
                          ? "Update Encountered an Error"
                          : "Application is Updating"}
                    </h2>
                    <p className="mt-0.5 text-xs text-slate-400">
                      {(updateProgress?.from_version || updateProgress?.from_sha) &&
                      (updateProgress?.target_version || updateProgress?.target_sha)
                        ? `Installing update ${updateProgress?.from_version || updateProgress?.from_sha} → ${
                            updateProgress?.target_version || updateProgress?.target_sha
                          }. You can continue browsing the frontend while backend services reload.`
                        : "Installing NetDash update and verifying container health."}
                    </p>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  {updateProgress?.status !== "error" && (
                    <button
                      onClick={() => setMinimizedPreference(true)}
                      className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-800"
                    >
                      Show Dashboard While Updating
                    </button>
                  )}
                  {(updateProgress?.status === "completed" ||
                    updateProgress?.status === "error") && (
                    <button
                      onClick={handleDismissUpdateProgress}
                      className="rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                    >
                      Return to Dashboard
                    </button>
                  )}
                </div>
              </div>

              {/* Overall Install Progress Bar */}
              <div className="mt-6 rounded-lg border border-slate-800 bg-[#090D16] p-4">
                <div className="mb-2 flex items-center justify-between text-xs">
                  <span className="font-medium text-slate-200">
                    {updateProgress?.message || "Preparing update..."}
                  </span>
                  <span className="font-mono-tabular text-sm font-semibold text-emerald-400">
                    {updateProgress?.overall_pct ?? 1}%
                  </span>
                </div>
                <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-800">
                  <div
                    style={{
                      width: `${Math.max(0, Math.min(100, updateProgress?.overall_pct ?? 1))}%`,
                    }}
                    className={`h-full rounded-full transition-[width] duration-500 ease-out ${
                      updateProgress?.status === "error"
                        ? "bg-rose-500"
                        : "bg-gradient-to-r from-emerald-500 to-sky-400"
                    }`}
                  />
                </div>
              </div>

              {/* Per-Component Progress Breakdown */}
              <div className="mt-5 space-y-2.5">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  System Component Progress
                </p>
                {(updateProgress?.components || []).map((comp) => {
                  const badgeCls =
                    comp.status === "completed"
                      ? "bg-emerald-500/15 text-emerald-400"
                      : comp.status === "running"
                        ? "bg-sky-500/15 text-sky-300"
                        : comp.status === "error"
                          ? "bg-rose-500/15 text-rose-400"
                          : "bg-slate-800 text-slate-400";
                  const barCls =
                    comp.status === "completed"
                      ? "bg-emerald-500"
                      : comp.status === "error"
                        ? "bg-rose-500"
                        : "bg-sky-400";
                  return (
                    <div
                      key={comp.id}
                      className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <span className="font-semibold text-white">{comp.name}</span>
                          <span
                            className={`rounded px-1.5 py-0.5 font-mono-tabular text-[10px] uppercase ${badgeCls}`}
                          >
                            {comp.status === "running" ? "updating" : comp.status}
                          </span>
                        </div>
                        <span className="font-mono-tabular font-semibold text-slate-200">
                          {comp.pct}%
                        </span>
                      </div>
                      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                        <div
                          style={{ width: `${Math.max(0, Math.min(100, comp.pct))}%` }}
                          className={`h-full rounded-full transition-[width] duration-400 ease-out ${barCls}`}
                        />
                      </div>
                      <div className="mt-1.5 text-[11px] text-slate-400">{comp.detail || "—"}</div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        ) : (
          <>
        {/* ==================== VIEW 1: OVERVIEW ==================== */}
        {activeTab === "pulse" && (
          <>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>Discovered Network Devices</span>
                  <Monitor className="h-4 w-4 text-emerald-400" />
                </div>
                <div className="mt-3 flex items-baseline justify-between font-mono-tabular">
                  <div>
                    <div className="text-lg font-semibold text-white">
                      {allDevices.filter((d) => d.is_online).length} Online
                    </div>
                    <div className="text-xs text-slate-400">
                      {allDevices.length} Total Discovered
                    </div>
                  </div>
                  <a
                    href="/devices"
                    onClick={(e) => {
                      if (isModifiedClick(e)) return;
                      e.preventDefault();
                      navigateToTab("devices");
                    }}
                    className="rounded border border-slate-700 bg-slate-800/80 px-2.5 py-1 font-sans text-xs text-emerald-400 hover:bg-slate-700"
                  >
                    View Devices →
                  </a>
                </div>
              </div>

              <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>Polled SNMP Hosts</span>
                  <Server className="h-4 w-4 text-sky-400" />
                </div>
                <div className="mt-3 flex items-baseline justify-between font-mono-tabular">
                  <div>
                    <div className="text-lg font-semibold text-white">
                      {overview?.snmp_devices?.filter((d) => d.is_reachable).length || 0} /{" "}
                      {config?.snmp_targets?.length || 0}
                    </div>
                    <div className="text-xs text-slate-400">Reachable SNMP Targets</div>
                  </div>
                  <a
                    href="/targets"
                    onClick={(e) => {
                      if (isModifiedClick(e)) return;
                      e.preventDefault();
                      navigateToTab("targets");
                    }}
                    className="rounded border border-slate-700 bg-slate-800/80 px-2 py-1 font-sans text-xs text-emerald-400 hover:bg-slate-700"
                  >
                    + Add Host
                  </a>
                </div>
              </div>

              <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>Searched Subnets</span>
                  <Network className="h-4 w-4 text-amber-400" />
                </div>
                <div className="mt-3 flex items-baseline justify-between font-mono-tabular">
                  <div>
                    <div className="text-lg font-semibold text-white">
                      {config?.subnets?.length || 0} Subnets
                    </div>
                    <div className="text-xs text-slate-400">
                      {overview?.scanner_summary?.online_hosts || 0} Active IPs
                    </div>
                  </div>
                  <a
                    href="/targets"
                    onClick={(e) => {
                      if (isModifiedClick(e)) return;
                      e.preventDefault();
                      navigateToTab("targets");
                    }}
                    className="rounded border border-slate-700 bg-slate-800/80 px-2 py-1 font-sans text-xs text-emerald-400 hover:bg-slate-700"
                  >
                    + Add Subnet
                  </a>
                </div>
              </div>

              <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>Syslog Stream (24h)</span>
                  <ShieldCheck className="h-4 w-4 text-emerald-400" />
                </div>
                <div className="mt-3 flex items-baseline justify-between font-mono-tabular">
                  <div>
                    <div className="text-lg font-semibold text-white">
                      {overview?.syslog_summary?.total_24h || 0}
                    </div>
                    <div className="text-xs text-slate-400">Ingested Events</div>
                  </div>
                  <div className="text-right text-xs">
                    <div className="text-rose-400">
                      {overview?.syslog_summary?.errors_24h || 0} Errors
                    </div>
                    <div className="text-amber-400">
                      {overview?.syslog_summary?.warnings_24h || 0} Warnings
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* Network Throughput Overview Graph with Live Streaming, ISP Scaling & Multi-Range Axis */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-sm font-semibold text-white">
                      {overviewGraphMode === "live"
                        ? "Network Traffic Overview (Live Stream)"
                        : overviewGraphMode === "1h"
                          ? "Network Traffic Overview (Last 1 Hour)"
                          : overviewGraphMode === "12h"
                            ? "Network Traffic Overview (Last 12 Hours)"
                            : overviewGraphMode === "1d"
                              ? "Network Traffic Overview (Last 24 Hours)"
                              : overviewGraphMode === "7d"
                                ? "Network Traffic Overview (Last 7 Days)"
                                : "Network Traffic Overview (Last 30 Days)"}
                    </h3>
                    {overviewGraphMode === "live" && (
                      <span className="inline-flex items-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0.5 font-mono-tabular text-[10px] font-semibold text-emerald-400">
                        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                        LIVE
                      </span>
                    )}
                    {loadingOverviewRange && overviewGraphMode !== "live" && (
                      <RefreshCw className="h-3.5 w-3.5 animate-spin text-sky-400" />
                    )}
                  </div>
                  <p className="text-xs text-slate-400">
                    {isCustomOverviewSource
                      ? `Custom Source: ${overviewGraphSource?.summary || "Selected Interfaces"} — ${
                          overviewGraphMode === "live"
                            ? "real-time 2s traffic stream"
                            : "historical throughput"
                        }`
                      : gw
                        ? `UniFi Gateway (${gw.gateway_name} • ${gw.wan_ip || "WAN"}) ${
                            overviewGraphMode === "live"
                              ? "real-time 2s traffic stream"
                              : "historical throughput"
                          }`
                        : overviewGraphMode === "live"
                          ? "Aggregated real-time 2s traffic across all polled SNMP interfaces"
                          : "Aggregated inbound & outbound traffic across all polled SNMP network interfaces"}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-3">
                  {currentRxBps !== null && currentTxBps !== null && (
                    <div className="flex items-center gap-3 font-mono-tabular text-xs">
                      <span className="text-emerald-400">RX: {formatBps(currentRxBps)}</span>
                      <span className="text-sky-400">TX: {formatBps(currentTxBps)}</span>
                      {currentLatencyMs !== null && currentLatencyMs > 0 && (
                        <span className="text-slate-400">
                          Latency: {currentLatencyMs.toFixed(1)} ms
                        </span>
                      )}
                    </div>
                  )}
                  <a
                    href="/settings"
                    onClick={(e) => {
                      if (isModifiedClick(e)) return;
                      e.preventDefault();
                      navigateToTab("settings");
                    }}
                    className={`rounded border px-2 py-0.5 font-mono-tabular text-[11px] transition ${
                      isCustomOverviewSource
                        ? "border-sky-500/40 bg-sky-500/10 text-sky-300 hover:border-sky-400/60"
                        : "border-slate-800 bg-[#090D16] text-slate-400 hover:border-slate-700 hover:text-slate-200"
                    }`}
                    title="Choose devices & interfaces displayed on Overview Dashboard in Settings"
                  >
                    {isCustomOverviewSource
                      ? `Source: ${overviewGraphSource?.summary || "Custom"}`
                      : "Source: Auto"}
                  </a>
                  <button
                    type="button"
                    onClick={toggleOverviewScaleMode}
                    className={`rounded border px-2 py-0.5 font-mono-tabular text-[11px] transition ${
                      overviewScaleMode === "auto"
                        ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:border-emerald-400/60"
                        : "border-sky-500/40 bg-sky-500/10 text-sky-300 hover:border-sky-400/60"
                    }`}
                    title="Toggle between dynamic auto-scaled Y-axis (highlights live traffic fluctuations) and fixed ISP ceiling scaling"
                  >
                    {overviewScaleMode === "auto" || !ispCeilingBps
                      ? "Scale: Auto"
                      : `Scale: ISP (${ispCeilingMbps} Mbps)`}
                  </button>
                  <a
                    href="/settings"
                    onClick={(e) => {
                      if (isModifiedClick(e)) return;
                      e.preventDefault();
                      navigateToTab("settings");
                    }}
                    className="rounded border border-slate-800 bg-[#090D16] px-2 py-0.5 font-mono-tabular text-[11px] text-slate-400 transition hover:border-slate-700 hover:text-slate-200"
                    title="Configure ISP Download/Upload Speed in Settings"
                  >
                    {ispCeilingBps
                      ? `${ispConfig?.isp_name ? `${ispConfig.isp_name} • ` : "ISP: "}${
                          ispConfig?.download_mbps ?? 1000
                        }/${ispConfig?.upload_mbps ?? 1000} Mbps`
                      : "ISP: Auto"}
                  </a>
                  <div className="inline-flex items-center rounded-md border border-slate-800 bg-[#090D16] p-0.5 text-[11px] font-medium">
                    {(
                      [
                        { id: "live", label: "Live" },
                        { id: "1h", label: "1H" },
                        { id: "12h", label: "12H" },
                        { id: "1d", label: "1D" },
                        { id: "7d", label: "7D" },
                        { id: "1m", label: "1M" },
                      ] as Array<{ id: OverviewRangeKey; label: string }>
                    ).map((opt) => (
                      <button
                        key={opt.id}
                        type="button"
                        onClick={() => setOverviewGraphMode(opt.id)}
                        className={`rounded px-2 py-0.5 transition ${
                          overviewGraphMode === opt.id
                            ? opt.id === "live"
                              ? "bg-emerald-500/20 text-emerald-300"
                              : "bg-slate-800 text-white"
                            : "text-slate-400 hover:text-slate-200"
                        }`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
              <DualAreaSparkChart
                showYAxis={true}
                fixedMaxBps={overviewFixedMaxBps}
                liveMode={isLiveOverview}
                liveWindowMs={120_000}
                windowMs={
                  isLiveOverview
                    ? null
                    : rangeKeyToWindowMs(overviewGraphMode === "live" ? "1d" : overviewGraphMode)
                }
                points={activeOverviewSeries.map((pt) => ({
                  label: pt.time,
                  inVal: pt.rx_bps,
                  outVal: pt.tx_bps,
                }))}
              />
            </div>

            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-white">
                    Polled SNMP Targets ({overview?.snmp_devices?.length || 0})
                  </h3>
                  <a
                    href="/targets"
                    onClick={(e) => {
                      if (isModifiedClick(e)) return;
                      e.preventDefault();
                      navigateToTab("targets");
                    }}
                    className="flex items-center gap-1 rounded border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs text-slate-200 hover:bg-slate-700"
                  >
                    <Plus className="h-3.5 w-3.5" /> Manage SNMP Hosts & Secrets
                  </a>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="border-b border-slate-800 text-slate-400">
                        <th className="pb-2 font-medium">Target IP</th>
                        <th className="pb-2 font-medium">Name / Label</th>
                        <th className="pb-2 font-medium">Interfaces</th>
                        <th className="pb-2 font-medium">Poll Time</th>
                        <th className="pb-2 text-right font-medium">State</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                      {(overview?.snmp_devices || []).length === 0 ? (
                        <tr>
                          <td colSpan={5} className="py-6 text-center font-sans text-slate-500">
                            No SNMP targets configured yet. Click{" "}
                            <a
                              href="/targets"
                              onClick={(e) => {
                                if (isModifiedClick(e)) return;
                                e.preventDefault();
                                navigateToTab("targets");
                              }}
                              className="text-emerald-400 underline"
                            >
                              Manage SNMP Hosts & Secrets
                            </a>{" "}
                            to add devices.
                          </td>
                        </tr>
                      ) : (
                        (overview?.snmp_devices || []).map((s) => {
                          const stateDesc = s.is_reachable
                            ? `Reachable — SNMP responded in ${s.last_poll_duration_ms.toFixed(0)} ms${
                                s.last_polled
                                  ? ` (${new Date(s.last_polled).toLocaleTimeString()})`
                                  : ""
                              }`
                            : `Timeout — SNMP host did not respond to the last poll${
                                s.last_polled
                                  ? ` (${new Date(s.last_polled).toLocaleTimeString()})`
                                  : ""
                              }`;
                          return (
                            <tr
                              key={s.device_ip}
                              onClick={() => openDevicePage(s.device_ip, s.device_id)}
                              className="cursor-pointer hover:bg-slate-800/40"
                            >
                              <td className="py-2.5 font-semibold text-emerald-400 underline">
                                <a
                                  href={getDeviceHref(s.device_ip, s.device_id)}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    if (isModifiedClick(e)) return;
                                    e.preventDefault();
                                    openDevicePage(s.device_ip, s.device_id);
                                  }}
                                  className="hover:text-emerald-300"
                                >
                                  {s.device_ip}
                                </a>
                              </td>
                              <td className="py-2.5 font-sans text-slate-300">
                                {s.sys_name || "—"}
                              </td>
                              <td className="py-2.5 text-slate-300">{s.interface_count}</td>
                              <td className="py-2.5 text-slate-400">
                                {s.last_poll_duration_ms.toFixed(0)} ms
                              </td>
                              <td className="py-2.5 text-right">
                                <div className="group/state relative inline-flex items-center justify-end">
                                  <span
                                    tabIndex={0}
                                    role="img"
                                    aria-label={stateDesc}
                                    title={stateDesc}
                                    onClick={(e) => e.stopPropagation()}
                                    className={`inline-flex h-6 w-6 items-center justify-center rounded-full border transition ${
                                      s.is_reachable
                                        ? "border-emerald-500/35 bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25"
                                        : "border-rose-500/35 bg-rose-500/15 text-rose-400 hover:bg-rose-500/25"
                                    }`}
                                  >
                                    {s.is_reachable ? (
                                      <Check className="h-3.5 w-3.5 stroke-[2.5]" aria-hidden="true" />
                                    ) : (
                                      <X className="h-3.5 w-3.5 stroke-[2.5]" aria-hidden="true" />
                                    )}
                                  </span>
                                  <span
                                    role="tooltip"
                                    className="pointer-events-none invisible absolute right-full top-1/2 z-20 mr-2 -translate-y-1/2 whitespace-nowrap rounded-md border border-slate-700 bg-[#090D16] px-2.5 py-1 font-sans text-[11px] font-normal text-slate-200 opacity-0 shadow-lg transition-opacity duration-150 group-hover/state:visible group-hover/state:opacity-100 group-focus-within/state:visible group-focus-within/state:opacity-100"
                                  >
                                    {stateDesc}
                                  </span>
                                </div>
                              </td>
                            </tr>
                          );
                        })
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-white">
                    Recently Discovered Devices ({allDevices.slice(0, 8).length})
                  </h3>
                  <a
                    href="/devices"
                    onClick={(e) => {
                      if (isModifiedClick(e)) return;
                      e.preventDefault();
                      navigateToTab("devices");
                    }}
                    className="rounded border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs text-slate-200 hover:bg-slate-700"
                  >
                    Open Full Devices Page →
                  </a>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="border-b border-slate-800 text-slate-400">
                        <th className="pb-2 font-medium">IPv4 Address</th>
                        <th className="pb-2 font-medium">Hostname</th>
                        <th className="pb-2 font-medium">MAC / Vendor</th>
                        <th className="pb-2 text-right font-medium">Sources</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                      {allDevices.length === 0 ? (
                        <tr>
                          <td colSpan={4} className="py-6 text-center font-sans text-slate-500">
                            No devices discovered yet. Add a subnet in{" "}
                            <a
                              href="/targets"
                              onClick={(e) => {
                                if (isModifiedClick(e)) return;
                                e.preventDefault();
                                navigateToTab("targets");
                              }}
                              className="text-emerald-400 underline"
                            >
                              Targets & SNMP
                            </a>{" "}
                            to scan your network.
                          </td>
                        </tr>
                      ) : (
                        allDevices.slice(0, 8).map((d) => (
                          <tr
                            key={d.ip_address}
                            onClick={() => openDevicePage(d.ip_address, d.device_id)}
                            className="cursor-pointer hover:bg-slate-800/40"
                          >
                            <td className="py-2.5 font-semibold text-emerald-400 underline">
                              <a
                                href={getDeviceHref(d.ip_address, d.device_id)}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  if (isModifiedClick(e)) return;
                                  e.preventDefault();
                                  openDevicePage(d.ip_address, d.device_id);
                                }}
                                className="hover:text-emerald-300"
                              >
                                {d.ip_address}
                              </a>
                            </td>
                            <td className="py-2.5 font-sans text-slate-200">{d.hostname}</td>
                            <td className="py-2.5 font-sans text-slate-400">
                              {d.vendor || d.mac_address || "—"}
                            </td>
                            <td className="py-2.5 text-right">
                              <div className="flex justify-end gap-1">
                                {d.sources.map((src) => (
                                  <span
                                    key={src}
                                    className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300"
                                  >
                                    {src}
                                  </span>
                                ))}
                              </div>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </>
        )}

        {/* ==================== VIEW 2: DEVICES PAGE & PER-DEVICE DETAIL PAGE ==================== */}
        {activeTab === "devices" && !selectedDeviceRef && (
          <div className="space-y-5">
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-[#111827] p-4">
              <div className="flex flex-wrap items-center gap-3">
                <div className="relative">
                  <Search className="pointer-events-none absolute top-2 left-2.5 h-3.5 w-3.5 text-slate-500" />
                  <input
                    type="text"
                    placeholder="Filter by ID, IP, hostname, MAC, vendor, OS..."
                    value={deviceSearch}
                    onChange={(e) => setDeviceSearch(e.target.value)}
                    className="w-72 rounded-md border border-slate-700 bg-[#090D16] py-1.5 pr-3 pl-8 text-xs text-white placeholder:text-slate-500"
                  />
                </div>

                <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-[#090D16] p-1">
                  {[
                    { id: "all", label: `All (${allDevices.length})` },
                    {
                      id: "online",
                      label: `Online (${allDevices.filter((d) => d.is_online).length})`,
                    },
                    {
                      id: "snmp",
                      label: `SNMP Monitored (${
                        allDevices.filter((d) => d.sources.includes("SNMP")).length
                      })`,
                    },
                  ].map((f) => (
                    <button
                      key={f.id}
                      onClick={() => setDeviceFilter(f.id as "all" | "online" | "snmp")}
                      className={`rounded px-2.5 py-1 text-xs font-medium transition ${
                        deviceFilter === f.id
                          ? "bg-emerald-600 text-white"
                          : "text-slate-400 hover:text-slate-200"
                      }`}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <button
                  onClick={handleRematchAllMibs}
                  disabled={rematchingMibs}
                  className="flex items-center gap-1.5 rounded-md border border-sky-500/40 bg-sky-500/15 px-3 py-1.5 text-xs font-medium text-sky-300 hover:bg-sky-500/25 disabled:opacity-50"
                  title="Automatically match all previously discovered devices with vendor MIBs and probe SNMP"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${rematchingMibs ? "animate-spin" : ""}`} />
                  <span>
                    {rematchingMibs
                      ? "Auto-Matching & Probing MIBs..."
                      : "Auto-Match & Poll Devices with MIBs"}
                  </span>
                </button>
                <button
                  onClick={triggerScanNow}
                  disabled={scanningNow}
                  className="flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-700 disabled:opacity-50"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${scanningNow ? "animate-spin" : ""}`} />
                  <span>{scanningNow ? "Scanning..." : "Rescan Subnets"}</span>
                </button>
                <a
                  href="/targets"
                  onClick={(e) => {
                    if (isModifiedClick(e)) return;
                    e.preventDefault();
                    navigateToTab("targets");
                  }}
                  className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                >
                  <Plus className="h-3.5 w-3.5" /> Add Host or Subnet
                </a>
              </div>
            </div>

            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 className="text-sm font-semibold text-white">
                    Discovered & Monitored Network Devices ({filteredDevices.length})
                  </h2>
                  <p className="text-xs text-slate-400">
                    Click any column header to sort, or click any device row to open its full
                    telemetry webpage.
                  </p>
                </div>
                {deviceFilter === "all" && (
                  <span className="rounded border border-emerald-500/25 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-400">
                    Online devices pinned first
                  </span>
                )}
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      {(
                        [
                          { key: "device_id", label: "ID", alignRight: false },
                          { key: "status", label: "Status", alignRight: false },
                          { key: "ip_address", label: "IPv4 Address", alignRight: false },
                          { key: "hostname", label: "Hostname / sysName", alignRight: false },
                          { key: "mac_address", label: "MAC Address", alignRight: false },
                          { key: "vendor", label: "Vendor / Hardware", alignRight: false },
                          { key: "subnet", label: "Subnet / VLAN", alignRight: false },
                          { key: "snmp", label: "SNMP / Interfaces", alignRight: false },
                          { key: "sources", label: "Telemetry Sources", alignRight: true },
                        ] as Array<{ key: DeviceSortKey; label: string; alignRight: boolean }>
                      ).map((col) => {
                        const isActive = deviceSortKey === col.key;
                        return (
                          <th
                            key={col.key}
                            aria-sort={
                              isActive
                                ? deviceSortDir === "asc"
                                  ? "ascending"
                                  : "descending"
                                : "none"
                            }
                            className={`pb-2.5 font-medium ${col.alignRight ? "text-right" : ""}`}
                          >
                            <button
                              type="button"
                              onClick={() => handleToggleDeviceSort(col.key)}
                              className={`group inline-flex items-center gap-1 rounded px-1 py-0.5 transition ${
                                col.alignRight ? "ml-auto justify-end" : ""
                              } ${
                                isActive
                                  ? "text-emerald-400"
                                  : "text-slate-400 hover:text-slate-200"
                              }`}
                            >
                              <span>{col.label}</span>
                              {isActive ? (
                                deviceSortDir === "asc" ? (
                                  <ArrowUp className="h-3 w-3 shrink-0" />
                                ) : (
                                  <ArrowDown className="h-3 w-3 shrink-0" />
                                )
                              ) : (
                                <ArrowUpDown className="h-3 w-3 shrink-0 opacity-45 transition-opacity group-hover:opacity-100" />
                              )}
                            </button>
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                    {filteredDevices.length === 0 ? (
                      <tr>
                        <td colSpan={9} className="py-10 text-center font-sans text-slate-500">
                          No devices match your current filter. Add a subnet or SNMP target in{" "}
                          <a
                            href="/targets"
                            onClick={(e) => {
                              if (isModifiedClick(e)) return;
                              e.preventDefault();
                              navigateToTab("targets");
                            }}
                            className="text-emerald-400 underline"
                          >
                            Targets & SNMP
                          </a>
                          .
                        </td>
                      </tr>
                    ) : (
                      filteredDevices.map((d) => (
                        <tr
                          key={d.ip_address}
                          onClick={() => openDevicePage(d.ip_address, d.device_id)}
                          className="cursor-pointer transition hover:bg-slate-800/50"
                        >
                          <td className="py-3 text-slate-400">
                            {d.device_id != null ? `#${d.device_id}` : "—"}
                          </td>
                          <td className="py-3">
                            <span
                              className={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[11px] font-medium ${
                                d.is_online
                                  ? "bg-emerald-500/15 text-emerald-400"
                                  : "bg-slate-800 text-slate-400"
                              }`}
                            >
                              <span
                                className={`h-1.5 w-1.5 rounded-full ${
                                  d.is_online ? "bg-emerald-400" : "bg-slate-500"
                                }`}
                              />
                              {d.is_online ? "ONLINE" : "OFFLINE"}
                            </span>
                          </td>
                          <td className="py-3 font-semibold text-emerald-400 underline">
                            <a
                              href={getDeviceHref(d.ip_address, d.device_id)}
                              onClick={(e) => {
                                e.stopPropagation();
                                if (isModifiedClick(e)) return;
                                e.preventDefault();
                                openDevicePage(d.ip_address, d.device_id);
                              }}
                              className="hover:text-emerald-300"
                            >
                              {d.ip_address}
                            </a>
                          </td>
                          <td className="py-3 font-sans font-medium text-white">
                            {d.hostname || d.ip_address}
                          </td>
                          <td className="py-3 text-slate-300">{d.mac_address || "—"}</td>
                          <td className="max-w-xs py-3 font-sans text-slate-300">
                            <div className="truncate">{d.vendor || d.sys_descr || "—"}</div>
                            {d.matched_mibs && d.matched_mibs.length > 0 && (
                              <div className="mt-1 flex flex-wrap gap-1">
                                {d.matched_mibs.slice(0, 3).map((mib) => (
                                  <span
                                    key={mib}
                                    className="rounded border border-sky-500/25 bg-sky-500/10 px-1.5 py-0.2 font-mono-tabular text-[10px] text-sky-300"
                                  >
                                    {mib}
                                  </span>
                                ))}
                                {d.matched_mibs.length > 3 && (
                                  <span className="font-mono-tabular text-[10px] text-slate-500">
                                    +{d.matched_mibs.length - 3}
                                  </span>
                                )}
                              </div>
                            )}
                          </td>
                          <td className="py-3 text-slate-400">
                            {d.subnet_cidr || "—"}
                            {d.vlan_id ? ` (VLAN ${d.vlan_id})` : ""}
                          </td>
                          <td className="py-3 text-slate-300">
                            {d.sources.includes("SNMP") ? (
                              <span>
                                {d.interface_count} ifs •{" "}
                                {d.snmp_profile_name || "SNMP"}
                              </span>
                            ) : (
                              <span className="font-sans text-slate-500">Not Polled</span>
                            )}
                          </td>
                          <td className="py-3 text-right">
                            <div className="flex justify-end gap-1">
                              {d.sources.map((src) => (
                                <span
                                  key={src}
                                  className="rounded border border-slate-700 bg-slate-800/90 px-1.5 py-0.5 text-[10px] text-slate-300"
                                >
                                  {src}
                                </span>
                              ))}
                            </div>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* ==================== PER-DEVICE DETAILED TELEMETRY PAGE ==================== */}
        {activeTab === "devices" && selectedDeviceRef && (
          <div className="space-y-6">
            {/* Top Bar with Back Button & Quick SNMP Actions */}
            <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-800 bg-[#111827] p-4">
              <div className="flex items-center gap-3">
                <a
                  href="/devices"
                  onClick={(e) => {
                    if (isModifiedClick(e)) return;
                    e.preventDefault();
                    navigateToTab("devices");
                  }}
                  className="flex items-center gap-1.5 rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-800"
                >
                  <ArrowLeft className="h-3.5 w-3.5" /> Back to Devices
                </a>
                <div>
                  <div className="flex flex-wrap items-center gap-2.5">
                    {resolvedDetailDeviceId != null && (
                      <span className="rounded border border-sky-500/35 bg-sky-500/15 px-2 py-0.5 font-mono-tabular text-[11px] font-semibold text-sky-300">
                        ID #{resolvedDetailDeviceId}
                      </span>
                    )}
                    <h2 className="font-mono-tabular text-base font-bold text-white">
                      {activeDetailIp || `Device #${selectedDeviceRef}`}
                    </h2>
                    <span className="text-sm font-medium text-slate-300">
                      {deviceDetail?.resolved_identity?.hostname ||
                        deviceDetail?.snmp_system?.sys_name ||
                        deviceDetail?.snmp_config?.label ||
                        deviceDetail?.discovered_host?.hostname ||
                        ""}
                    </span>
                    <span
                      className={`rounded px-2 py-0.5 font-mono-tabular text-[11px] ${
                        deviceDetail?.discovered_host?.is_online ||
                        deviceDetail?.snmp_system?.is_reachable ||
                        deviceDetail?.unifi_device?.status === "online"
                          ? "bg-emerald-500/15 text-emerald-400"
                          : "bg-slate-800 text-slate-400"
                      }`}
                    >
                      {deviceDetail?.discovered_host?.is_online ||
                      deviceDetail?.snmp_system?.is_reachable ||
                      deviceDetail?.unifi_device?.status === "online"
                        ? "ONLINE"
                        : "OFFLINE"}
                    </span>
                    {detailProfileType === "ups" ? (
                      <span className="inline-flex items-center gap-1 rounded border border-amber-500/30 bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-300">
                        <BatteryCharging className="h-3 w-3" /> UPS / Power Backup
                      </span>
                    ) : detailProfileType === "nas" ? (
                      <span className="inline-flex items-center gap-1 rounded border border-violet-500/30 bg-violet-500/15 px-2 py-0.5 text-[11px] font-medium text-violet-300">
                        <HardDrive className="h-3 w-3" /> NAS / Storage
                      </span>
                    ) : detailProfileType === "printer" ? (
                      <span className="inline-flex items-center gap-1 rounded border border-fuchsia-500/30 bg-fuchsia-500/15 px-2 py-0.5 text-[11px] font-medium text-fuchsia-300">
                        <Printer className="h-3 w-3" /> Network Printer
                      </span>
                    ) : detailProfileType === "server" ? (
                      <span className="inline-flex items-center gap-1 rounded border border-sky-500/30 bg-sky-500/15 px-2 py-0.5 text-[11px] font-medium text-sky-300">
                        <Server className="h-3 w-3" /> Server / Host
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 rounded border border-cyan-500/30 bg-cyan-500/15 px-2 py-0.5 text-[11px] font-medium text-cyan-300">
                        <Network className="h-3 w-3" /> Network Device
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-slate-400">
                    {detailProfileType === "ups"
                      ? "UPS Power, Battery Health, Runtime Telemetry, Management Interface & Syslogs"
                      : detailProfileType === "printer"
                        ? "Printer Toner & Supply Levels, SNMP Counters, and Syslogs"
                        : "Complete Polled Telemetry — Discovery, SNMP System & Interfaces, and Syslogs"}
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-2">
                {!deviceDetail?.snmp_config ? (
                  <button
                    disabled={!activeDetailIp}
                    onClick={() =>
                      activeDetailIp &&
                      handleQuickEnableSnmpOnDevice(
                        activeDetailIp,
                        deviceDetail?.discovered_host?.hostname || activeDetailIp,
                      )
                    }
                    className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-sky-500 disabled:opacity-50"
                  >
                    <Plus className="h-3.5 w-3.5" /> Enable SNMP Polling on This Device
                  </button>
                ) : (
                  <button
                    onClick={() => activeDetailIp && handlePollSingleDeviceNow(activeDetailIp)}
                    disabled={pollingDeviceNow || !activeDetailIp}
                    className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
                  >
                    <RefreshCw className={`h-3.5 w-3.5 ${pollingDeviceNow ? "animate-spin" : ""}`} />
                    <span>{pollingDeviceNow ? "Polling SNMP..." : "Poll SNMP Now"}</span>
                  </button>
                )}
              </div>
            </div>

            {loadingDeviceDetail && !deviceDetail ? (
              <div className="rounded-xl border border-slate-800 bg-[#111827] p-10 text-center text-xs text-slate-400">
                Loading polled telemetry for {activeDetailIp || `Device #${selectedDeviceRef}`}...
              </div>
            ) : deviceDetailError && !deviceDetail ? (
              <div className="rounded-xl border border-rose-500/35 bg-[#111827] p-8 text-center">
                <div className="text-sm font-semibold text-rose-300">{deviceDetailError}</div>
                <p className="mt-1 text-xs text-slate-400">
                  The requested device ID or IP address could not be loaded.
                </p>
                <a
                  href="/devices"
                  onClick={(e) => {
                    if (isModifiedClick(e)) return;
                    e.preventDefault();
                    navigateToTab("devices");
                  }}
                  className="mt-4 inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-800"
                >
                  <ArrowLeft className="h-3.5 w-3.5" /> Return to Devices
                </a>
              </div>
            ) : (
              <>
                {/* Identity & SNMP System Summary Cards */}
                <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                  {/* Card 1: Network Discovery & L2/L3 Identity */}
                  <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                    <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
                      <Network className="h-4 w-4 text-emerald-400" />
                      Network Identity & ARP / Nmap Discovery
                    </h3>
                    {(() => {
                      const ri = deviceDetail?.resolved_identity;
                      const hostname =
                        ri?.hostname ||
                        deviceDetail?.discovered_host?.hostname ||
                        deviceDetail?.snmp_system?.sys_name ||
                        "—";
                      const macAddr =
                        ri?.mac_address ||
                        deviceDetail?.discovered_host?.mac_address ||
                        deviceDetail?.snmp_system?.mac_address ||
                        deviceDetail?.unifi_client?.mac ||
                        deviceDetail?.unifi_device?.mac ||
                        "—";
                      const vendorName =
                        ri?.vendor ||
                        deviceDetail?.discovered_host?.vendor ||
                        deviceDetail?.unifi_client?.oui_vendor ||
                        deviceDetail?.snmp_system?.vendor_name ||
                        "—";
                      const subnetCidr =
                        ri?.subnet_cidr ||
                        deviceDetail?.discovered_host?.subnet_cidr ||
                        deviceDetail?.snmp_system?.subnet_cidr;
                      const vlanId =
                        ri?.vlan_id ??
                        deviceDetail?.discovered_host?.vlan_id ??
                        deviceDetail?.unifi_client?.vlan_id;
                      const discMethod =
                        ri?.discovery_source ||
                        deviceDetail?.discovered_host?.discovery_source ||
                        "manual / snmp";
                      const firstSeen =
                        ri?.first_seen ||
                        deviceDetail?.discovered_host?.first_seen ||
                        deviceDetail?.snmp_system?.first_polled ||
                        deviceDetail?.snmp_config?.created_at;
                      const lastSeen =
                        ri?.last_seen ||
                        deviceDetail?.discovered_host?.last_seen ||
                        deviceDetail?.snmp_system?.last_polled ||
                        deviceDetail?.unifi_device?.last_seen;

                      return (
                        <dl className="grid grid-cols-2 gap-y-3 text-xs">
                          <div>
                            <dt className="text-slate-400">Device ID</dt>
                            <dd className="mt-0.5 font-mono-tabular font-semibold text-sky-300">
                              {resolvedDetailDeviceId != null ? `#${resolvedDetailDeviceId}` : "—"}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">Device Webpage</dt>
                            <dd className="mt-0.5 font-mono-tabular text-emerald-400">
                              /devices/{resolvedDetailDeviceId ?? selectedDeviceRef}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">IPv4 Address</dt>
                            <dd className="mt-0.5 font-mono-tabular font-semibold text-white">
                              {activeDetailIp || "—"}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">Hostname</dt>
                            <dd className="mt-0.5 text-slate-200">{hostname}</dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">MAC Address</dt>
                            <dd className="mt-0.5 font-mono-tabular text-slate-200">{macAddr}</dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">OUI Hardware Vendor</dt>
                            <dd className="mt-0.5 text-slate-200">{vendorName}</dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">Subnet CIDR & VLAN</dt>
                            <dd className="mt-0.5 font-mono-tabular text-slate-200">
                              {subnetCidr || "—"}
                              {vlanId != null ? ` (VLAN ${vlanId})` : ""}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">Discovery Method</dt>
                            <dd className="mt-0.5 font-mono-tabular text-slate-300">
                              {discMethod}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">First Seen</dt>
                            <dd className="mt-0.5 font-mono-tabular text-slate-400">
                              {firstSeen ? new Date(firstSeen).toLocaleString() : "—"}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">Last Seen</dt>
                            <dd className="mt-0.5 font-mono-tabular text-slate-300">
                              {lastSeen ? new Date(lastSeen).toLocaleString() : "—"}
                            </dd>
                          </div>
                        </dl>
                      );
                    })()}
                  </div>

                  {/* Card 2: Polled SNMP System & Matched Vendor MIB Information */}
                  <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                    <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
                      <Server className="h-4 w-4 text-sky-400" />
                      Matched Vendor Profile & Polled SNMP System Info
                    </h3>
                    {deviceDetail?.snmp_system ? (
                      <div className="space-y-3 text-xs">
                        {/* Matched Vendor & Active MIBs Banner */}
                        <div className="rounded-lg border border-sky-500/30 bg-sky-500/10 p-3">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div>
                              <span className="text-[11px] uppercase tracking-wider text-sky-300">
                                Matched Vendor Profile
                              </span>
                              <div className="text-sm font-semibold text-white">
                                {deviceDetail.snmp_system.vendor_name || "Standard SNMP Device"}{" "}
                                {deviceDetail.snmp_system.device_category
                                  ? `— ${deviceDetail.snmp_system.device_category}`
                                  : ""}
                              </div>
                            </div>
                            <span className="font-mono-tabular text-[11px] text-slate-300">
                              OID: {deviceDetail.snmp_system.sys_object_id || "SNMPv2-MIB"}
                            </span>
                          </div>
                          {deviceDetail.snmp_system.matched_mibs &&
                            deviceDetail.snmp_system.matched_mibs.length > 0 && (() => {
                              const snmpSys = deviceDetail?.snmp_system;
                              const mibs = snmpSys?.matched_mibs || [];
                              const respList = snmpSys?.extended_metrics?.responding_mibs || [];
                              const respSet = new Set(respList);
                              const hasRespTracking = respList.length > 0;
                              return (
                                <>
                                  <div className="mt-2 flex flex-wrap gap-1.5">
                                    {mibs.map((mib) => {
                                      const isResp = !hasRespTracking || respSet.has(mib);
                                      return (
                                        <span
                                          key={mib}
                                          title={
                                            isResp
                                              ? "Actively responding on this device"
                                              : "Profile MIB — not exposed by this device's SNMP agent"
                                          }
                                          className={`rounded border px-2 py-0.5 font-mono-tabular text-[10px] ${
                                            hasRespTracking
                                              ? isResp
                                                ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                                                : "border-slate-700/80 bg-[#090D16] text-slate-500"
                                              : "border-sky-400/30 bg-[#090D16] text-sky-300"
                                          }`}
                                        >
                                          {hasRespTracking && isResp ? `✓ ${mib}` : mib}
                                        </span>
                                      );
                                    })}
                                  </div>
                                  {hasRespTracking && (
                                    <div className="mt-2 font-mono-tabular text-[11px] text-slate-400">
                                      {respList.length} of {mibs.length} MIBs responding •{" "}
                                      {snmpSys?.interface_count ?? 0} interfaces polled
                                    </div>
                                  )}
                                </>
                              );
                            })()}
                        </div>

                        <dl className="grid grid-cols-2 gap-y-2.5">
                          <div>
                            <dt className="text-slate-400">SNMP sysName</dt>
                            <dd className="mt-0.5 font-semibold text-white">
                              {deviceDetail.snmp_system.sys_name || "—"}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">Assigned SNMP Profile</dt>
                            <dd className="mt-0.5 text-sky-400">
                              {deviceDetail.snmp_config?.profile_name || "Default"} (
                              {deviceDetail.snmp_config?.profile_version || "v2c"} :{" "}
                              {deviceDetail.snmp_config?.profile_port || 161})
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">Hardware Model</dt>
                            <dd className="mt-0.5 font-mono-tabular text-slate-200">
                              {deviceDetail.snmp_system.hardware_model ||
                                deviceDetail.unifi_device?.model ||
                                "—"}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">Serial Number & Firmware</dt>
                            <dd className="mt-0.5 font-mono-tabular text-slate-200">
                              {(() => {
                                const serial =
                                  deviceDetail?.snmp_system?.serial_number ||
                                  deviceDetail?.unifi_device?.serial_number ||
                                  "";
                                const fw =
                                  deviceDetail?.snmp_system?.firmware_version ||
                                  deviceDetail?.unifi_device?.firmware_version ||
                                  "";
                                if (serial && fw) return `${serial} (${fw})`;
                                if (serial) return serial;
                                if (fw) return `Firmware ${fw}`;
                                return "—";
                              })()}
                            </dd>
                          </div>
                          <div className="col-span-2">
                            <dt className="text-slate-400">System Description (sysDescr)</dt>
                            <dd className="mt-0.5 rounded border border-slate-800 bg-[#090D16] p-2 font-mono-tabular text-[11px] text-slate-300">
                              {deviceDetail.snmp_system.sys_descr ||
                                "Waiting for SNMP response from device..."}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">System Uptime (sysUpTime)</dt>
                            <dd className="mt-0.5 font-mono-tabular text-slate-200">
                              {deviceDetail.snmp_system.sys_uptime_ticks &&
                              deviceDetail.snmp_system.sys_uptime_ticks > 0
                                ? formatUptime(
                                    Math.floor(deviceDetail.snmp_system.sys_uptime_ticks / 100),
                                  )
                                : deviceDetail.unifi_device?.uptime_sec
                                  ? formatUptime(deviceDetail.unifi_device.uptime_sec)
                                  : "—"}
                            </dd>
                          </div>
                          {detailProfileType === "ups" && detailUps ? (
                            <div>
                              <dt className="text-slate-400">UPS Power & Battery Summary</dt>
                              <dd className="mt-0.5 font-mono-tabular text-slate-200">
                                {detailUps.output_status || "Unknown"} • Batt{" "}
                                {detailUps.battery_charge_pct != null
                                  ? `${detailUps.battery_charge_pct}%`
                                  : "—"}{" "}
                                • Load{" "}
                                {detailUps.output_load_pct != null
                                  ? `${detailUps.output_load_pct}%`
                                  : "—"}
                                {detailUps.battery_temp_c != null
                                  ? ` • ${detailUps.battery_temp_c}°C`
                                  : ""}
                              </dd>
                            </div>
                          ) : (
                            <div>
                              <dt className="text-slate-400">CPU, RAM & Temperature</dt>
                              <dd className="mt-0.5 font-mono-tabular text-slate-200">
                                CPU {deviceDetail.snmp_system.cpu_usage_pct?.toFixed(1) || "0.0"}% •
                                RAM {deviceDetail.snmp_system.mem_usage_pct?.toFixed(1) || "0.0"}%
                                {deviceDetail.snmp_system.temp_celsius != null
                                  ? ` • ${deviceDetail.snmp_system.temp_celsius}°C`
                                  : ""}
                              </dd>
                            </div>
                          )}
                          <div>
                            <dt className="text-slate-400">Location & Contact</dt>
                            <dd className="mt-0.5 text-slate-300">
                              {deviceDetail.snmp_system.sys_location || "—"}
                              {deviceDetail.snmp_system.sys_contact
                                ? ` (${deviceDetail.snmp_system.sys_contact})`
                                : ""}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-400">Last Polled & Latency</dt>
                            <dd className="mt-0.5 font-mono-tabular text-slate-300">
                              {deviceDetail.snmp_system.last_poll_duration_ms.toFixed(0)} ms •{" "}
                              {deviceDetail.snmp_system.last_polled
                                ? new Date(
                                    deviceDetail.snmp_system.last_polled,
                                  ).toLocaleTimeString()
                                : "—"}
                            </dd>
                          </div>
                        </dl>
                      </div>
                    ) : (
                      <div className="space-y-3 text-xs">
                        {deviceDetail?.matched_mib_profile && (
                          <div className="rounded-lg border border-sky-500/30 bg-sky-500/10 p-3">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <div>
                                <span className="text-[11px] uppercase tracking-wider text-sky-300">
                                  Auto-Matched Vendor MIB Profile
                                </span>
                                <div className="text-sm font-semibold text-white">
                                  {deviceDetail.matched_mib_profile.vendor_name ||
                                    deviceDetail.matched_mib_profile.vendor ||
                                    "Standard SNMP Device"}{" "}
                                  — {deviceDetail.matched_mib_profile.category}
                                </div>
                              </div>
                            </div>
                            {deviceDetail.matched_mib_profile.mibs.length > 0 && (
                              <div className="mt-2 flex flex-wrap gap-1.5">
                                {deviceDetail.matched_mib_profile.mibs.map((mib) => (
                                  <span
                                    key={mib}
                                    className="rounded border border-sky-400/30 bg-[#090D16] px-2 py-0.5 font-mono-tabular text-[10px] text-sky-300"
                                  >
                                    {mib}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                        <div className="py-4 text-center text-xs text-slate-400">
                          SNMP telemetry has not been collected from this host yet. Click{" "}
                          <strong className="text-white">Enable SNMP Polling on This Device</strong>{" "}
                          above to poll its matched MIBs.
                        </div>
                      </div>
                    )}
                  </div>
                </div>

                {/* UPS Power, Battery & Runtime Dashboard */}
                {(detailProfileType === "ups" || detailUps !== null) && (
                  <div className="space-y-5 rounded-xl border border-slate-800 bg-[#111827] p-5">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div className="flex flex-wrap items-center gap-2.5">
                        <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
                          <BatteryCharging className="h-4 w-4 text-amber-400" />
                          UPS Power, Battery &amp; Runtime Telemetry
                        </h3>
                        {detailUps?.output_status && (
                          <span
                            className={`rounded border px-2 py-0.5 font-mono-tabular text-[11px] font-medium ${
                              detailUps.on_battery
                                ? "border-rose-500/40 bg-rose-500/15 text-rose-300"
                                : detailUps.output_status.toLowerCase().includes("on line")
                                  ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                                  : "border-amber-500/40 bg-amber-500/15 text-amber-300"
                            }`}
                          >
                            {detailUps.output_status}
                          </span>
                        )}
                        {detailUps?.battery_status && (
                          <span
                            className={`rounded border px-2 py-0.5 font-mono-tabular text-[11px] font-medium ${
                              detailUps.battery_status.toLowerCase() === "normal"
                                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                                : detailUps.battery_status.toLowerCase() === "low" ||
                                    detailUps.battery_status.toLowerCase() === "fault" ||
                                    detailUps.battery_status.toLowerCase() === "depleted"
                                  ? "border-rose-500/40 bg-rose-500/15 text-rose-300"
                                  : "border-slate-700 bg-slate-800 text-slate-300"
                            }`}
                          >
                            Battery: {detailUps.battery_status}
                          </span>
                        )}
                        {detailUps?.sku && (
                          <span className="rounded border border-slate-700 bg-[#090D16] px-2 py-0.5 font-mono-tabular text-[11px] text-slate-300">
                            {detailUps.sku}
                          </span>
                        )}
                      </div>

                      <div className="flex flex-wrap items-center gap-2">
                        {detailUps?.on_battery && (
                          <span className="inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/15 px-2.5 py-1 text-[11px] font-semibold text-amber-300">
                            <Zap className="h-3.5 w-3.5" /> Running on Battery Power
                          </span>
                        )}
                        {detailUps?.battery_replace_needed && (
                          <span className="inline-flex items-center gap-1 rounded border border-rose-500/40 bg-rose-500/15 px-2.5 py-1 text-[11px] font-semibold text-rose-300">
                            <AlertTriangle className="h-3.5 w-3.5" /> Replace Battery Needed
                          </span>
                        )}
                        {(detailUps?.alarms_present ?? 0) > 0 && (
                          <span className="inline-flex items-center gap-1 rounded border border-rose-500/40 bg-rose-500/15 px-2.5 py-1 text-[11px] font-semibold text-rose-300">
                            <AlertTriangle className="h-3.5 w-3.5" />
                            {detailUps?.alarms_present} Active Alarm
                            {(detailUps?.alarms_present ?? 0) === 1 ? "" : "s"}
                          </span>
                        )}
                      </div>
                    </div>

                    {!detailUps ? (
                      <div className="rounded-lg border border-slate-800 bg-[#090D16] p-5 text-center text-xs text-slate-400">
                        Waiting for UPS SNMP MIB telemetry (<code>PowerNet-UPS-MIB</code> /{" "}
                        <code>UPS-MIB</code>). Click{" "}
                        <strong className="text-white">Poll SNMP Now</strong> above to collect power
                        and battery readings.
                      </div>
                    ) : (
                      <>
                        {/* 4 UPS KPI Progress Cards */}
                        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
                          {/* Card 1: Battery Charge & Health */}
                          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-4">
                            <div className="flex items-center justify-between text-xs text-slate-400">
                              <span className="flex items-center gap-1.5">
                                <Battery className="h-3.5 w-3.5 text-emerald-400" />
                                Battery Charge &amp; Health
                              </span>
                              {detailUps.battery_temp_c != null && (
                                <span className="inline-flex items-center gap-1 font-mono-tabular text-[11px] text-slate-300">
                                  <Thermometer className="h-3 w-3 text-amber-400" />
                                  {detailUps.battery_temp_c.toFixed(1)}°C
                                </span>
                              )}
                            </div>
                            <div className="mt-2 font-mono-tabular text-2xl font-bold text-white">
                              {detailUps.battery_charge_pct != null
                                ? `${detailUps.battery_charge_pct.toFixed(0)}%`
                                : "—"}
                            </div>
                            <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                              <div
                                className={`h-full rounded-full transition-all ${
                                  (detailUps.battery_charge_pct ?? 0) >= 80
                                    ? "bg-emerald-500"
                                    : (detailUps.battery_charge_pct ?? 0) >= 40
                                      ? "bg-amber-500"
                                      : "bg-rose-500"
                                }`}
                                style={{
                                  width: `${Math.min(100, Math.max(0, detailUps.battery_charge_pct ?? 0))}%`,
                                }}
                              />
                            </div>
                            <div className="mt-3 space-y-1 text-[11px] text-slate-400">
                              <div className="flex justify-between">
                                <span>Bus Voltage</span>
                                <span className="font-mono-tabular text-slate-200">
                                  {detailUps.battery_voltage_v != null
                                    ? `${detailUps.battery_voltage_v} VDC`
                                    : "—"}
                                  {detailUps.battery_nominal_voltage_v != null
                                    ? ` / ${detailUps.battery_nominal_voltage_v}V nom`
                                    : ""}
                                </span>
                              </div>
                              {(detailUps.external_battery_packs != null ||
                                detailUps.bad_battery_packs != null) && (
                                <div className="flex justify-between">
                                  <span>Battery Packs</span>
                                  <span className="font-mono-tabular text-slate-200">
                                    {detailUps.external_battery_packs ?? 0} ext •{" "}
                                    <span
                                      className={
                                        (detailUps.bad_battery_packs ?? 0) > 0
                                          ? "text-rose-400"
                                          : "text-emerald-400"
                                      }
                                    >
                                      {detailUps.bad_battery_packs ?? 0} bad
                                    </span>
                                  </span>
                                </div>
                              )}
                            </div>
                          </div>

                          {/* Card 2: Estimated Runtime */}
                          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-4">
                            <div className="flex items-center justify-between text-xs text-slate-400">
                              <span className="flex items-center gap-1.5">
                                <Clock className="h-3.5 w-3.5 text-sky-400" />
                                Estimated Runtime
                              </span>
                            </div>
                            <div className="mt-2 font-mono-tabular text-2xl font-bold text-white">
                              {formatDurationCompact(detailUps.runtime_remaining_sec)}
                            </div>
                            <div className="mt-3 space-y-1 text-[11px] text-slate-400">
                              <div className="flex justify-between">
                                <span>Time on Battery</span>
                                <span
                                  className={`font-mono-tabular ${
                                    (detailUps.time_on_battery_sec ?? 0) > 0
                                      ? "font-semibold text-amber-300"
                                      : "text-slate-200"
                                  }`}
                                >
                                  {detailUps.time_on_battery_sec && detailUps.time_on_battery_sec > 0
                                    ? formatDurationCompact(detailUps.time_on_battery_sec)
                                    : "0s (On Utility AC)"}
                                </span>
                              </div>
                              <div className="flex justify-between">
                                <span>Battery State</span>
                                <span className="font-mono-tabular text-slate-200">
                                  {detailUps.battery_status || "—"}
                                </span>
                              </div>
                              <div className="flex justify-between">
                                <span>Last Replaced</span>
                                <span className="font-mono-tabular text-slate-200">
                                  {detailUps.last_replace_date || "—"}
                                </span>
                              </div>
                            </div>
                          </div>

                          {/* Card 3: Output Load & Power */}
                          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-4">
                            <div className="flex items-center justify-between text-xs text-slate-400">
                              <span className="flex items-center gap-1.5">
                                <Zap className="h-3.5 w-3.5 text-amber-400" />
                                Output Load &amp; Power
                              </span>
                              {detailUps.output_watts != null && (
                                <span className="font-mono-tabular text-[11px] font-semibold text-amber-300">
                                  {detailUps.output_watts} W
                                </span>
                              )}
                            </div>
                            <div className="mt-2 font-mono-tabular text-2xl font-bold text-white">
                              {detailUps.output_load_pct != null
                                ? `${detailUps.output_load_pct.toFixed(0)}%`
                                : "—"}
                            </div>
                            <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                              <div
                                className={`h-full rounded-full transition-all ${
                                  (detailUps.output_load_pct ?? 0) < 70
                                    ? "bg-emerald-500"
                                    : (detailUps.output_load_pct ?? 0) < 85
                                      ? "bg-amber-500"
                                      : "bg-rose-500"
                                }`}
                                style={{
                                  width: `${Math.min(100, Math.max(0, detailUps.output_load_pct ?? 0))}%`,
                                }}
                              />
                            </div>
                            <div className="mt-3 space-y-1 text-[11px] text-slate-400">
                              <div className="flex justify-between">
                                <span>Real / Apparent</span>
                                <span className="font-mono-tabular text-slate-200">
                                  {detailUps.output_watts != null ? `${detailUps.output_watts} W` : "—"}
                                  {detailUps.output_va != null ? ` / ${detailUps.output_va} VA` : ""}
                                </span>
                              </div>
                              <div className="flex justify-between">
                                <span>Output Current</span>
                                <span className="font-mono-tabular text-slate-200">
                                  {detailUps.output_current_a != null
                                    ? `${detailUps.output_current_a} A`
                                    : "—"}
                                </span>
                              </div>
                              {detailUps.energy_kwh != null && (
                                <div className="flex justify-between">
                                  <span>Cumulative Energy</span>
                                  <span className="font-mono-tabular text-slate-200">
                                    {detailUps.energy_kwh} kWh
                                  </span>
                                </div>
                              )}
                            </div>
                          </div>

                          {/* Card 4: AC Input & Output Line */}
                          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-4">
                            <div className="flex items-center justify-between text-xs text-slate-400">
                              <span className="flex items-center gap-1.5">
                                <Activity className="h-3.5 w-3.5 text-sky-400" />
                                AC Input &amp; Output Line
                              </span>
                            </div>
                            <div className="mt-2 font-mono-tabular text-xl font-bold text-white">
                              {detailUps.input_voltage_v != null
                                ? `${detailUps.input_voltage_v}V`
                                : "—"}{" "}
                              <span className="text-slate-500">→</span>{" "}
                              {detailUps.output_voltage_v != null
                                ? `${detailUps.output_voltage_v}V`
                                : "—"}
                            </div>
                            <div className="mt-3 space-y-1 text-[11px] text-slate-400">
                              <div className="flex justify-between">
                                <span>Line Min / Max</span>
                                <span className="font-mono-tabular text-slate-200">
                                  {detailUps.input_min_voltage_v != null
                                    ? detailUps.input_min_voltage_v
                                    : "—"}
                                  –
                                  {detailUps.input_max_voltage_v != null
                                    ? detailUps.input_max_voltage_v
                                    : "—"}{" "}
                                  VAC
                                </span>
                              </div>
                              <div className="flex justify-between">
                                <span>Freq In / Out</span>
                                <span className="font-mono-tabular text-slate-200">
                                  {detailUps.input_frequency_hz != null
                                    ? detailUps.input_frequency_hz
                                    : "—"}{" "}
                                  /{" "}
                                  {detailUps.output_frequency_hz != null
                                    ? detailUps.output_frequency_hz
                                    : "—"}{" "}
                                  Hz
                                </span>
                              </div>
                              <div className="flex justify-between gap-2">
                                <span>Last Transfer</span>
                                <span className="truncate font-mono-tabular text-slate-200">
                                  {detailUps.last_transfer_reason || "—"}
                                </span>
                              </div>
                            </div>
                          </div>
                        </div>

                        {/* UPS Power & Battery Historical Chart */}
                        <div className="space-y-3 rounded-lg border border-slate-800 bg-[#090D16] p-4">
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <button
                                type="button"
                                onClick={() => setUpsChartMode("battery_load")}
                                className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                                  activeUpsChartMode === "battery_load"
                                    ? "border-emerald-500/50 bg-emerald-600 text-white"
                                    : "border-slate-700 bg-[#111827] text-slate-300 hover:bg-slate-800"
                                }`}
                              >
                                Battery &amp; Load (%)
                              </button>
                              <button
                                type="button"
                                onClick={() => setUpsChartMode("voltage")}
                                className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                                  activeUpsChartMode === "voltage"
                                    ? "border-sky-500/50 bg-sky-600 text-white"
                                    : "border-slate-700 bg-[#111827] text-slate-300 hover:bg-slate-800"
                                }`}
                              >
                                AC Voltage (VAC)
                              </button>
                              <button
                                type="button"
                                onClick={() => setUpsChartMode("runtime")}
                                className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                                  activeUpsChartMode === "runtime"
                                    ? "border-emerald-500/50 bg-emerald-600 text-white"
                                    : "border-slate-700 bg-[#111827] text-slate-300 hover:bg-slate-800"
                                }`}
                              >
                                Runtime (min)
                              </button>
                              {hasUpsWatts && (
                                <button
                                  type="button"
                                  onClick={() => setUpsChartMode("watts")}
                                  className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                                    activeUpsChartMode === "watts"
                                      ? "border-amber-500/50 bg-amber-600 text-white"
                                      : "border-slate-700 bg-[#111827] text-slate-300 hover:bg-slate-800"
                                  }`}
                                >
                                  Power (W)
                                </button>
                              )}
                            </div>

                            <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-[#111827] p-1">
                              {(["1h", "6h", "24h", "7d", "30d"] as const).map((rk) => (
                                <button
                                  key={rk}
                                  type="button"
                                  onClick={() => setUpsHistoryRange(rk)}
                                  className={`rounded px-2.5 py-1 font-mono-tabular text-xs font-medium uppercase transition ${
                                    upsHistoryRange === rk
                                      ? "bg-amber-600 text-white"
                                      : "text-slate-400 hover:text-slate-200"
                                  }`}
                                >
                                  {rk}
                                </button>
                              ))}
                            </div>
                          </div>

                          {activeUpsChartMode === "battery_load" && (
                            <DualAreaSparkChart
                              showYAxis
                              windowMs={rangeKeyToWindowMs(upsHistoryRange)}
                              fixedMaxBps={100}
                              primaryLabel="Battery Charge (%)"
                              secondaryLabel="UPS Load (%)"
                              primaryShortLabel="Battery"
                              secondaryShortLabel="Load"
                              primaryColor="#10B981"
                              secondaryColor="#F59E0B"
                              yAxisLabel="%"
                              formatValue={(v) => `${v.toFixed(1)}%`}
                              formatAxisTick={(v) => `${Math.round(v)}%`}
                              emptyMessage="Collecting UPS battery & load time-series samples..."
                              points={upsHistory.map((pt) => ({
                                label: pt.time,
                                inVal: pt.battery_charge_pct ?? 0,
                                outVal: pt.output_load_pct ?? 0,
                              }))}
                            />
                          )}
                          {activeUpsChartMode === "voltage" && (
                            <DualAreaSparkChart
                              showYAxis
                              windowMs={rangeKeyToWindowMs(upsHistoryRange)}
                              primaryLabel="Utility Input (VAC)"
                              secondaryLabel="UPS Output (VAC)"
                              primaryShortLabel="Input"
                              secondaryShortLabel="Output"
                              primaryColor="#38BDF8"
                              secondaryColor="#10B981"
                              yAxisLabel="VAC"
                              formatValue={(v) => `${v.toFixed(1)} V`}
                              formatAxisTick={(v) => String(Math.round(v))}
                              emptyMessage="Collecting UPS line voltage time-series samples..."
                              points={upsHistory.map((pt) => ({
                                label: pt.time,
                                inVal: pt.input_voltage_v ?? 0,
                                outVal: pt.output_voltage_v ?? 0,
                              }))}
                            />
                          )}
                          {activeUpsChartMode === "runtime" && (
                            <DualAreaSparkChart
                              showYAxis
                              hideSecondary
                              windowMs={rangeKeyToWindowMs(upsHistoryRange)}
                              primaryLabel="Estimated Runtime (min)"
                              primaryShortLabel="Runtime"
                              primaryColor="#10B981"
                              yAxisLabel="min"
                              formatValue={(v) => formatDurationCompact(Math.round(v * 60))}
                              formatAxisTick={(v) => `${Math.round(v)}m`}
                              emptyMessage="Collecting UPS runtime time-series samples..."
                              points={upsHistory.map((pt) => ({
                                label: pt.time,
                                inVal:
                                  pt.runtime_remaining_sec != null
                                    ? pt.runtime_remaining_sec / 60
                                    : 0,
                                outVal: 0,
                              }))}
                            />
                          )}
                          {activeUpsChartMode === "watts" && (
                            <DualAreaSparkChart
                              showYAxis
                              hideSecondary
                              windowMs={rangeKeyToWindowMs(upsHistoryRange)}
                              primaryLabel="Output Power (W)"
                              primaryShortLabel="Power"
                              primaryColor="#F59E0B"
                              yAxisLabel="W"
                              formatValue={(v) => `${v.toFixed(0)} W`}
                              formatAxisTick={(v) => String(Math.round(v))}
                              emptyMessage="Collecting UPS wattage time-series samples..."
                              points={upsHistory.map((pt) => ({
                                label: pt.time,
                                inVal: pt.output_watts ?? 0,
                                outVal: 0,
                              }))}
                            />
                          )}
                        </div>

                        {/* Battery Maintenance, Self-Test & Calibration Diagnostics */}
                        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs">
                            <div className="text-slate-400">Self-Test Result</div>
                            <div className="mt-1 flex items-center gap-1.5 font-mono-tabular font-semibold text-white">
                              {detailUps.self_test_result?.toLowerCase() === "passed" && (
                                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
                              )}
                              <span>{detailUps.self_test_result || "—"}</span>
                            </div>
                            <div className="mt-0.5 font-mono-tabular text-[11px] text-slate-400">
                              {detailUps.self_test_date ? `Date: ${detailUps.self_test_date}` : "No date"}
                            </div>
                          </div>

                          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs">
                            <div className="text-slate-400">Runtime Calibration</div>
                            <div className="mt-1 font-mono-tabular font-semibold text-white">
                              {detailUps.calibration_result || "—"}
                            </div>
                            <div className="mt-0.5 font-mono-tabular text-[11px] text-slate-400">
                              {detailUps.calibration_date
                                ? `Date: ${detailUps.calibration_date}`
                                : "No date"}
                            </div>
                          </div>

                          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs">
                            <div className="text-slate-400">UPS Model / Mfg Date</div>
                            <div className="mt-1 truncate font-mono-tabular font-semibold text-white">
                              {detailUps.hardware_model || detailUps.sku || "—"}
                            </div>
                            <div className="mt-0.5 font-mono-tabular text-[11px] text-slate-400">
                              {detailUps.manufacture_date
                                ? `Mfg: ${detailUps.manufacture_date}`
                                : detailUps.serial_number
                                  ? `SN: ${detailUps.serial_number}`
                                  : "—"}
                            </div>
                          </div>

                          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs">
                            <div className="text-slate-400">Battery Replacement &amp; Alarms</div>
                            <div className="mt-1 font-mono-tabular font-semibold text-white">
                              {detailUps.battery_replace_needed ? (
                                <span className="text-rose-400">Replacement Recommended</span>
                              ) : (
                                <span className="text-emerald-400">Healthy</span>
                              )}
                            </div>
                            <div className="mt-0.5 font-mono-tabular text-[11px] text-slate-400">
                              {detailUps.last_replace_date
                                ? `Replaced: ${detailUps.last_replace_date}`
                                : "No replacement date"}{" "}
                              • {detailUps.alarms_present ?? 0} alarms
                            </div>
                          </div>
                        </div>
                      </>
                    )}
                  </div>
                )}

                {/* Printer Supplies & Consumables Card */}
                {(detailProfileType === "printer" || detailPrinterSupplies.length > 0) && (
                  <div className="space-y-4 rounded-xl border border-slate-800 bg-[#111827] p-5">
                    <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
                      <Printer className="h-4 w-4 text-fuchsia-400" />
                      Printer Supplies &amp; Consumables (<code>Printer-MIB</code>)
                    </h3>
                    {detailPrinterSupplies.length === 0 ? (
                      <div className="rounded-lg border border-slate-800 bg-[#090D16] p-4 text-xs text-slate-400">
                        Waiting for <code>Printer-MIB</code> toner/drum/supply levels from this device.
                      </div>
                    ) : (
                      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                        {detailPrinterSupplies.map((sup, idx) => (
                          <div
                            key={`${sup.name}-${idx}`}
                            className="rounded-lg border border-slate-800 bg-[#090D16] p-3"
                          >
                            <div className="flex items-center justify-between gap-2 text-xs">
                              <span className="truncate font-medium text-slate-200">{sup.name}</span>
                              <span className="font-mono-tabular font-semibold text-white">
                                {sup.pct != null ? `${sup.pct.toFixed(0)}%` : `${sup.level}`}
                              </span>
                            </div>
                            {sup.pct != null && (
                              <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                                <div
                                  className={`h-full rounded-full ${
                                    sup.pct > 25
                                      ? "bg-emerald-500"
                                      : sup.pct > 10
                                        ? "bg-amber-500"
                                        : "bg-rose-500"
                                  }`}
                                  style={{ width: `${Math.min(100, Math.max(0, sup.pct))}%` }}
                                />
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {/* Extended Vendor MIB Telemetry: Sensors, CPU/RAM/Storage, Wireless VAPs, and LLDP Neighbors */}
                {deviceDetail?.snmp_system?.extended_metrics && (
                  <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                    {/* Hardware Sensors & Vendor Scalars */}
                    {((deviceDetail.snmp_system.extended_metrics.sensors &&
                      deviceDetail.snmp_system.extended_metrics.sensors.length > 0) ||
                      (deviceDetail.snmp_system.extended_metrics.vendor_scalars &&
                        Object.keys(deviceDetail.snmp_system.extended_metrics.vendor_scalars)
                          .length > 0) ||
                      deviceDetail.snmp_system.extended_metrics.cpu_details?.load_1m != null) && (
                      <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                        <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
                          <Cpu className="h-4 w-4 text-amber-400" />
                          Vendor MIB Sensors, Load & Hardware Metrics
                        </h3>
                        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
                          {deviceDetail.snmp_system.extended_metrics.cpu_details?.load_1m !=
                            null && (
                            <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3">
                              <div className="text-[11px] text-slate-400">Load Avg (1/5/15m)</div>
                              <div className="mt-1 font-mono-tabular text-xs font-semibold text-white">
                                {deviceDetail.snmp_system.extended_metrics.cpu_details.load_1m} /{" "}
                                {deviceDetail.snmp_system.extended_metrics.cpu_details.load_5m} /{" "}
                                {deviceDetail.snmp_system.extended_metrics.cpu_details.load_15m}
                              </div>
                            </div>
                          )}
                          {deviceDetail.snmp_system.extended_metrics.memory_details?.total_bytes !=
                            null && (
                            <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3">
                              <div className="text-[11px] text-slate-400">Physical RAM</div>
                              <div className="mt-1 font-mono-tabular text-xs font-semibold text-sky-400">
                                {formatBytes(
                                  deviceDetail.snmp_system.extended_metrics.memory_details
                                    .used_bytes,
                                )}{" "}
                                /{" "}
                                {formatBytes(
                                  deviceDetail.snmp_system.extended_metrics.memory_details
                                    .total_bytes,
                                )}
                              </div>
                            </div>
                          )}
                          {Object.entries(
                            deviceDetail.snmp_system.extended_metrics.vendor_scalars || {},
                          ).map(([k, v]) => (
                            <div
                              key={k}
                              className="rounded-lg border border-slate-800 bg-[#090D16] p-3"
                            >
                              <div className="text-[11px] text-slate-400">{k}</div>
                              <div className="mt-1 font-mono-tabular text-xs font-semibold text-emerald-400">
                                {String(v)}
                              </div>
                            </div>
                          ))}
                          {(deviceDetail.snmp_system.extended_metrics.sensors || []).map(
                            (s, idx) => (
                              <div
                                key={idx}
                                className="rounded-lg border border-slate-800 bg-[#090D16] p-3"
                              >
                                <div className="text-[11px] text-slate-400">
                                  {s.name} ({s.category})
                                </div>
                                <div className="mt-1 font-mono-tabular text-xs font-semibold text-amber-300">
                                  {s.value}
                                </div>
                              </div>
                            ),
                          )}
                        </div>
                      </div>
                    )}

                    {/* Storage Volumes & Filesystems (HOST-RESOURCES-MIB) */}
                    {deviceDetail.snmp_system.extended_metrics.storage_volumes &&
                      deviceDetail.snmp_system.extended_metrics.storage_volumes.length > 0 && (
                        <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                          <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
                            <HardDrive className="h-4 w-4 text-emerald-400" />
                            Polled Storage Volumes & Disks (HOST-RESOURCES-MIB)
                          </h3>
                          <div className="space-y-3">
                            {deviceDetail.snmp_system.extended_metrics.storage_volumes.map(
                              (vol, idx) => (
                                <div key={idx} className="text-xs">
                                  <div className="mb-1 flex items-center justify-between font-mono-tabular">
                                    <span className="font-semibold text-slate-200">
                                      {vol.mount}
                                    </span>
                                    <span className="text-slate-400">
                                      {formatBytes(vol.used_bytes)} /{" "}
                                      {formatBytes(vol.total_bytes)} ({vol.usage_pct}%)
                                    </span>
                                  </div>
                                  <div className="h-2 w-full overflow-hidden rounded-full bg-[#090D16]">
                                    <div
                                      style={{ width: `${Math.min(100, vol.usage_pct)}%` }}
                                      className={`h-full rounded-full ${
                                        vol.usage_pct >= 90
                                          ? "bg-rose-500"
                                          : vol.usage_pct >= 75
                                            ? "bg-amber-500"
                                            : "bg-emerald-500"
                                      }`}
                                    />
                                  </div>
                                </div>
                              ),
                            )}
                          </div>
                        </div>
                      )}

                    {/* Wireless VAPs (UBNT-UniFi-MIB) */}
                    {deviceDetail.snmp_system.extended_metrics.wireless_radios &&
                      deviceDetail.snmp_system.extended_metrics.wireless_radios.length > 0 && (
                        <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                          <h3 className="mb-3 text-sm font-semibold text-white">
                            Polled Wireless Access Point SSIDs (UBNT-UniFi-MIB)
                          </h3>
                          <table className="w-full text-left font-mono-tabular text-xs">
                            <thead>
                              <tr className="border-b border-slate-800 text-slate-400">
                                <th className="pb-2">SSID</th>
                                <th className="pb-2">Radio</th>
                                <th className="pb-2">Channel</th>
                                <th className="pb-2">TX Power</th>
                                <th className="pb-2 text-right">Clients</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-800/50">
                              {deviceDetail.snmp_system.extended_metrics.wireless_radios.map(
                                (w, idx) => (
                                  <tr key={idx}>
                                    <td className="py-2 font-sans font-semibold text-white">
                                      {w.ssid}
                                    </td>
                                    <td className="py-2 text-slate-300">{w.radio}</td>
                                    <td className="py-2 text-sky-400">Ch {w.channel}</td>
                                    <td className="py-2 text-slate-400">{w.tx_power}</td>
                                    <td className="py-2 text-right text-emerald-400">
                                      {w.stations}
                                    </td>
                                  </tr>
                                ),
                              )}
                            </tbody>
                          </table>
                        </div>
                      )}

                    {/* LLDP Connected Neighbors (LLDP-MIB) */}
                    {deviceDetail.snmp_system.extended_metrics.lldp_neighbors &&
                      deviceDetail.snmp_system.extended_metrics.lldp_neighbors.length > 0 && (
                        <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                          <h3 className="mb-3 text-sm font-semibold text-white">
                            LLDP Connected Neighbors (LLDP-MIB)
                          </h3>
                          <table className="w-full text-left font-mono-tabular text-xs">
                            <thead>
                              <tr className="border-b border-slate-800 text-slate-400">
                                <th className="pb-2">Local Port</th>
                                <th className="pb-2">Neighbor Device</th>
                                <th className="pb-2">Remote Port</th>
                                <th className="pb-2">Description</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-800/50">
                              {deviceDetail.snmp_system.extended_metrics.lldp_neighbors.map(
                                (nb, idx) => (
                                  <tr key={idx}>
                                    <td className="py-2 text-emerald-400">Port #{nb.local_port}</td>
                                    <td className="py-2 font-sans font-semibold text-white">
                                      {nb.neighbor_name}
                                    </td>
                                    <td className="py-2 text-sky-400">{nb.neighbor_port}</td>
                                    <td className="py-2 font-sans text-slate-400">
                                      {nb.neighbor_descr}
                                    </td>
                                  </tr>
                                ),
                              )}
                            </tbody>
                          </table>
                        </div>
                      )}

                    {/* Assigned IPv4 Interfaces (IP-MIB) */}
                    {deviceDetail.snmp_system.extended_metrics.ip_addresses &&
                      deviceDetail.snmp_system.extended_metrics.ip_addresses.length > 0 && (
                        <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                          <h3 className="mb-3 text-sm font-semibold text-white">
                            Assigned IPv4 Interfaces (IP-MIB)
                          </h3>
                          <div className="overflow-x-auto">
                            <table className="w-full text-left font-mono-tabular text-xs">
                              <thead>
                                <tr className="border-b border-slate-800 text-slate-400">
                                  <th className="pb-2">IPv4 Address</th>
                                  <th className="pb-2">Subnet CIDR</th>
                                  <th className="pb-2">Interface</th>
                                  <th className="pb-2">MAC Address</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-slate-800/50">
                                {deviceDetail.snmp_system.extended_metrics.ip_addresses.map(
                                  (ipRow, idx) => (
                                    <tr key={idx}>
                                      <td className="py-2 font-semibold text-white">{ipRow.ip}</td>
                                      <td className="py-2 text-sky-400">{ipRow.cidr || "—"}</td>
                                      <td className="py-2 text-slate-300">
                                        {ipRow.if_name ||
                                          (ipRow.if_index > 0 ? `#${ipRow.if_index}` : "—")}
                                      </td>
                                      <td className="py-2 text-slate-400">
                                        {ipRow.mac_address || "—"}
                                      </td>
                                    </tr>
                                  ),
                                )}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      )}
                  </div>
                )}

                {/* Device SNMP Interfaces & Bandwidth Chart */}
                {deviceDetail?.snmp_interfaces && deviceDetail.snmp_interfaces.length > 0 && (
                  <div className="space-y-4 rounded-xl border border-slate-800 bg-[#111827] p-5">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <h3 className="text-sm font-semibold text-white">
                          {detailIfIndex === 0
                            ? detailProfileType === "ups"
                              ? `UPS Management Network Interface (IF-MIB) (${deviceDetail.snmp_interfaces.length} Port${deviceDetail.snmp_interfaces.length === 1 ? "" : "s"})`
                              : `Device Traffic Overview — All Ports / Interfaces Combined (${deviceDetail.snmp_interfaces.length} Ports)`
                            : `Port #${detailIfIndex} (${
                                deviceDetail.snmp_interfaces.find(
                                  (i) => i.if_index === detailIfIndex,
                                )?.if_name || "Interface"
                              }) — Historical Throughput`}
                        </h3>
                        <p className="text-xs text-slate-400">
                          {detailIfIndex === 0
                            ? detailProfileType === "ups"
                              ? "Management card network port traffic & link status (UPS power, battery, and runtime trends are shown above)."
                              : "Showing total aggregated inbound & outbound traffic across all ports/interfaces on this device. Click any interface row below to inspect an individual port."
                            : "Showing traffic for selected port. Click 'All Ports Combined (Overview)' to return to total device traffic."}
                        </p>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          onClick={() => setDetailIfIndex(0)}
                          className={`rounded-md border px-3 py-1.5 text-xs font-medium transition ${
                            detailIfIndex === 0
                              ? "border-emerald-500/50 bg-emerald-600 text-white"
                              : "border-slate-700 bg-[#090D16] text-slate-300 hover:bg-slate-800"
                          }`}
                        >
                          All Ports Combined (Overview)
                        </button>
                        <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-[#090D16] p-1">
                          {["1h", "6h", "24h", "7d", "30d", "1y", "all"].map((rk) => (
                            <button
                              key={rk}
                              onClick={() => setDetailTrendRange(rk)}
                              className={`rounded px-2.5 py-1 font-mono-tabular text-xs font-medium uppercase transition ${
                                detailTrendRange === rk
                                  ? "bg-emerald-600 text-white"
                                  : "text-slate-400 hover:text-slate-200"
                              }`}
                            >
                              {rk}
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>

                    <DualAreaSparkChart
                      windowMs={rangeKeyToWindowMs(detailTrendRange)}
                      points={detailTrendSeries.map((pt) => ({
                        label: pt.time,
                        inVal: pt.avg_in_bps,
                        outVal: pt.avg_out_bps,
                        p95In: pt.p95_in_bps,
                      }))}
                    />

                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-xs">
                        <thead>
                          <tr className="border-b border-slate-800 text-slate-400">
                            <th className="pb-2 font-medium">ifIndex</th>
                            <th className="pb-2 font-medium">Interface</th>
                            <th className="pb-2 font-medium">Description / Alias</th>
                            <th className="pb-2 font-medium">Speed</th>
                            <th className="pb-2 font-medium">RX Rate</th>
                            <th className="pb-2 font-medium">TX Rate</th>
                            <th className="pb-2 font-medium">Total RX / TX</th>
                            <th className="pb-2 font-medium">Errors</th>
                            <th className="pb-2 text-right font-medium">Status</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                          {deviceDetail.snmp_interfaces.map((intf) => (
                            <tr
                              key={intf.if_index}
                              onClick={() =>
                                setDetailIfIndex(
                                  detailIfIndex === intf.if_index ? 0 : intf.if_index,
                                )
                              }
                              className={`cursor-pointer hover:bg-slate-800/40 ${
                                detailIfIndex === intf.if_index ? "bg-slate-800/60" : ""
                              }`}
                            >
                              <td className="py-2.5 text-slate-400">#{intf.if_index}</td>
                              <td className="py-2.5 font-semibold text-white">{intf.if_name}</td>
                              <td className="py-2.5 font-sans text-slate-400">
                                {intf.if_alias || intf.if_descr || "—"}
                              </td>
                              <td className="py-2.5 text-slate-300">
                                {intf.if_speed_bps > 0 ? formatBps(intf.if_speed_bps) : "Auto"}
                              </td>
                              <td className="py-2.5 text-emerald-400">{formatBps(intf.in_bps)}</td>
                              <td className="py-2.5 text-sky-400">{formatBps(intf.out_bps)}</td>
                              <td className="py-2.5 text-slate-300">
                                {formatBytes(intf.in_octets)} / {formatBytes(intf.out_octets)}
                              </td>
                              <td className="py-2.5 text-slate-400">
                                {intf.in_errors} / {intf.out_errors}
                              </td>
                              <td className="py-2.5 text-right">
                                <span
                                  className={`inline-block rounded px-1.5 py-0.5 text-[11px] ${
                                    intf.if_oper_status === 1
                                      ? "bg-emerald-500/15 text-emerald-400"
                                      : "bg-slate-800 text-slate-400"
                                  }`}
                                >
                                  {intf.if_oper_status === 1 ? "UP" : "DOWN"}
                                </span>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {/* Device Recent Syslogs */}
                <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                  <h3 className="mb-3 text-sm font-semibold text-white">
                    Syslog Messages from {activeDetailIp || `Device #${selectedDeviceRef}`} (
                    {deviceDetail?.recent_logs?.length || 0})
                  </h3>
                  <div className="max-h-80 overflow-y-auto font-mono-tabular text-xs">
                    <table className="w-full text-left">
                      <thead className="sticky top-0 bg-[#111827]">
                        <tr className="border-b border-slate-800 text-slate-400">
                          <th className="pb-2 font-medium">Timestamp</th>
                          <th className="pb-2 font-medium">Severity</th>
                          <th className="pb-2 font-medium">App</th>
                          <th className="pb-2 font-medium">Message</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-800/50">
                        {!deviceDetail?.recent_logs || deviceDetail.recent_logs.length === 0 ? (
                          <tr>
                            <td colSpan={4} className="py-6 text-center font-sans text-slate-500">
                              No syslog events received from{" "}
                              {activeDetailIp || `Device #${selectedDeviceRef}`} yet.
                            </td>
                          </tr>
                        ) : (
                          deviceDetail.recent_logs.map((l, idx) => (
                            <tr key={idx}>
                              <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                                {l.time ? new Date(l.time).toLocaleString() : "—"}
                              </td>
                              <td className="py-2 pr-3">
                                <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase text-slate-300">
                                  {l.severity}
                                </span>
                              </td>
                              <td className="py-2 pr-3 text-sky-400">{l.app_name}</td>
                              <td className="py-2 break-all text-slate-200">{l.message}</td>
                            </tr>
                          ))
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              </>
            )}
          </div>
        )}

        {/* ==================== VIEW 3: SNMP TELEMETRY & PORTS ==================== */}
        {activeTab === "snmp" && (
          <div className="space-y-6">
            {/* Top Control Bar */}
            <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-800 bg-[#111827] p-4">
              <div>
                <h2 className="flex items-center gap-2 text-base font-semibold text-white">
                  <Database className="h-4 w-4 text-sky-400" />
                  SNMP Ingestion &amp; Query Telemetry
                </h2>
                <p className="text-xs text-slate-400">
                  Server-wide SNMP polling throughput, OID ingestion volume, query latency, and
                  per-target health
                </p>
              </div>

              <div className="flex flex-wrap items-center gap-3">
                <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-[#090D16] p-1">
                  {(["1h", "6h", "24h", "7d", "30d"] as const).map((rk) => (
                    <button
                      key={rk}
                      type="button"
                      onClick={() => setSnmpTelemetryRange(rk)}
                      className={`rounded px-2.5 py-1 font-mono-tabular text-xs font-medium uppercase transition ${
                        snmpTelemetryRange === rk
                          ? "bg-sky-600 text-white"
                          : "text-slate-400 hover:text-slate-200"
                      }`}
                    >
                      {rk}
                    </button>
                  ))}
                </div>

                <button
                  type="button"
                  onClick={handlePollAllSnmp}
                  disabled={pollingAllSnmp}
                  className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-60"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${pollingAllSnmp ? "animate-spin" : ""}`} />
                  {pollingAllSnmp ? "Polling SNMP..." : "Poll All SNMP Now"}
                </button>

                <a
                  href="/targets"
                  onClick={(e) => {
                    if (isModifiedClick(e)) return;
                    e.preventDefault();
                    navigateToTab("targets");
                  }}
                  className="flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs font-medium text-emerald-400 hover:bg-slate-700"
                >
                  <Plus className="h-3.5 w-3.5" /> Add / Edit SNMP Hosts
                </a>
              </div>
            </div>

            {/* 5 Fleet Summary KPI Cards */}
            {(() => {
              const s = snmpTelemetry?.summary;
              const verText =
                s && s.version_breakdown && Object.keys(s.version_breakdown).length > 0
                  ? Object.entries(s.version_breakdown)
                      .map(([k, v]) => `${k}: ${v}`)
                      .join(" • ")
                  : "No enabled targets";
              const rate = s?.success_rate_pct ?? 100;
              return (
                <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
                  <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                    <div className="text-xs text-slate-400">SNMP Targets Online</div>
                    <div className="mt-1.5 font-mono-tabular text-2xl font-bold text-white">
                      {s?.reachable_targets ?? 0}{" "}
                      <span className="text-sm font-normal text-slate-400">
                        / {s?.enabled_targets ?? 0}
                      </span>
                    </div>
                    <div className="mt-1 truncate font-mono-tabular text-[11px] text-slate-400">
                      {(s?.unreachable_targets ?? 0) > 0 ? (
                        <span className="text-rose-400">{s?.unreachable_targets} offline • </span>
                      ) : null}
                      {verText}
                    </div>
                  </div>

                  <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                    <div className="text-xs text-slate-400">OIDs Ingested</div>
                    <div className="mt-1.5 font-mono-tabular text-2xl font-bold text-emerald-400">
                      {(s?.total_oids_ingested ?? 0).toLocaleString()}
                    </div>
                    <div className="mt-1 font-mono-tabular text-[11px] text-slate-400">
                      {s?.oids_per_min ?? 0} OIDs/min • {(s?.total_walks ?? 0).toLocaleString()} walks
                    </div>
                  </div>

                  <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                    <div className="text-xs text-slate-400">Poll Success Rate</div>
                    <div
                      className={`mt-1.5 font-mono-tabular text-2xl font-bold ${
                        rate >= 98
                          ? "text-emerald-400"
                          : rate >= 90
                            ? "text-amber-400"
                            : "text-rose-400"
                      }`}
                    >
                      {rate.toFixed(1)}%
                    </div>
                    <div className="mt-1 font-mono-tabular text-[11px] text-slate-400">
                      {s?.successful_polls ?? 0} ok / {s?.failed_polls ?? 0} failed
                    </div>
                  </div>

                  <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                    <div className="text-xs text-slate-400">Query Latency</div>
                    <div className="mt-1.5 font-mono-tabular text-2xl font-bold text-sky-400">
                      {(s?.avg_latency_ms ?? 0).toFixed(0)} ms
                    </div>
                    <div className="mt-1 font-mono-tabular text-[11px] text-slate-400">
                      P95 {(s?.p95_latency_ms ?? 0).toFixed(0)} ms • Max{" "}
                      {(s?.max_latency_ms ?? 0).toFixed(0)} ms
                    </div>
                  </div>

                  <div className="col-span-2 rounded-xl border border-slate-800 bg-[#111827] p-4 lg:col-span-1">
                    <div className="text-xs text-slate-400">Interfaces &amp; DB Rows</div>
                    <div className="mt-1.5 font-mono-tabular text-2xl font-bold text-white">
                      {s?.total_interfaces_tracked ?? 0}
                    </div>
                    <div className="mt-1 font-mono-tabular text-[11px] text-slate-400">
                      {(s?.raw_table_rows_24h ?? 0).toLocaleString()} raw rows (24h)
                    </div>
                  </div>
                </div>
              );
            })()}

            {/* SNMP Ingestion & Query Telemetry Chart */}
            <div className="space-y-4 rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-white">
                    {snmpChartMode === "ingestion"
                      ? "SNMP Ingestion Rate (OIDs & Interface Samples per Bucket)"
                      : snmpChartMode === "latency"
                        ? "SNMP Query Round-Trip Latency (Avg & Peak ms)"
                        : "SNMP Poll Outcomes (Successful vs Failed Queries)"}
                  </h3>
                  <p className="text-xs text-slate-400">
                    Telemetry recorded by the SNMP poller into TimescaleDB (
                    <code>snmp_poll_metrics</code>) across the selected{" "}
                    <span className="font-mono-tabular uppercase text-slate-300">
                      {snmpTelemetryRange}
                    </span>{" "}
                    window.
                  </p>
                </div>

                <div className="flex flex-wrap items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => setSnmpChartMode("ingestion")}
                    className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                      snmpChartMode === "ingestion"
                        ? "border-emerald-500/50 bg-emerald-600 text-white"
                        : "border-slate-700 bg-[#090D16] text-slate-300 hover:bg-slate-800"
                    }`}
                  >
                    OIDs &amp; Samples
                  </button>
                  <button
                    type="button"
                    onClick={() => setSnmpChartMode("latency")}
                    className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                      snmpChartMode === "latency"
                        ? "border-sky-500/50 bg-sky-600 text-white"
                        : "border-slate-700 bg-[#090D16] text-slate-300 hover:bg-slate-800"
                    }`}
                  >
                    Query Latency (ms)
                  </button>
                  <button
                    type="button"
                    onClick={() => setSnmpChartMode("polls")}
                    className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                      snmpChartMode === "polls"
                        ? "border-emerald-500/50 bg-emerald-600 text-white"
                        : "border-slate-700 bg-[#090D16] text-slate-300 hover:bg-slate-800"
                    }`}
                  >
                    Poll Volume
                  </button>
                </div>
              </div>

              {snmpChartMode === "ingestion" && (
                <DualAreaSparkChart
                  showYAxis
                  windowMs={rangeKeyToWindowMs(snmpTelemetryRange)}
                  primaryLabel="OIDs Ingested"
                  secondaryLabel="Interface Samples"
                  primaryShortLabel="OIDs"
                  secondaryShortLabel="If Samples"
                  primaryColor="#10B981"
                  secondaryColor="#38BDF8"
                  yAxisLabel="OIDs"
                  formatValue={(v) => `${Math.round(v).toLocaleString()}`}
                  formatAxisTick={(v) =>
                    v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(Math.round(v))
                  }
                  emptyMessage="Waiting for SNMP poll telemetry samples..."
                  points={(snmpTelemetry?.series || []).map((pt) => ({
                    label: pt.time,
                    inVal: pt.oids_collected,
                    outVal: pt.interface_samples,
                  }))}
                />
              )}

              {snmpChartMode === "latency" && (
                <DualAreaSparkChart
                  showYAxis
                  windowMs={rangeKeyToWindowMs(snmpTelemetryRange)}
                  primaryLabel="Avg Latency (ms)"
                  secondaryLabel="Peak Latency (ms)"
                  primaryShortLabel="Avg"
                  secondaryShortLabel="Peak"
                  primaryColor="#38BDF8"
                  secondaryColor="#F59E0B"
                  yAxisLabel="ms"
                  formatValue={(v) => `${v.toFixed(1)} ms`}
                  formatAxisTick={(v) => String(Math.round(v))}
                  emptyMessage="Waiting for SNMP query latency telemetry..."
                  points={(snmpTelemetry?.series || []).map((pt) => ({
                    label: pt.time,
                    inVal: pt.avg_latency_ms,
                    outVal: pt.max_latency_ms,
                  }))}
                />
              )}

              {snmpChartMode === "polls" && (
                <DualAreaSparkChart
                  showYAxis
                  windowMs={rangeKeyToWindowMs(snmpTelemetryRange)}
                  primaryLabel="Successful Polls"
                  secondaryLabel="Failed Polls"
                  primaryShortLabel="OK"
                  secondaryShortLabel="Failed"
                  primaryColor="#10B981"
                  secondaryColor="#F43F5E"
                  yAxisLabel="Polls"
                  formatValue={(v) => `${Math.round(v)}`}
                  formatAxisTick={(v) => String(Math.round(v))}
                  emptyMessage="Waiting for SNMP poll count telemetry..."
                  points={(snmpTelemetry?.series || []).map((pt) => ({
                    label: pt.time,
                    inVal: pt.polls_ok,
                    outVal: pt.polls_failed,
                  }))}
                />
              )}
            </div>

            {/* Active Fleet MIBs Strip */}
            {(snmpTelemetry?.summary?.active_mibs_fleet?.length ?? 0) > 0 && (
              <div className="rounded-xl border border-slate-800 bg-[#111827] px-5 py-3.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-medium text-slate-400">Active Fleet MIBs:</span>
                  {(snmpTelemetry?.summary?.active_mibs_fleet || []).map((mib) => (
                    <span
                      key={mib}
                      className="rounded border border-sky-500/30 bg-[#090D16] px-2 py-0.5 font-mono-tabular text-[11px] text-sky-300"
                    >
                      {mib}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Per-Target SNMP Query Health Table */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold text-white">
                  Per-Target SNMP Query Health ({snmpTelemetry?.targets?.length ?? 0})
                </h3>
                <span className="text-xs text-slate-400">
                  Click any target row to inspect its device-specific telemetry page
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="pb-2 font-medium">Status</th>
                      <th className="pb-2 font-medium">Device</th>
                      <th className="pb-2 font-medium">Type &amp; Vendor</th>
                      <th className="pb-2 font-medium">Profile / Ver</th>
                      <th className="pb-2 font-medium">OIDs / Poll</th>
                      <th className="pb-2 font-medium">Walks</th>
                      <th className="pb-2 font-medium">Avg / Last Latency</th>
                      <th className="pb-2 font-medium">Success Rate</th>
                      <th className="pb-2 font-medium">Responding MIBs</th>
                      <th className="pb-2 text-right font-medium">Last Polled</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60">
                    {(snmpTelemetry?.targets?.length ?? 0) === 0 ? (
                      <tr>
                        <td colSpan={10} className="py-6 text-center text-slate-500">
                          No SNMP targets configured yet. Click Add / Edit SNMP Hosts above.
                        </td>
                      </tr>
                    ) : (
                      (snmpTelemetry?.targets || []).map((t) => (
                        <tr
                          key={t.device_ip}
                          onClick={() => openDevicePage(t.device_ip, t.device_id)}
                          className="cursor-pointer transition hover:bg-slate-800/40"
                        >
                          <td className="py-2.5">
                            <span
                              className={`inline-block rounded px-1.5 py-0.5 font-mono-tabular text-[11px] ${
                                !t.enabled
                                  ? "bg-slate-800 text-slate-500"
                                  : t.is_reachable
                                    ? "bg-emerald-500/15 text-emerald-400"
                                    : "bg-rose-500/15 text-rose-400"
                              }`}
                            >
                              {!t.enabled ? "DISABLED" : t.is_reachable ? "ONLINE" : "OFFLINE"}
                            </span>
                          </td>
                          <td className="py-2.5">
                            <div className="font-mono-tabular font-semibold text-emerald-400 underline">
                              <a
                                href={getDeviceHref(t.device_ip, t.device_id)}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  if (isModifiedClick(e)) return;
                                  e.preventDefault();
                                  openDevicePage(t.device_ip, t.device_id);
                                }}
                                className="hover:text-emerald-300"
                              >
                                {t.device_ip}
                              </a>
                            </div>
                            <div className="text-[11px] text-slate-400">{t.sys_name || "—"}</div>
                          </td>
                          <td className="py-2.5">
                            <span
                              className={`mr-1.5 inline-block rounded border px-1.5 py-0.5 text-[10px] uppercase ${
                                t.device_profile_type === "ups"
                                  ? "border-amber-500/30 bg-amber-500/15 text-amber-300"
                                  : t.device_profile_type === "nas"
                                    ? "border-violet-500/30 bg-violet-500/15 text-violet-300"
                                    : t.device_profile_type === "printer"
                                      ? "border-fuchsia-500/30 bg-fuchsia-500/15 text-fuchsia-300"
                                      : t.device_profile_type === "server"
                                        ? "border-sky-500/30 bg-sky-500/15 text-sky-300"
                                        : "border-cyan-500/30 bg-cyan-500/15 text-cyan-300"
                              }`}
                            >
                              {t.device_profile_type}
                            </span>
                            <span className="text-slate-300">
                              {t.vendor_name || t.device_category || "—"}
                            </span>
                          </td>
                          <td className="py-2.5 font-mono-tabular text-slate-300">
                            {t.profile_name} ({t.snmp_version})
                          </td>
                          <td className="py-2.5 font-mono-tabular text-white">
                            {t.oids_per_poll.toFixed(0)}
                          </td>
                          <td className="py-2.5 font-mono-tabular text-slate-300">
                            {t.walks_per_poll.toFixed(0)}
                          </td>
                          <td className="py-2.5 font-mono-tabular text-sky-400">
                            {t.avg_latency_ms.toFixed(0)} / {t.last_poll_duration_ms.toFixed(0)} ms
                          </td>
                          <td className="py-2.5 font-mono-tabular">
                            <span
                              className={
                                t.success_rate_pct >= 98
                                  ? "text-emerald-400"
                                  : t.success_rate_pct >= 90
                                    ? "text-amber-400"
                                    : "text-rose-400"
                              }
                            >
                              {t.success_rate_pct.toFixed(1)}%
                            </span>
                            {t.failed_polls > 0 && (
                              <span className="ml-1 text-[10px] text-rose-400">
                                ({t.failed_polls} err)
                              </span>
                            )}
                          </td>
                          <td className="py-2.5">
                            <div className="flex flex-wrap gap-1">
                              {t.responding_mibs.slice(0, 3).map((mib) => (
                                <span
                                  key={mib}
                                  className="rounded border border-slate-700 bg-[#090D16] px-1.5 py-0.5 font-mono-tabular text-[10px] text-slate-300"
                                >
                                  {mib}
                                </span>
                              ))}
                              {t.responding_mibs.length > 3 && (
                                <span className="font-mono-tabular text-[10px] text-slate-500">
                                  +{t.responding_mibs.length - 3}
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="py-2.5 text-right font-mono-tabular text-slate-400">
                            {t.last_polled ? new Date(t.last_polled).toLocaleTimeString() : "Never"}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            {/* 64-Bit Interface Counters (ifXTable) */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <h3 className="text-sm font-semibold text-white">
                  64-Bit Interface Counters (<code>ifXTable</code>)
                </h3>
                <div className="flex items-center gap-2">
                  <label className="text-[11px] text-slate-400">Device IP:</label>
                  <select
                    value={snmpPortFilterIp}
                    onChange={(e) => setSnmpPortFilterIp(e.target.value)}
                    className="rounded-md border border-slate-700 bg-[#090D16] px-2.5 py-1 font-mono-tabular text-xs text-white"
                  >
                    <option value="">All Polled Devices</option>
                    {Array.from(new Set(interfaces.map((i) => i.device_ip))).map((ip) => (
                      <option key={ip} value={ip}>
                        {ip}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="pb-2 font-medium">Device</th>
                      <th className="pb-2 font-medium">Index</th>
                      <th className="pb-2 font-medium">Interface</th>
                      <th className="pb-2 font-medium">Alias / Description</th>
                      <th className="pb-2 font-medium">Link Speed</th>
                      <th className="pb-2 font-medium">RX Rate</th>
                      <th className="pb-2 font-medium">TX Rate</th>
                      <th className="pb-2 font-medium">Errors (In/Out)</th>
                      <th className="pb-2 text-right font-medium">OperStatus</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                    {interfaces
                      .filter((intf) => !snmpPortFilterIp || intf.device_ip === snmpPortFilterIp)
                      .map((intf) => (
                        <tr
                          key={`${intf.device_ip}-${intf.if_index}`}
                          onClick={() => openDevicePage(intf.device_ip, intf.device_id)}
                          className="cursor-pointer hover:bg-slate-800/40"
                        >
                          <td className="py-2.5 text-emerald-400 underline">
                            <a
                              href={getDeviceHref(intf.device_ip, intf.device_id)}
                              onClick={(e) => {
                                e.stopPropagation();
                                if (isModifiedClick(e)) return;
                                e.preventDefault();
                                openDevicePage(intf.device_ip, intf.device_id);
                              }}
                              className="hover:text-emerald-300"
                            >
                              {intf.device_ip}
                            </a>
                          </td>
                          <td className="py-2.5 text-slate-400">#{intf.if_index}</td>
                          <td className="py-2.5 font-semibold text-white">{intf.if_name}</td>
                          <td className="py-2.5 font-sans text-slate-400">
                            {intf.if_alias || intf.if_descr || "—"}
                          </td>
                          <td className="py-2.5 text-slate-300">
                            {intf.if_speed_bps > 0 ? formatBps(intf.if_speed_bps) : "Auto"}
                          </td>
                          <td className="py-2.5 text-emerald-400">{formatBps(intf.in_bps)}</td>
                          <td className="py-2.5 text-sky-400">{formatBps(intf.out_bps)}</td>
                          <td className="py-2.5 text-slate-400">
                            {intf.in_errors} / {intf.out_errors}
                          </td>
                          <td className="py-2.5 text-right">
                            <span
                              className={`inline-block rounded px-1.5 py-0.5 text-[11px] ${
                                intf.if_oper_status === 1
                                  ? "bg-emerald-500/15 text-emerald-400"
                                  : "bg-slate-800 text-slate-400"
                              }`}
                            >
                              {intf.if_oper_status === 1 ? "UP" : "DOWN"}
                            </span>
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* ==================== VIEW 4: SUBNET SCANNER MATRIX ==================== */}
        {activeTab === "scanner" && (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-800 bg-[#111827] p-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-slate-400">Subnets:</span>
                {subnets.map((cidr) => (
                  <button
                    key={cidr}
                    onClick={() => setSelectedSubnet(cidr)}
                    className={`rounded-md px-3 py-1.5 font-mono-tabular text-xs font-medium transition ${
                      selectedSubnet === cidr
                        ? "bg-emerald-600 text-white"
                        : "border border-slate-800 bg-[#090D16] text-slate-300 hover:text-white"
                    }`}
                  >
                    {cidr}
                  </button>
                ))}
                <a
                  href="/targets"
                  onClick={(e) => {
                    if (isModifiedClick(e)) return;
                    e.preventDefault();
                    navigateToTab("targets");
                  }}
                  className="flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-xs font-medium text-emerald-400 hover:bg-slate-700"
                >
                  <Plus className="h-3.5 w-3.5" /> Add / Manage Subnets
                </a>
              </div>

              <button
                onClick={triggerScanNow}
                disabled={scanningNow || subnets.length === 0}
                className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
              >
                <Play className="h-3.5 w-3.5" />
                <span>{scanningNow ? "Scanning Subnets..." : "Trigger Nmap Sweep Now"}</span>
              </button>
            </div>

            {selectedSubnet && selectedSubnet.endsWith("/24") && (
              <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-white">
                    /24 IP Occupancy Grid ({selectedSubnet}) — Click Any Active IP for Details
                  </h3>
                  <div className="flex items-center gap-4 text-xs text-slate-400">
                    <span className="flex items-center gap-1.5">
                      <span className="h-2.5 w-2.5 rounded-xs bg-emerald-500" /> Online
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span className="h-2.5 w-2.5 rounded-xs bg-amber-500/60" /> Historical /
                      Offline
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span className="h-2.5 w-2.5 rounded-xs bg-slate-800" /> Unused
                    </span>
                  </div>
                </div>
                <div className="grid grid-cols-16 gap-1 sm:grid-cols-32">
                  {Array.from({ length: 254 }, (_, i) => i + 1).map((octet) => {
                    const prefix = selectedSubnet
                      .split("/")[0]
                      .split(".")
                      .slice(0, 3)
                      .join(".");
                    const fullIp = `${prefix}.${octet}`;
                    const match = filteredHosts.find((h) => h.ip_address === fullIp);
                    const bgClass = match
                      ? match.is_online
                        ? "bg-emerald-500 text-slate-950 font-semibold cursor-pointer hover:ring-2 hover:ring-white"
                        : "bg-amber-500/40 text-amber-200 cursor-pointer"
                      : "bg-[#090D16] text-slate-600 border border-slate-800/70";
                    return match ? (
                      <a
                        key={octet}
                        href={getDeviceHref(fullIp, match.device_id)}
                        onClick={(e) => {
                          if (isModifiedClick(e)) return;
                          e.preventDefault();
                          openDevicePage(fullIp, match.device_id);
                        }}
                        title={`${fullIp} • ${match.hostname || "host"} (${match.mac_address || "no MAC"})`}
                        className={`flex h-6 items-center justify-center rounded-xs font-mono-tabular text-[10px] ${bgClass}`}
                      >
                        .{octet}
                      </a>
                    ) : (
                      <div
                        key={octet}
                        title={`${fullIp} (Unused)`}
                        className={`flex h-6 items-center justify-center rounded-xs font-mono-tabular text-[10px] ${bgClass}`}
                      >
                        .{octet}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <h3 className="mb-3 text-sm font-semibold text-white">
                Discovered Hosts ({filteredHosts.length}) — Click Any Host to Open Device Page
              </h3>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="pb-2 font-medium">IPv4 Address</th>
                      <th className="pb-2 font-medium">Hostname</th>
                      <th className="pb-2 font-medium">MAC Address</th>
                      <th className="pb-2 font-medium">OUI Vendor</th>
                      <th className="pb-2 font-medium">VLAN / Port</th>
                      <th className="pb-2 font-medium">Source</th>
                      <th className="pb-2 text-right font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                    {filteredHosts.map((h) => (
                      <tr
                        key={h.ip_address}
                        onClick={() => openDevicePage(h.ip_address, h.device_id)}
                        className="cursor-pointer hover:bg-slate-800/40"
                      >
                        <td className="py-2.5 font-semibold text-emerald-400 underline">
                          <a
                            href={getDeviceHref(h.ip_address, h.device_id)}
                            onClick={(e) => {
                              e.stopPropagation();
                              if (isModifiedClick(e)) return;
                              e.preventDefault();
                              openDevicePage(h.ip_address, h.device_id);
                            }}
                            className="hover:text-emerald-300"
                          >
                            {h.ip_address}
                          </a>
                        </td>
                        <td className="py-2.5 font-sans text-slate-200">{h.hostname || "—"}</td>
                        <td className="py-2.5 text-slate-300">{h.mac_address || "—"}</td>
                        <td className="py-2.5 font-sans text-slate-400">{h.vendor || "—"}</td>
                        <td className="py-2.5 text-slate-400">
                          {h.vlan_id ? `VLAN ${h.vlan_id}` : "—"}
                          {h.switch_port ? ` (Port #${h.switch_port})` : ""}
                        </td>
                        <td className="py-2.5 text-slate-400">{h.discovery_source}</td>
                        <td className="py-2.5 text-right">
                          <span
                            className={`inline-block rounded px-1.5 py-0.5 text-[11px] ${
                              h.is_online
                                ? "bg-emerald-500/15 text-emerald-400"
                                : "bg-slate-800 text-slate-400"
                            }`}
                          >
                            {h.is_online ? "ONLINE" : "OFFLINE"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* ==================== VIEW 5: LIVE SYSLOG STREAM ==================== */}
        {activeTab === "syslog" && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-[#111827] p-4">
              <div className="flex flex-wrap items-center gap-3">
                <div className="relative">
                  <Search className="pointer-events-none absolute top-2 left-2.5 h-3.5 w-3.5 text-slate-500" />
                  <input
                    type="text"
                    placeholder="Search syslog messages, apps, hosts..."
                    value={logSearch}
                    onChange={(e) => setLogSearch(e.target.value)}
                    className="rounded-md border border-slate-700 bg-[#090D16] py-1.5 pr-3 pl-8 text-xs text-white placeholder:text-slate-500"
                  />
                </div>

                <select
                  value={logSeverity}
                  onChange={(e) => setLogSeverity(e.target.value)}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs text-white"
                >
                  <option value="all">All Severities</option>
                  <option value="err">Error / Critical</option>
                  <option value="warning">Warning</option>
                  <option value="notice">Notice</option>
                  <option value="info">Info</option>
                  <option value="debug">Debug</option>
                </select>

                <select
                  value={logSourceIp}
                  onChange={(e) => setLogSourceIp(e.target.value)}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                >
                  <option value="all">All Source IPs</option>
                  {logSources.map((s) => (
                    <option key={s.source_ip} value={s.source_ip}>
                      {s.source_ip} ({s.hostname})
                    </option>
                  ))}
                </select>
              </div>

              <button
                onClick={() => setLogsPaused(!logsPaused)}
                className={`rounded-md border px-3 py-1.5 text-xs font-medium ${
                  logsPaused
                    ? "border-amber-500/40 bg-amber-500/15 text-amber-300"
                    : "border-slate-700 bg-[#090D16] text-slate-300"
                }`}
              >
                {logsPaused ? "Resume Live Stream" : "Pause Stream"}
              </button>
            </div>

            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
              <div className="max-h-[600px] overflow-y-auto font-mono-tabular text-xs">
                <table className="w-full text-left">
                  <thead className="sticky top-0 bg-[#111827]">
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="pb-2 font-medium">Timestamp</th>
                      <th className="pb-2 font-medium">Severity</th>
                      <th className="pb-2 font-medium">Source Host</th>
                      <th className="pb-2 font-medium">App</th>
                      <th className="pb-2 font-medium">Message</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/50">
                    {logs.length === 0 ? (
                      <tr>
                        <td colSpan={5} className="py-8 text-center font-sans text-slate-500">
                          No syslog events matching filter. Point network devices to UDP/TCP port
                          514 on this VM.
                        </td>
                      </tr>
                    ) : (
                      logs.map((l, idx) => {
                        const sevColor =
                          l.severity_num <= 3
                            ? "bg-rose-500/15 text-rose-400"
                            : l.severity_num === 4
                              ? "bg-amber-500/15 text-amber-400"
                              : "bg-emerald-500/10 text-emerald-400";
                        return (
                          <tr key={`${l.time}-${idx}`} className="hover:bg-slate-800/30">
                            <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                              {l.time ? new Date(l.time).toLocaleTimeString() : "—"}
                            </td>
                            <td className="py-2 pr-3">
                              <span
                                className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${sevColor}`}
                              >
                                {l.severity}
                              </span>
                            </td>
                            <td
                              onClick={() => openDevicePage(l.source_ip, l.device_id)}
                              className="cursor-pointer py-2 pr-3 whitespace-nowrap text-emerald-400 underline"
                            >
                              <a
                                href={getDeviceHref(l.source_ip, l.device_id)}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  if (isModifiedClick(e)) return;
                                  e.preventDefault();
                                  openDevicePage(l.source_ip, l.device_id);
                                }}
                                className="hover:text-emerald-300"
                              >
                                {l.hostname || l.source_ip}
                              </a>
                            </td>
                            <td className="py-2 pr-3 whitespace-nowrap text-sky-400">
                              {l.app_name}
                            </td>
                            <td className="py-2 break-all text-slate-200">{l.message}</td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* ==================== VIEW 6: TARGETS & SNMP ==================== */}
        {activeTab === "targets" && (
          <div className="space-y-6">
            {settingsFeedback && (
              <div
                className={`flex items-center justify-between rounded-lg border px-4 py-2.5 text-xs ${
                  settingsFeedback.type === "ok"
                    ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                    : "border-rose-500/40 bg-rose-500/15 text-rose-300"
                }`}
              >
                <span>{settingsFeedback.text}</span>
                <button onClick={() => setSettingsFeedback(null)}>
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            )}

            {/* 1. Subnets to Search */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <h2 className="text-sm font-semibold text-white">
                Allowed Subnets to Search (Nmap + ARP Discovery)
              </h2>
              <p className="mt-0.5 text-xs text-slate-400">
                Add or remove IPv4 CIDRs to sweep for active hosts. Adding a subnet immediately
                queues a background scan.
              </p>

              <form onSubmit={handleAddSubnet} className="mt-4 flex flex-wrap gap-2.5">
                <input
                  type="text"
                  placeholder="IPv4 CIDR (e.g. 10.1.3.0/24)"
                  value={newSubnetCidr}
                  onChange={(e) => setNewSubnetCidr(e.target.value)}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                  required
                />
                <input
                  type="text"
                  placeholder="Description / Label (e.g. Management VLAN)"
                  value={newSubnetLabel}
                  onChange={(e) => setNewSubnetLabel(e.target.value)}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs text-white"
                />
                <input
                  type="number"
                  placeholder="VLAN ID (optional)"
                  value={newSubnetVlan}
                  onChange={(e) => setNewSubnetVlan(e.target.value)}
                  className="w-36 rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                />
                <button
                  type="submit"
                  className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                >
                  <Plus className="h-3.5 w-3.5" /> Add Subnet
                </button>
              </form>

              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="pb-2 font-medium">Subnet CIDR</th>
                      <th className="pb-2 font-medium">Label</th>
                      <th className="pb-2 font-medium">VLAN Tag</th>
                      <th className="pb-2 text-right font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                    {(config?.subnets || []).map((s) => (
                      <tr key={s.id}>
                        <td className="py-2.5 font-semibold text-white">{s.subnet_cidr}</td>
                        <td className="py-2.5 font-sans text-slate-300">{s.label || "—"}</td>
                        <td className="py-2.5 text-slate-400">
                          {s.vlan_id ? `VLAN ${s.vlan_id}` : "Untagged"}
                        </td>
                        <td className="py-2.5 text-right">
                          <button
                            onClick={() => handleDeleteSubnet(s.id)}
                            className="inline-flex items-center gap-1 rounded px-2 py-1 text-rose-400 hover:bg-rose-500/15"
                          >
                            <Trash2 className="h-3.5 w-3.5" /> Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* 2. Multiple SNMP Credential Profiles & Secrets (With Reveal & Edit) */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <KeyRound className="h-4 w-4 text-sky-400" />
                  <h2 className="text-sm font-semibold text-white">
                    SNMP Query Profiles & Credentials (Multiple v2c / v3 Secrets)
                  </h2>
                </div>
                {editingProfileId && (
                  <span className="rounded border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-xs text-amber-300">
                    Editing Profile #{editingProfileId}
                  </span>
                )}
              </div>
              <p className="mt-0.5 text-xs text-slate-400">
                Create or edit reusable SNMPv2c community profiles or SNMPv3 AuthPriv secret
                profiles. Use the eye icon to reveal secrets while typing or on saved profiles.
              </p>

              <form
                onSubmit={handleSaveProfile}
                className="mt-4 space-y-3 rounded-lg border border-slate-800 bg-[#090D16] p-4"
              >
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
                  <div>
                    <label className="block text-[11px] text-slate-400">Profile Name</label>
                    <input
                      type="text"
                      placeholder="e.g. Core Switches v2c"
                      value={newProfileName}
                      onChange={(e) => setNewProfileName(e.target.value)}
                      className="mt-1 w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 text-xs text-white"
                      required
                    />
                  </div>
                  <div>
                    <label className="block text-[11px] text-slate-400">SNMP Version</label>
                    <select
                      value={newProfileVersion}
                      onChange={(e) => setNewProfileVersion(e.target.value)}
                      className="mt-1 w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 text-xs text-white"
                    >
                      <option value="v2c">SNMP v2c (Community)</option>
                      <option value="v3">SNMP v3 (User / Auth / Priv)</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-[11px] text-slate-400">UDP Port</label>
                    <input
                      type="number"
                      value={newProfilePort}
                      onChange={(e) => setNewProfilePort(Number(e.target.value))}
                      className="mt-1 w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 font-mono-tabular text-xs text-white"
                    />
                  </div>
                  {newProfileVersion === "v2c" ? (
                    <div>
                      <label className="block text-[11px] text-slate-400">
                        v2c Community Secret
                      </label>
                      <div className="relative mt-1">
                        <input
                          type={showFormSecrets ? "text" : "password"}
                          placeholder="public"
                          value={newProfileCommunity}
                          onChange={(e) => setNewProfileCommunity(e.target.value)}
                          className="w-full rounded-md border border-slate-700 bg-[#111827] py-1.5 pr-8 pl-3 font-mono-tabular text-xs text-white"
                        />
                        <button
                          type="button"
                          onClick={() => setShowFormSecrets(!showFormSecrets)}
                          className="absolute top-1.5 right-2 text-slate-400 hover:text-slate-200"
                          title={showFormSecrets ? "Hide secret" : "Reveal secret"}
                        >
                          {showFormSecrets ? (
                            <EyeOff className="h-3.5 w-3.5" />
                          ) : (
                            <Eye className="h-3.5 w-3.5" />
                          )}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div>
                      <label className="block text-[11px] text-slate-400">v3 Security User</label>
                      <input
                        type="text"
                        placeholder="snmpv3user"
                        value={newProfileV3User}
                        onChange={(e) => setNewProfileV3User(e.target.value)}
                        className="mt-1 w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 font-mono-tabular text-xs text-white"
                      />
                    </div>
                  )}
                </div>

                {newProfileVersion === "v3" && (
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
                    <div>
                      <label className="block text-[11px] text-slate-400">Auth Protocol</label>
                      <select
                        value={newProfileV3AuthProto}
                        onChange={(e) => setNewProfileV3AuthProto(e.target.value)}
                        className="mt-1 w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 text-xs text-white"
                      >
                        <option value="SHA">SHA</option>
                        <option value="SHA-256">SHA-256</option>
                        <option value="MD5">MD5</option>
                      </select>
                    </div>
                    <div>
                      <label className="block text-[11px] text-slate-400">Auth Passphrase</label>
                      <div className="relative mt-1">
                        <input
                          type={showFormSecrets ? "text" : "password"}
                          placeholder="Auth secret key"
                          value={newProfileV3AuthKey}
                          onChange={(e) => setNewProfileV3AuthKey(e.target.value)}
                          className="w-full rounded-md border border-slate-700 bg-[#111827] py-1.5 pr-8 pl-3 font-mono-tabular text-xs text-white"
                        />
                        <button
                          type="button"
                          onClick={() => setShowFormSecrets(!showFormSecrets)}
                          className="absolute top-1.5 right-2 text-slate-400 hover:text-slate-200"
                        >
                          {showFormSecrets ? (
                            <EyeOff className="h-3.5 w-3.5" />
                          ) : (
                            <Eye className="h-3.5 w-3.5" />
                          )}
                        </button>
                      </div>
                    </div>
                    <div>
                      <label className="block text-[11px] text-slate-400">Privacy Protocol</label>
                      <select
                        value={newProfileV3PrivProto}
                        onChange={(e) => setNewProfileV3PrivProto(e.target.value)}
                        className="mt-1 w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 text-xs text-white"
                      >
                        <option value="AES">AES</option>
                        <option value="AES-256">AES-256</option>
                        <option value="DES">DES</option>
                      </select>
                    </div>
                    <div>
                      <label className="block text-[11px] text-slate-400">
                        Privacy Passphrase
                      </label>
                      <div className="relative mt-1">
                        <input
                          type={showFormSecrets ? "text" : "password"}
                          placeholder="Priv secret key"
                          value={newProfileV3PrivKey}
                          onChange={(e) => setNewProfileV3PrivKey(e.target.value)}
                          className="w-full rounded-md border border-slate-700 bg-[#111827] py-1.5 pr-8 pl-3 font-mono-tabular text-xs text-white"
                        />
                        <button
                          type="button"
                          onClick={() => setShowFormSecrets(!showFormSecrets)}
                          className="absolute top-1.5 right-2 text-slate-400 hover:text-slate-200"
                        >
                          {showFormSecrets ? (
                            <EyeOff className="h-3.5 w-3.5" />
                          ) : (
                            <Eye className="h-3.5 w-3.5" />
                          )}
                        </button>
                      </div>
                    </div>
                  </div>
                )}

                <div className="flex justify-end gap-2">
                  {editingProfileId && (
                    <button
                      type="button"
                      onClick={resetProfileForm}
                      className="rounded-md border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-800"
                    >
                      Cancel Edit
                    </button>
                  )}
                  <button
                    type="submit"
                    className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-sky-500"
                  >
                    {editingProfileId ? (
                      <>
                        <Check className="h-3.5 w-3.5" /> Update SNMP Profile
                      </>
                    ) : (
                      <>
                        <Plus className="h-3.5 w-3.5" /> Save SNMP Profile
                      </>
                    )}
                  </button>
                </div>
              </form>

              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="pb-2 font-medium">Profile Name</th>
                      <th className="pb-2 font-medium">Version</th>
                      <th className="pb-2 font-medium">Port</th>
                      <th className="pb-2 font-medium">Credentials / Secret</th>
                      <th className="pb-2 text-right font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                    {(config?.snmp_profiles || []).map((p) => {
                      const isRevealed = Boolean(revealedProfileIds[p.id]);
                      return (
                        <tr key={p.id}>
                          <td className="py-2.5 font-sans font-semibold text-white">{p.name}</td>
                          <td className="py-2.5 uppercase text-sky-400">{p.version}</td>
                          <td className="py-2.5 text-slate-300">{p.port}</td>
                          <td className="py-2.5 text-slate-300">
                            <div className="flex items-center gap-2">
                              {p.version === "v3" ? (
                                <span>
                                  User: {p.v3_user} • Auth:{" "}
                                  {isRevealed ? p.v3_auth_key || "(none)" : "••••••••"} (
                                  {p.v3_auth_proto}) • Priv:{" "}
                                  {isRevealed ? p.v3_priv_key || "(none)" : "••••••••"} (
                                  {p.v3_priv_proto})
                                </span>
                              ) : (
                                <span>
                                  Community:{" "}
                                  <strong className="text-emerald-400">
                                    {isRevealed ? p.community : "••••••••"}
                                  </strong>
                                </span>
                              )}
                              <button
                                type="button"
                                onClick={() =>
                                  setRevealedProfileIds({
                                    ...revealedProfileIds,
                                    [p.id]: !isRevealed,
                                  })
                                }
                                className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-white"
                                title={isRevealed ? "Hide secret" : "Reveal secret"}
                              >
                                {isRevealed ? (
                                  <EyeOff className="h-3.5 w-3.5" />
                                ) : (
                                  <Eye className="h-3.5 w-3.5" />
                                )}
                              </button>
                            </div>
                          </td>
                          <td className="py-2.5 text-right">
                            <div className="flex justify-end gap-1.5">
                              <button
                                onClick={() => startEditingProfile(p)}
                                className="inline-flex items-center gap-1 rounded px-2 py-1 text-sky-400 hover:bg-sky-500/15"
                              >
                                <Edit3 className="h-3.5 w-3.5" /> Edit
                              </button>
                              <button
                                onClick={() => handleDeleteProfile(p.id)}
                                className="inline-flex items-center gap-1 rounded px-2 py-1 text-rose-400 hover:bg-rose-500/15"
                              >
                                <Trash2 className="h-3.5 w-3.5" /> Delete
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            {/* 3. Monitored SNMP Target Hosts (Add & Edit) */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-white">
                  Monitored SNMP Hosts (Assign Any SNMP Profile)
                </h2>
                {editingHostId && (
                  <span className="rounded border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-xs text-amber-300">
                    Editing Host #{editingHostId}
                  </span>
                )}
              </div>
              <p className="mt-0.5 text-xs text-slate-400">
                Add routers, switches, firewalls, or servers and link each one to any SNMP profile
                above.
              </p>

              <form onSubmit={handleSaveHost} className="mt-4 flex flex-wrap gap-2.5">
                <input
                  type="text"
                  placeholder="Host IP or Hostname (e.g. 10.1.3.253)"
                  value={newHostIp}
                  onChange={(e) => setNewHostIp(e.target.value)}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                  required
                />
                <input
                  type="text"
                  placeholder="Friendly Name (e.g. Core Switch)"
                  value={newHostLabel}
                  onChange={(e) => setNewHostLabel(e.target.value)}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs text-white"
                />
                <select
                  value={newHostProfileId}
                  onChange={(e) => setNewHostProfileId(Number(e.target.value))}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs text-white"
                >
                  {(config?.snmp_profiles || []).map((p) => (
                    <option key={p.id} value={p.id}>
                      Profile: {p.name} ({p.version})
                    </option>
                  ))}
                </select>
                {editingHostId && (
                  <button
                    type="button"
                    onClick={resetHostForm}
                    className="rounded-md border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-800"
                  >
                    Cancel
                  </button>
                )}
                <button
                  type="submit"
                  className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                >
                  {editingHostId ? (
                    <>
                      <Check className="h-3.5 w-3.5" /> Update SNMP Host
                    </>
                  ) : (
                    <>
                      <Plus className="h-3.5 w-3.5" /> Add SNMP Host
                    </>
                  )}
                </button>
              </form>

              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="pb-2 font-medium">Host IP</th>
                      <th className="pb-2 font-medium">Label</th>
                      <th className="pb-2 font-medium">Assigned SNMP Profile</th>
                      <th className="pb-2 text-right font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                    {(config?.snmp_targets || []).map((t) => (
                      <tr key={t.id}>
                        <td
                          onClick={() => openDevicePage(t.device_ip, t.device_id)}
                          className="cursor-pointer py-2.5 font-semibold text-emerald-400 underline"
                        >
                          <a
                            href={getDeviceHref(t.device_ip, t.device_id)}
                            onClick={(e) => {
                              e.stopPropagation();
                              if (isModifiedClick(e)) return;
                              e.preventDefault();
                              openDevicePage(t.device_ip, t.device_id);
                            }}
                            className="hover:text-emerald-300"
                          >
                            {t.device_ip}
                          </a>
                        </td>
                        <td className="py-2.5 font-sans text-slate-300">{t.label || "—"}</td>
                        <td className="py-2.5 font-sans text-sky-400">
                          {t.profile_name || "Default"} ({t.profile_version || "v2c"})
                        </td>
                        <td className="py-2.5 text-right">
                          <div className="flex justify-end gap-1.5">
                            <button
                              onClick={() => startEditingHost(t)}
                              className="inline-flex items-center gap-1 rounded px-2 py-1 text-sky-400 hover:bg-sky-500/15"
                            >
                              <Edit3 className="h-3.5 w-3.5" /> Edit
                            </button>
                            <button
                              onClick={() => handleDeleteHost(t.id)}
                              className="inline-flex items-center gap-1 rounded px-2 py-1 text-rose-400 hover:bg-rose-500/15"
                            >
                              <Trash2 className="h-3.5 w-3.5" /> Remove
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* 4. Optional UniFi Controllers */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <h2 className="text-sm font-semibold text-white">
                Optional UniFi Controllers / Gateways
              </h2>
              <p className="mt-0.5 text-xs text-slate-400">
                Completely optional. Add one or more UniFi OS Gateways / Controllers to pull WAN,
                PoE, and client telemetry.
              </p>

              <form onSubmit={handleAddUnifi} className="mt-4 flex flex-wrap gap-2.5">
                <input
                  type="text"
                  placeholder="Controller Label (e.g. UDM Pro)"
                  value={newUnifiName}
                  onChange={(e) => setNewUnifiName(e.target.value)}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs text-white"
                />
                <input
                  type="text"
                  placeholder="Controller IP / Host (e.g. 10.1.3.253)"
                  value={newUnifiHost}
                  onChange={(e) => setNewUnifiHost(e.target.value)}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                  required
                />
                <div className="relative">
                  <input
                    type={showUnifiKey ? "text" : "password"}
                    placeholder="UniFi X-API-KEY Secret"
                    value={newUnifiApiKey}
                    onChange={(e) => setNewUnifiApiKey(e.target.value)}
                    className="rounded-md border border-slate-700 bg-[#090D16] py-1.5 pr-8 pl-3 font-mono-tabular text-xs text-white"
                    required
                  />
                  <button
                    type="button"
                    onClick={() => setShowUnifiKey(!showUnifiKey)}
                    className="absolute top-1.5 right-2 text-slate-400 hover:text-slate-200"
                  >
                    {showUnifiKey ? (
                      <EyeOff className="h-3.5 w-3.5" />
                    ) : (
                      <Eye className="h-3.5 w-3.5" />
                    )}
                  </button>
                </div>
                <input
                  type="text"
                  placeholder="Site Slug (default)"
                  value={newUnifiSite}
                  onChange={(e) => setNewUnifiSite(e.target.value)}
                  className="w-32 rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                />
                <button
                  type="submit"
                  className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                >
                  <Plus className="h-3.5 w-3.5" /> Save Controller
                </button>
              </form>

              {(config?.unifi_controllers || []).length > 0 && (
                <div className="mt-4 overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="border-b border-slate-800 text-slate-400">
                        <th className="pb-2 font-medium">Name</th>
                        <th className="pb-2 font-medium">Host</th>
                        <th className="pb-2 font-medium">Site</th>
                        <th className="pb-2 text-right font-medium">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                      {config!.unifi_controllers.map((u) => (
                        <tr key={u.id}>
                          <td className="py-2.5 font-sans font-semibold text-white">{u.name}</td>
                          <td className="py-2.5 text-slate-300">{u.host}</td>
                          <td className="py-2.5 text-slate-400">{u.site_name}</td>
                          <td className="py-2.5 text-right">
                            <button
                              onClick={() => handleDeleteUnifi(u.id)}
                              className="inline-flex items-center gap-1 rounded px-2 py-1 text-rose-400 hover:bg-rose-500/15"
                            >
                              <Trash2 className="h-3.5 w-3.5" /> Remove
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* 5. Backend Server MIB Library & Custom MIB Upload (/app/mibs) */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <h2 className="text-sm font-semibold text-white">
                Server Backend MIB Library & Custom MIB Storage (<code>/app/mibs</code>)
              </h2>
              <p className="mt-0.5 text-xs text-slate-400">
                When any device is polled, NetDash fingerprints its <code>sysObjectID</code>{" "}
                (Enterprise PEN) and <code>sysDescr</code> against the backend MIB library to pull
                vendor-specific sensors, hardware serials, storage, wireless VAPs, and LLDP
                neighbors automatically.
              </p>

              <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-2">
                <div>
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-sky-400">
                    Stored Backend MIB Files ({storedMibs.length})
                  </h3>
                  <div className="space-y-2">
                    {storedMibs.map((m) => (
                      <div
                        key={m.filename}
                        className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs"
                      >
                        <div className="flex items-center justify-between">
                          <span className="font-mono-tabular font-semibold text-emerald-400">
                            {m.module_name}
                          </span>
                          <span className="font-mono-tabular text-[11px] text-slate-400">
                            {m.filename} • {m.object_count} OIDs
                            {m.enterprise_pen ? ` • PEN #${m.enterprise_pen}` : ""}
                          </span>
                        </div>
                        <p className="mt-1 text-[11px] text-slate-400">
                          {m.organization ? `${m.organization} — ` : ""}
                          {m.description}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>

                <div>
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-emerald-400">
                    Upload or Paste Additional Vendor MIB File (.mib)
                  </h3>
                  <form
                    onSubmit={handleUploadMib}
                    className="space-y-2.5 rounded-lg border border-slate-800 bg-[#090D16] p-3.5 text-xs"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        type="text"
                        placeholder="Filename (e.g. VENDOR-CUSTOM-MIB.mib)"
                        value={customMibFilename}
                        onChange={(e) => setCustomMibFilename(e.target.value)}
                        className="flex-1 rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 font-mono-tabular text-xs text-white"
                        required
                      />
                      <label className="cursor-pointer rounded-md border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-700">
                        Load .mib File
                        <input
                          type="file"
                          accept=".mib,.txt"
                          className="hidden"
                          onChange={(e) => {
                            const file = e.target.files?.[0];
                            if (!file) return;
                            setCustomMibFilename(file.name);
                            file.text().then((txt) => setCustomMibContent(txt));
                          }}
                        />
                      </label>
                    </div>
                    <textarea
                      rows={5}
                      placeholder="Paste SMIv2 MIB definition text (MY-MIB DEFINITIONS ::= BEGIN ... END)..."
                      value={customMibContent}
                      onChange={(e) => setCustomMibContent(e.target.value)}
                      className="w-full rounded-md border border-slate-700 bg-[#111827] p-2.5 font-mono-tabular text-xs text-white"
                      required
                    />
                    <div className="flex justify-end">
                      <button
                        type="submit"
                        className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                      >
                        <Plus className="h-3.5 w-3.5" /> Store MIB on Backend Server
                      </button>
                    </div>
                  </form>

                  <h3 className="mt-4 mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
                    Auto-Matched Device Vendor Profiles ({vendorCatalog.length})
                  </h3>
                  <div className="max-h-48 space-y-1.5 overflow-y-auto rounded-lg border border-slate-800 bg-[#090D16] p-2.5 text-xs">
                    {vendorCatalog.map((vc) => (
                      <div
                        key={vc.key}
                        className="flex items-center justify-between border-b border-slate-800/60 pb-1.5 last:border-b-0 last:pb-0"
                      >
                        <div>
                          <span className="font-semibold text-white">{vc.vendor_name}</span>{" "}
                          <span className="text-slate-400">({vc.category})</span>
                        </div>
                        <span className="font-mono-tabular text-[11px] text-sky-400">
                          {vc.mibs.slice(0, 3).join(", ")}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ==================== VIEW 7: SERVER SETTINGS, UPDATES & SERVICES ==================== */}
        {activeTab === "settings" && (
          <div className="space-y-6">
            {/* Top Bar */}
            <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 bg-[#090D16] text-emerald-400">
                  <Settings className="h-5 w-5" />
                </div>
                <div>
                  <h2 className="text-sm font-semibold text-white">
                    Server Settings & System Status
                  </h2>
                  <p className="text-xs text-slate-400">
                    Server hardware & runtime information, software updates, and background service
                    health & versions.
                  </p>
                </div>
              </div>
              <button
                onClick={() => fetchServerInfo(true)}
                disabled={loadingServerInfo}
                className="flex items-center gap-1.5 rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-800 disabled:opacity-50"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${loadingServerInfo ? "animate-spin" : ""}`} />
                <span>{loadingServerInfo ? "Refreshing..." : "Refresh Status"}</span>
              </button>
            </div>

            {settingsFeedback && (
              <div
                className={`flex items-center justify-between rounded-lg border px-4 py-2.5 text-xs ${
                  settingsFeedback.type === "ok"
                    ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                    : "border-rose-500/40 bg-rose-500/15 text-rose-300"
                }`}
              >
                <span>{settingsFeedback.text}</span>
                <button type="button" onClick={() => setSettingsFeedback(null)}>
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            )}

            {/* 1. Software Updates Panel */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <div className="flex flex-wrap items-center gap-2.5">
                    <DownloadCloud className="h-4 w-4 text-emerald-400" />
                    <h3 className="text-sm font-semibold text-white">Server Software Updates</h3>
                    {updateInfo?.update_available ? (
                      <>
                        <span className="rounded border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 font-mono-tabular text-[11px] font-medium text-amber-300">
                          Update Available ({updateInfo.commits_behind} update
                          {updateInfo.commits_behind === 1 ? "" : "s"})
                        </span>
                        {!installingUpdate && promptDismissedSha === updateInfo.remote_sha && (
                          <button
                            type="button"
                            onClick={openUpdateModal}
                            className="text-xs font-medium text-amber-300 underline decoration-amber-400/70 underline-offset-4 transition hover:text-amber-200"
                          >
                            What's New!
                          </button>
                        )}
                      </>
                    ) : (
                      <span className="rounded border border-emerald-500/30 bg-emerald-500/15 px-2 py-0.5 font-mono-tabular text-[11px] font-medium text-emerald-400">
                        Up to Date
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    New commits on GitHub are fetched in check-only mode and never auto-installed
                    until you explicitly choose to install them.
                  </p>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => fetchUpdateStatus(true)}
                    disabled={checkingUpdate || installingUpdate}
                    className="flex items-center gap-1.5 rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-800 disabled:opacity-50"
                  >
                    <RefreshCw
                      className={`h-3.5 w-3.5 ${checkingUpdate ? "animate-spin" : ""}`}
                    />
                    <span>{checkingUpdate ? "Checking GitHub..." : "Check for Updates"}</span>
                  </button>
                  {updateInfo?.update_available && (
                    <button
                      onClick={handleInstallUpdate}
                      disabled={installingUpdate}
                      className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
                    >
                      <DownloadCloud className="h-3.5 w-3.5" />
                      <span>
                        {installingUpdate ? "Installing Update..." : "Download & Install Now"}
                      </span>
                    </button>
                  )}
                </div>
              </div>

              <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-4">
                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs">
                  <div className="text-slate-400">Installed Version</div>
                  <div className="mt-1 font-mono-tabular font-semibold text-white">
                    {updateInfo?.current_version ||
                      serverInfo?.server?.git?.current_version ||
                      "—"}
                    {(updateInfo?.current_sha || serverInfo?.server?.git?.current_sha) && (
                      <span className="ml-1.5 text-[11px] font-normal text-slate-500">
                        ({updateInfo?.current_sha || serverInfo?.server?.git?.current_sha})
                      </span>
                    )}
                  </div>
                </div>
                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs">
                  <div className="text-slate-400">Latest Version (origin/main)</div>
                  <div className="mt-1 font-mono-tabular font-semibold text-emerald-400">
                    {updateInfo?.remote_version || updateInfo?.remote_sha || "—"}
                    {updateInfo?.remote_sha && (
                      <span className="ml-1.5 text-[11px] font-normal text-slate-500">
                        ({updateInfo.remote_sha})
                      </span>
                    )}
                  </div>
                </div>
                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs">
                  <div className="text-slate-400">Git Branch</div>
                  <div className="mt-1 font-mono-tabular text-slate-200">
                    {serverInfo?.server?.git?.branch || "main"}
                  </div>
                </div>
                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs">
                  <div className="text-slate-400">Last Checked</div>
                  <div className="mt-1 font-mono-tabular text-slate-300">
                    {updateInfo?.checked_at
                      ? new Date(updateInfo.checked_at).toLocaleTimeString()
                      : "—"}
                  </div>
                </div>
              </div>

              {serverInfo?.server?.git?.last_commit_subject && (
                <div className="mt-3 rounded-lg border border-slate-800 bg-[#090D16] px-3.5 py-2 text-xs text-slate-300">
                  <span className="text-slate-500">Current Release Note: </span>
                  <span>{serverInfo.server.git.last_commit_subject}</span>
                </div>
              )}

              {installingUpdate && (
                <div className="mt-4 flex items-center gap-3 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-300">
                  <RefreshCw className="h-4 w-4 shrink-0 animate-spin" />
                  <span>
                    Downloading commits from GitHub and rebuilding containers on host... This page
                    will reload automatically once complete.
                  </span>
                </div>
              )}
            </div>

            {/* 2. Internet Service Provider (ISP) Speed */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
                    <Activity className="h-4 w-4 text-emerald-400" />
                    Internet Service Provider (ISP) Speed
                  </h3>
                  <p className="mt-0.5 text-xs text-slate-400">
                    Set your provisioned ISP bandwidth in Mbps to lock the Overview traffic graph
                    Y-axis from <span className="font-mono-tabular text-slate-300">0</span> to your
                    configured ceiling (set 0 for auto-scaling).
                  </p>
                </div>
                <span className="rounded border border-slate-700 bg-[#090D16] px-2.5 py-1 font-mono-tabular text-xs text-emerald-400">
                  {ispCeilingMbps > 0
                    ? `Ceiling: ${ispCeilingMbps} Mbps`
                    : "Auto Scale"}
                </span>
              </div>

              <form
                onSubmit={handleSaveIspSettings}
                className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-4"
              >
                <div>
                  <label className="mb-1 block text-[11px] font-medium text-slate-400">
                    ISP Name (Optional)
                  </label>
                  <input
                    type="text"
                    placeholder="e.g. AT&T Fiber / Xfinity"
                    value={ispNameInput}
                    onChange={(e) => setIspNameInput(e.target.value)}
                    className="w-full rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 text-xs text-white"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] font-medium text-slate-400">
                    Download Speed (Mbps)
                  </label>
                  <input
                    type="number"
                    min={0}
                    max={1000000}
                    step="any"
                    placeholder="1000"
                    value={ispDownloadInput}
                    onChange={(e) => setIspDownloadInput(e.target.value)}
                    className="w-full rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                    required
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] font-medium text-slate-400">
                    Upload Speed (Mbps)
                  </label>
                  <input
                    type="number"
                    min={0}
                    max={1000000}
                    step="any"
                    placeholder="1000"
                    value={ispUploadInput}
                    onChange={(e) => setIspUploadInput(e.target.value)}
                    className="w-full rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                    required
                  />
                </div>
                <div className="flex items-end">
                  <button
                    type="submit"
                    disabled={savingIsp}
                    className="flex w-full items-center justify-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
                  >
                    <Check className="h-3.5 w-3.5" />
                    <span>{savingIsp ? "Saving..." : "Save ISP Speed"}</span>
                  </button>
                </div>
              </form>

              <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[11px]">
                <span className="mr-1 text-slate-500">Quick Symmetrical Presets:</span>
                {[100, 300, 500, 1000, 2500, 5000, 10000].map((mbps) => (
                  <button
                    key={mbps}
                    type="button"
                    onClick={() => {
                      setIspDownloadInput(String(mbps));
                      setIspUploadInput(String(mbps));
                    }}
                    className="rounded border border-slate-800 bg-[#090D16] px-2 py-0.5 font-mono-tabular text-slate-300 hover:border-slate-700 hover:bg-slate-800 hover:text-white"
                  >
                    {mbps} Mbps
                  </button>
                ))}
              </div>
            </div>

            {/* 2b. Overview Dashboard Graph Source Selector */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
                    <Network className="h-4 w-4 text-sky-400" />
                    Overview Dashboard Graph Source
                  </h3>
                  <p className="mt-0.5 text-xs text-slate-400">
                    Choose <strong className="text-slate-200">Auto</strong> (UniFi WAN or all SNMP
                    interfaces) or pick one or more discovered devices and specific interfaces (or all
                    ports) to display on the main Overview Dashboard graph.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span
                    className={`rounded border px-2.5 py-1 font-mono-tabular text-xs ${
                      overviewGraphSource?.mode === "custom"
                        ? "border-sky-500/40 bg-sky-500/10 text-sky-300"
                        : "border-slate-700 bg-[#090D16] text-emerald-400"
                    }`}
                  >
                    {overviewGraphSource?.summary || "Auto"}
                  </span>
                  <button
                    type="button"
                    onClick={fetchOverviewGraphOptions}
                    disabled={loadingOverviewGraphOptions}
                    className="flex items-center gap-1.5 rounded-md border border-slate-700 bg-[#090D16] px-2.5 py-1 text-xs font-medium text-slate-200 hover:bg-slate-800 disabled:opacity-50"
                  >
                    <RefreshCw
                      className={`h-3.5 w-3.5 ${loadingOverviewGraphOptions ? "animate-spin" : ""}`}
                    />
                    Refresh Devices
                  </button>
                </div>
              </div>

              {/* Mode Toggle */}
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setOverviewGraphModeInput("auto")}
                  className={`rounded-md border px-3 py-1.5 text-xs font-medium transition ${
                    overviewGraphModeInput === "auto"
                      ? "border-emerald-500/50 bg-emerald-600 text-white"
                      : "border-slate-700 bg-[#090D16] text-slate-300 hover:bg-slate-800"
                  }`}
                >
                  Auto (UniFi WAN / All SNMP)
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setOverviewGraphModeInput("custom");
                    if (overviewGraphOptions.length === 0) fetchOverviewGraphOptions();
                  }}
                  className={`rounded-md border px-3 py-1.5 text-xs font-medium transition ${
                    overviewGraphModeInput === "custom"
                      ? "border-sky-500/50 bg-sky-600 text-white"
                      : "border-slate-700 bg-[#090D16] text-slate-300 hover:bg-slate-800"
                  }`}
                >
                  Custom Devices &amp; Interfaces
                </button>
              </div>

              {overviewGraphModeInput === "custom" && (
                <div className="mt-4 space-y-3">
                  {/* Selected Summary Chips */}
                  <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3">
                    <div className="mb-2 flex items-center justify-between gap-2 text-[11px] text-slate-400">
                      <span>
                        Selected Sources ({overviewGraphSelectionsInput.length} device
                        {overviewGraphSelectionsInput.length === 1 ? "" : "s"})
                      </span>
                      {overviewGraphSelectionsInput.length > 0 && (
                        <button
                          type="button"
                          onClick={() => setOverviewGraphSelectionsInput([])}
                          className="text-rose-400 hover:text-rose-300"
                        >
                          Clear All
                        </button>
                      )}
                    </div>
                    {overviewGraphSelectionsInput.length === 0 ? (
                      <div className="text-xs text-slate-500">
                        No devices selected yet. Pick one or more devices/interfaces below.
                      </div>
                    ) : (
                      <div className="flex flex-wrap gap-1.5">
                        {overviewGraphSelectionsInput.map((sel) => {
                          const devInfo = overviewGraphOptions.find(
                            (d) => d.device_ip === sel.device_ip,
                          );
                          const devLabel =
                            devInfo?.hostname && devInfo.hostname !== sel.device_ip
                              ? `${devInfo.hostname} (${sel.device_ip})`
                              : sel.device_ip;
                          if (sel.if_indices.length === 0) {
                            return (
                              <span
                                key={`${sel.device_ip}-all`}
                                className="inline-flex items-center gap-1.5 rounded border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 font-mono-tabular text-[11px] text-sky-200"
                              >
                                {devLabel}: All Interfaces
                                <button
                                  type="button"
                                  onClick={() => removeOverviewSelectionChip(sel.device_ip)}
                                  className="text-slate-400 hover:text-white"
                                  aria-label={`Remove ${sel.device_ip}`}
                                >
                                  <X className="h-3 w-3" />
                                </button>
                              </span>
                            );
                          }
                          return sel.if_indices.map((ifIdx) => {
                            const iface = devInfo?.interfaces.find((i) => i.if_index === ifIdx);
                            return (
                              <span
                                key={`${sel.device_ip}-${ifIdx}`}
                                className="inline-flex items-center gap-1.5 rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 font-mono-tabular text-[11px] text-emerald-200"
                              >
                                {devLabel} • #{ifIdx} {iface?.if_name || "if"}
                                <button
                                  type="button"
                                  onClick={() =>
                                    removeOverviewSelectionChip(sel.device_ip, ifIdx)
                                  }
                                  className="text-slate-400 hover:text-white"
                                  aria-label={`Remove ${sel.device_ip} interface ${ifIdx}`}
                                >
                                  <X className="h-3 w-3" />
                                </button>
                              </span>
                            );
                          });
                        })}
                      </div>
                    )}
                  </div>

                  {/* Filter Bar */}
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="relative min-w-[240px] flex-1">
                      <Search className="pointer-events-none absolute top-2 left-2.5 h-3.5 w-3.5 text-slate-500" />
                      <input
                        type="text"
                        value={overviewGraphSearch}
                        onChange={(e) => setOverviewGraphSearch(e.target.value)}
                        placeholder="Filter discovered devices by IP, hostname, vendor, or interface..."
                        className="w-full rounded-md border border-slate-700 bg-[#090D16] py-1.5 pr-3 pl-8 text-xs text-white placeholder-slate-500"
                      />
                    </div>
                    <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-300">
                      <input
                        type="checkbox"
                        checked={overviewGraphSnmpOnly}
                        onChange={(e) => setOverviewGraphSnmpOnly(e.target.checked)}
                        className="rounded border-slate-600 bg-[#090D16] text-emerald-500"
                      />
                      <span>SNMP-polled interfaces only</span>
                    </label>
                  </div>

                  {/* Scrollable Device & Interface Checklist */}
                  <div className="max-h-[420px] space-y-2 overflow-y-auto pr-1">
                    {(() => {
                      const q = overviewGraphSearch.trim().toLowerCase();
                      const visibleDevices = overviewGraphOptions.filter((dev) => {
                        if (overviewGraphSnmpOnly && dev.interfaces.length === 0) return false;
                        if (!q) return true;
                        const devMatch =
                          dev.device_ip.toLowerCase().includes(q) ||
                          (dev.hostname || "").toLowerCase().includes(q) ||
                          (dev.vendor || "").toLowerCase().includes(q);
                        if (devMatch) return true;
                        return dev.interfaces.some(
                          (intf) =>
                            intf.if_name.toLowerCase().includes(q) ||
                            intf.if_alias.toLowerCase().includes(q) ||
                            intf.if_descr.toLowerCase().includes(q),
                        );
                      });
                      if (visibleDevices.length === 0) {
                        return (
                          <div className="rounded-lg border border-slate-800 bg-[#090D16] p-4 text-center text-xs text-slate-500">
                            {loadingOverviewGraphOptions
                              ? "Loading discovered devices and SNMP interfaces..."
                              : "No discovered devices match your filter."}
                          </div>
                        );
                      }
                      return visibleDevices.map((dev) => {
                        const selObj = overviewGraphSelectionsInput.find(
                          (s) => s.device_ip === dev.device_ip,
                        );
                        const isSelected = Boolean(selObj);
                        const isExpanded =
                          isSelected || Boolean(expandedOverviewDeviceIps[dev.device_ip]);
                        const ifQuery = (interfaceSearchByIp[dev.device_ip] || "")
                          .trim()
                          .toLowerCase();
                        const shownInterfaces = ifQuery
                          ? dev.interfaces.filter(
                              (intf) =>
                                intf.if_name.toLowerCase().includes(ifQuery) ||
                                intf.if_alias.toLowerCase().includes(ifQuery) ||
                                intf.if_descr.toLowerCase().includes(ifQuery) ||
                                `#${intf.if_index}`.includes(ifQuery),
                            )
                          : dev.interfaces;

                        return (
                          <div
                            key={dev.device_ip}
                            className={`rounded-lg border transition ${
                              isSelected
                                ? "border-sky-500/40 bg-[#090D16]"
                                : "border-slate-800 bg-[#090D16]/70"
                            }`}
                          >
                            <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5">
                              <div className="flex min-w-0 items-center gap-2.5">
                                <input
                                  type="checkbox"
                                  checked={isSelected}
                                  onChange={() => toggleOverviewDevice(dev.device_ip)}
                                  className="h-3.5 w-3.5 rounded border-slate-600 bg-[#111827] text-sky-500"
                                />
                                <span
                                  className={`h-2 w-2 shrink-0 rounded-full ${
                                    dev.is_online ? "bg-emerald-400" : "bg-slate-600"
                                  }`}
                                />
                                <span className="font-mono-tabular text-xs font-semibold text-white">
                                  {dev.device_ip}
                                </span>
                                {dev.hostname && dev.hostname !== dev.device_ip && (
                                  <span className="truncate text-xs text-slate-300">
                                    {dev.hostname}
                                  </span>
                                )}
                                {dev.vendor && (
                                  <span className="hidden truncate text-[11px] text-slate-500 sm:inline">
                                    • {dev.vendor}
                                  </span>
                                )}
                              </div>

                              <div className="flex items-center gap-2">
                                <span
                                  className={`rounded px-2 py-0.5 font-mono-tabular text-[10px] ${
                                    dev.interfaces.length > 0
                                      ? "bg-emerald-500/10 text-emerald-300"
                                      : "bg-slate-800 text-slate-400"
                                  }`}
                                >
                                  {dev.interfaces.length > 0
                                    ? `${dev.interfaces.length} interface${
                                        dev.interfaces.length === 1 ? "" : "s"
                                      }`
                                    : "No SNMP interfaces yet"}
                                </span>
                                <button
                                  type="button"
                                  onClick={() =>
                                    setExpandedOverviewDeviceIps((prev) => ({
                                      ...prev,
                                      [dev.device_ip]: !prev[dev.device_ip],
                                    }))
                                  }
                                  className="rounded border border-slate-700 bg-[#111827] px-2 py-0.5 text-[11px] text-slate-300 hover:bg-slate-800"
                                >
                                  {isExpanded ? "Hide" : "Expand"}
                                </button>
                              </div>
                            </div>

                            {isExpanded && (
                              <div className="border-t border-slate-800/80 px-3 py-2.5">
                                {dev.interfaces.length > 0 ? (
                                  <div className="space-y-2.5">
                                    <div className="flex flex-wrap items-center justify-between gap-2">
                                      <div className="flex items-center gap-1.5">
                                        <button
                                          type="button"
                                          onClick={() =>
                                            setOverviewDeviceAllInterfaces(dev.device_ip)
                                          }
                                          className={`rounded border px-2 py-0.5 text-[11px] font-medium transition ${
                                            isSelected && (selObj?.if_indices.length ?? 0) === 0
                                              ? "border-sky-500/50 bg-sky-600 text-white"
                                              : "border-slate-700 bg-[#111827] text-slate-300 hover:bg-slate-800"
                                          }`}
                                        >
                                          All Interfaces
                                        </button>
                                        <span
                                          className={`rounded border px-2 py-0.5 text-[11px] font-medium ${
                                            isSelected && (selObj?.if_indices.length ?? 0) > 0
                                              ? "border-emerald-500/50 bg-emerald-600/20 text-emerald-300"
                                              : "border-slate-800 bg-[#111827] text-slate-400"
                                          }`}
                                        >
                                          Specific Interface(s):{" "}
                                          {selObj ? selObj.if_indices.length : 0}
                                        </span>
                                      </div>

                                      {dev.interfaces.length > 8 && (
                                        <input
                                          type="text"
                                          value={interfaceSearchByIp[dev.device_ip] || ""}
                                          onChange={(e) =>
                                            setInterfaceSearchByIp((prev) => ({
                                              ...prev,
                                              [dev.device_ip]: e.target.value,
                                            }))
                                          }
                                          placeholder="Filter ports..."
                                          className="w-44 rounded border border-slate-700 bg-[#111827] px-2 py-0.5 text-[11px] text-white placeholder-slate-500"
                                        />
                                      )}
                                    </div>

                                    <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                                      {shownInterfaces.map((intf) => {
                                        const ifChecked = Boolean(
                                          selObj?.if_indices.includes(intf.if_index),
                                        );
                                        return (
                                          <label
                                            key={intf.if_index}
                                            className={`flex cursor-pointer items-center justify-between gap-2 rounded border px-2.5 py-1.5 text-[11px] transition ${
                                              ifChecked
                                                ? "border-emerald-500/40 bg-emerald-500/10"
                                                : "border-slate-800 bg-[#111827]/70 hover:border-slate-700"
                                            }`}
                                          >
                                            <div className="flex min-w-0 items-center gap-2">
                                              <input
                                                type="checkbox"
                                                checked={ifChecked}
                                                onChange={() =>
                                                  toggleOverviewDeviceInterface(
                                                    dev.device_ip,
                                                    intf.if_index,
                                                  )
                                                }
                                                className="h-3 w-3 rounded border-slate-600 bg-[#090D16] text-emerald-500"
                                              />
                                              <span className="font-mono-tabular text-slate-400">
                                                #{intf.if_index}
                                              </span>
                                              <span className="truncate font-mono-tabular font-semibold text-white">
                                                {intf.if_name}
                                              </span>
                                              {(intf.if_alias || intf.if_descr) && (
                                                <span className="truncate text-slate-400">
                                                  ({intf.if_alias || intf.if_descr})
                                                </span>
                                              )}
                                            </div>
                                            <div className="flex shrink-0 items-center gap-1.5 font-mono-tabular">
                                              {intf.if_speed_bps > 0 && (
                                                <span className="text-[10px] text-slate-400">
                                                  {formatBps(intf.if_speed_bps)}
                                                </span>
                                              )}
                                              <span className="text-[10px] text-emerald-400">
                                                ↓{formatBps(intf.in_bps)}
                                              </span>
                                              <span className="text-[10px] text-sky-400">
                                                ↑{formatBps(intf.out_bps)}
                                              </span>
                                              <span
                                                className={`rounded px-1 py-0.2 text-[10px] ${
                                                  intf.if_oper_status === 1
                                                    ? "bg-emerald-500/15 text-emerald-400"
                                                    : "bg-slate-800 text-slate-500"
                                                }`}
                                              >
                                                {intf.if_oper_status === 1 ? "UP" : "DOWN"}
                                              </span>
                                            </div>
                                          </label>
                                        );
                                      })}
                                    </div>
                                  </div>
                                ) : (
                                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400">
                                    <span>
                                      No SNMP interfaces discovered yet for this device.
                                    </span>
                                    <a
                                      href={getDeviceHref(dev.device_ip, dev.device_id)}
                                      onClick={(e) => {
                                        if (isModifiedClick(e)) return;
                                        e.preventDefault();
                                        openDevicePage(dev.device_ip, dev.device_id);
                                      }}
                                      className="rounded border border-sky-500/30 bg-sky-500/10 px-2.5 py-1 text-[11px] font-medium text-sky-300 hover:bg-sky-500/20"
                                    >
                                      Open Device to Enable SNMP →
                                    </a>
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      });
                    })()}
                  </div>
                </div>
              )}

              <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-800/80 pt-3">
                <span className="text-[11px] text-slate-400">
                  {overviewGraphModeInput === "auto"
                    ? "Auto mode tracks UniFi Gateway WAN when enabled, or all non-loopback SNMP ports."
                    : `Custom mode sums live and historical RX/TX across ${overviewGraphSelectionsInput.length} selected device${
                        overviewGraphSelectionsInput.length === 1 ? "" : "s"
                      }.`}
                </span>
                <button
                  type="button"
                  onClick={handleSaveOverviewGraphSettings}
                  disabled={
                    savingOverviewGraph ||
                    (overviewGraphModeInput === "custom" &&
                      overviewGraphSelectionsInput.length === 0)
                  }
                  className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
                >
                  <Check className="h-3.5 w-3.5" />
                  <span>
                    {savingOverviewGraph ? "Saving..." : "Save Overview Graph Source"}
                  </span>
                </button>
              </div>
            </div>

            {/* 3. Background Services & Versions */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
                    <Server className="h-4 w-4 text-sky-400" />
                    Background Services &amp; Versions
                  </h3>
                  <p className="mt-0.5 text-xs text-slate-400">
                    Live health checks, container controls (start/stop/restart), logs, and probe
                    latency across the NetDash stack.
                  </p>
                </div>
                <span className="rounded border border-slate-700 bg-[#090D16] px-2.5 py-1 font-mono-tabular text-xs text-emerald-400">
                  {serverInfo?.services
                    ? `${serverInfo.services.filter((s) => s.status === "running").length} / ${
                        serverInfo.services.length
                      } Running`
                    : "Loading..."}
                </span>
              </div>

              {serviceActionBanner && (
                <div
                  className={`mb-3 flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs ${
                    serviceActionBanner.type === "ok"
                      ? "border-emerald-500/35 bg-emerald-500/10 text-emerald-300"
                      : "border-rose-500/35 bg-rose-500/10 text-rose-300"
                  }`}
                >
                  <span>{serviceActionBanner.text}</span>
                  <button
                    type="button"
                    onClick={() => setServiceActionBanner(null)}
                    className="rounded p-0.5 opacity-80 hover:opacity-100"
                    aria-label="Dismiss service message"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}

              {serverInfo?.server && serverInfo.server.docker_control_available === false && (
                <div className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
                  Docker socket control is not active yet. Mount{" "}
                  <code className="font-mono-tabular">/var/run/docker.sock</code> on{" "}
                  <code className="font-mono-tabular">netdash-api</code> (included in this update)
                  to enable Start / Stop / Restart controls and container log viewing.
                </div>
              )}

              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="pb-2.5 font-medium">Status</th>
                      <th className="pb-2.5 font-medium">Service</th>
                      <th className="pb-2.5 font-medium">Container</th>
                      <th className="pb-2.5 font-medium">Version</th>
                      <th className="pb-2.5 font-medium">Uptime</th>
                      <th className="pb-2.5 font-medium">Latency</th>
                      <th className="pb-2.5 font-medium">Runtime Details</th>
                      <th className="pb-2.5 text-right font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                    {!serverInfo?.services ? (
                      <tr>
                        <td colSpan={8} className="py-8 text-center font-sans text-slate-500">
                          Probing background services...
                        </td>
                      </tr>
                    ) : (
                      serverInfo.services.map((svc) => {
                        const badgeCls =
                          svc.status === "running"
                            ? "bg-emerald-500/15 text-emerald-400"
                            : svc.status === "degraded"
                              ? "bg-amber-500/15 text-amber-400"
                              : "bg-rose-500/15 text-rose-400";
                        const dotCls =
                          svc.status === "running"
                            ? "bg-emerald-400"
                            : svc.status === "degraded"
                              ? "bg-amber-400"
                              : "bg-rose-400";
                        const busyAction = serviceActionBusy[svc.id] || null;
                        const dockerReady = Boolean(
                          svc.can_control ?? serverInfo?.server?.docker_control_available,
                        );
                        const canStart =
                          dockerReady && !busyAction && svc.status !== "running";
                        const canStop =
                          dockerReady &&
                          !busyAction &&
                          (svc.status !== "stopped" || svc.docker_state === "restarting");
                        const canRestart = dockerReady && !busyAction;
                        return (
                          <tr key={svc.id} className="hover:bg-slate-800/30">
                            <td className="py-3 pr-3">
                              <span
                                className={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[11px] font-medium uppercase ${badgeCls}`}
                              >
                                <span className={`h-1.5 w-1.5 rounded-full ${dotCls}`} />
                                {svc.status}
                              </span>
                            </td>
                            <td className="py-3 pr-3 font-sans font-semibold text-white">
                              {svc.name}
                            </td>
                            <td className="py-3 pr-3 text-slate-400">{svc.container}</td>
                            <td className="py-3 pr-3 text-sky-300">{svc.version}</td>
                            <td className="py-3 pr-3 text-slate-300">
                              {svc.uptime_sec != null ? formatUptime(svc.uptime_sec) : "—"}
                            </td>
                            <td className="py-3 pr-3 text-slate-400">
                              {svc.latency_ms != null ? `${svc.latency_ms.toFixed(1)} ms` : "—"}
                            </td>
                            <td className="py-3 pr-3 font-sans text-slate-300">{svc.detail}</td>
                            <td className="py-3 text-right font-sans">
                              <div className="inline-flex items-center justify-end gap-1">
                                <button
                                  type="button"
                                  disabled={!canStart}
                                  onClick={() => handleControlService(svc, "start")}
                                  title={
                                    dockerReady
                                      ? `Start ${svc.container}`
                                      : "Docker socket control unavailable"
                                  }
                                  className="inline-flex items-center gap-1 rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-[11px] font-medium text-emerald-300 transition hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                                >
                                  <Play className="h-3 w-3" />
                                  <span>{busyAction === "start" ? "..." : "Start"}</span>
                                </button>
                                <button
                                  type="button"
                                  disabled={!canStop}
                                  onClick={() => handleControlService(svc, "stop")}
                                  title={
                                    dockerReady
                                      ? svc.critical
                                        ? `Stop ${svc.container} (critical service)`
                                        : `Stop ${svc.container}`
                                      : "Docker socket control unavailable"
                                  }
                                  className="inline-flex items-center gap-1 rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[11px] font-medium text-rose-300 transition hover:bg-rose-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                                >
                                  <Square className="h-3 w-3" />
                                  <span>{busyAction === "stop" ? "..." : "Stop"}</span>
                                </button>
                                <button
                                  type="button"
                                  disabled={!canRestart}
                                  onClick={() => handleControlService(svc, "restart")}
                                  title={
                                    dockerReady
                                      ? `Restart ${svc.container}`
                                      : "Docker socket control unavailable"
                                  }
                                  className="inline-flex items-center gap-1 rounded border border-sky-500/30 bg-sky-500/10 px-2 py-1 text-[11px] font-medium text-sky-300 transition hover:bg-sky-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                                >
                                  <RefreshCw
                                    className={`h-3 w-3 ${
                                      busyAction === "restart" ? "animate-spin" : ""
                                    }`}
                                  />
                                  <span>{busyAction === "restart" ? "..." : "Restart"}</span>
                                </button>
                                <button
                                  type="button"
                                  disabled={!dockerReady}
                                  onClick={() => handleViewServiceLogs(svc)}
                                  title={
                                    dockerReady
                                      ? `View recent logs for ${svc.container}`
                                      : "Docker socket control unavailable"
                                  }
                                  className="inline-flex items-center gap-1 rounded border border-slate-700 bg-[#090D16] px-2 py-1 text-[11px] font-medium text-slate-300 transition hover:border-slate-600 hover:bg-slate-800 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
                                >
                                  <Terminal className="h-3 w-3" />
                                  <span>Logs</span>
                                </button>
                              </div>
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>

              {viewingLogsService && (
                <div className="mt-4 rounded-lg border border-slate-800 bg-[#090D16] p-3.5">
                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2 text-xs">
                      <Terminal className="h-3.5 w-3.5 text-sky-400" />
                      <span className="font-semibold text-white">{viewingLogsService.name}</span>
                      <span className="font-mono-tabular text-[11px] text-slate-400">
                        ({viewingLogsService.container} • last 100 lines)
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        disabled={loadingServiceLogs}
                        onClick={() =>
                          handleViewServiceLogs({
                            id: viewingLogsService.id,
                            name: viewingLogsService.name,
                            container: viewingLogsService.container,
                            status: "running",
                            version: "",
                            uptime_sec: null,
                            latency_ms: null,
                            detail: "",
                          })
                        }
                        className="inline-flex items-center gap-1 rounded border border-slate-700 bg-[#111827] px-2 py-1 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-50"
                      >
                        <RefreshCw
                          className={`h-3 w-3 ${loadingServiceLogs ? "animate-spin" : ""}`}
                        />
                        <span>Refresh</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setViewingLogsService(null);
                          setServiceLogsText("");
                        }}
                        className="inline-flex items-center gap-1 rounded border border-slate-700 bg-[#111827] px-2 py-1 text-[11px] text-slate-400 hover:bg-slate-800 hover:text-white"
                      >
                        <X className="h-3 w-3" />
                        <span>Close</span>
                      </button>
                    </div>
                  </div>
                  <pre className="max-h-72 overflow-auto rounded border border-slate-800/80 bg-[#060911] p-3 font-mono-tabular text-[11px] leading-relaxed text-slate-300 whitespace-pre-wrap break-all">
                    {loadingServiceLogs && !serviceLogsText
                      ? `Loading recent logs for ${viewingLogsService.container}...`
                      : serviceLogsText || "(No container output yet)"}
                  </pre>
                </div>
              )}

              {/* Worker Collector Sub-Loops */}
              {serverInfo?.services
                ?.find((s) => s.id === "worker")
                ?.sub_services?.length ? (
                <div className="mt-4 border-t border-slate-800 pt-4">
                  <p className="mb-2.5 text-xs font-semibold uppercase tracking-wider text-slate-400">
                    Background Worker Collector Loops
                  </p>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                    {serverInfo.services
                      .find((s) => s.id === "worker")!
                      .sub_services!.map((loop) => {
                        const isOk = loop.status === "running" || loop.status === "scanning";
                        return (
                          <div
                            key={loop.key}
                            className="rounded-lg border border-slate-800 bg-[#090D16] p-3 text-xs"
                          >
                            <div className="flex items-center justify-between">
                              <span className="font-semibold text-white">{loop.name}</span>
                              <span
                                className={`rounded px-1.5 py-0.5 font-mono-tabular text-[10px] uppercase ${
                                  isOk
                                    ? "bg-emerald-500/15 text-emerald-400"
                                    : loop.status === "error"
                                      ? "bg-rose-500/15 text-rose-400"
                                      : "bg-slate-800 text-slate-400"
                                }`}
                              >
                                {loop.status}
                              </span>
                            </div>
                            <div className="mt-1.5 font-mono-tabular text-[11px] text-slate-400">
                              Interval: {loop.interval_sec ? `${loop.interval_sec}s` : "—"} • Last:{" "}
                              {loop.last_run
                                ? new Date(loop.last_run).toLocaleTimeString()
                                : "Pending"}
                            </div>
                            <div className="mt-1 text-[11px] text-slate-300">
                              {loop.detail || "—"}
                            </div>
                          </div>
                        );
                      })}
                  </div>
                </div>
              ) : null}
            </div>

            {/* 3. Server Information & Host Footprint */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <h3 className="mb-4 flex items-center gap-2 text-sm font-semibold text-white">
                <Cpu className="h-4 w-4 text-emerald-400" />
                Server Information & Runtime Environment
              </h3>

              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3.5 text-xs">
                  <div className="text-slate-400">Hostname & Architecture</div>
                  <div className="mt-1 font-mono-tabular font-semibold text-white">
                    {serverInfo?.server?.hostname || "—"}
                  </div>
                  <div className="mt-0.5 text-[11px] text-slate-400">
                    {serverInfo?.server?.architecture || "—"} • Python{" "}
                    {serverInfo?.server?.python_version || "—"}
                  </div>
                </div>

                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3.5 text-xs">
                  <div className="text-slate-400">Operating System & Kernel</div>
                  <div className="mt-1 font-semibold text-white">
                    {serverInfo?.server?.os_name || "—"}
                  </div>
                  <div className="mt-0.5 font-mono-tabular text-[11px] text-slate-400">
                    Kernel {serverInfo?.server?.kernel || "—"}
                  </div>
                </div>

                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3.5 text-xs">
                  <div className="text-slate-400">CPU & Load Average</div>
                  <div className="mt-1 font-mono-tabular font-semibold text-white">
                    {serverInfo?.server?.cpu_cores ?? "—"} vCPU Cores
                  </div>
                  <div className="mt-0.5 font-mono-tabular text-[11px] text-slate-400">
                    Load:{" "}
                    {serverInfo?.server?.load_avg
                      ? serverInfo.server.load_avg.join(" / ")
                      : "—"}
                  </div>
                </div>

                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3.5 text-xs">
                  <div className="flex items-center justify-between text-slate-400">
                    <span>API Uptime & DB Storage</span>
                    <Clock className="h-3.5 w-3.5 text-slate-500" />
                  </div>
                  <div className="mt-1 font-mono-tabular font-semibold text-white">
                    {serverInfo?.server
                      ? formatUptime(serverInfo.server.api_uptime_sec)
                      : "—"}
                  </div>
                  <div className="mt-0.5 flex items-center gap-1 font-mono-tabular text-[11px] text-sky-400">
                    <Database className="h-3 w-3" />
                    <span>
                      DB:{" "}
                      {serverInfo?.server?.db_size_bytes != null
                        ? formatBytes(serverInfo.server.db_size_bytes)
                        : "—"}
                    </span>
                  </div>
                </div>
              </div>

              {/* Memory & Disk Bars */}
              <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3.5 text-xs">
                  <div className="mb-1.5 flex items-center justify-between font-mono-tabular">
                    <span className="font-sans font-medium text-slate-300">Host Memory (RAM)</span>
                    <span className="text-slate-400">
                      {serverInfo?.server?.memory?.total_bytes != null
                        ? `${formatBytes(serverInfo.server.memory.used_bytes)} / ${formatBytes(
                            serverInfo.server.memory.total_bytes,
                          )} (${serverInfo.server.memory.usage_pct}%)`
                        : "Unavailable"}
                    </span>
                  </div>
                  <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
                    <div
                      style={{
                        width: `${Math.min(100, serverInfo?.server?.memory?.usage_pct || 0)}%`,
                      }}
                      className={`h-full rounded-full ${
                        (serverInfo?.server?.memory?.usage_pct || 0) >= 90
                          ? "bg-rose-500"
                          : (serverInfo?.server?.memory?.usage_pct || 0) >= 75
                            ? "bg-amber-500"
                            : "bg-sky-500"
                      }`}
                    />
                  </div>
                </div>

                <div className="rounded-lg border border-slate-800 bg-[#090D16] p-3.5 text-xs">
                  <div className="mb-1.5 flex items-center justify-between font-mono-tabular">
                    <span className="font-sans font-medium text-slate-300">Root Disk Usage</span>
                    <span className="text-slate-400">
                      {serverInfo?.server?.disk?.total_bytes != null
                        ? `${formatBytes(serverInfo.server.disk.used_bytes)} / ${formatBytes(
                            serverInfo.server.disk.total_bytes,
                          )} (${serverInfo.server.disk.usage_pct}%)`
                        : "Unavailable"}
                    </span>
                  </div>
                  <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
                    <div
                      style={{
                        width: `${Math.min(100, serverInfo?.server?.disk?.usage_pct || 0)}%`,
                      }}
                      className={`h-full rounded-full ${
                        (serverInfo?.server?.disk?.usage_pct || 0) >= 90
                          ? "bg-rose-500"
                          : (serverInfo?.server?.disk?.usage_pct || 0) >= 75
                            ? "bg-amber-500"
                            : "bg-emerald-500"
                      }`}
                    />
                  </div>
                </div>
              </div>

              {/* Runtime Server Configuration Summary */}
              {serverInfo?.server?.config && (
                <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
                  {[
                    { label: "Web UI Port", val: `:${serverInfo.server.config.web_port}` },
                    {
                      label: "Syslog Port",
                      val: `:${serverInfo.server.config.syslog_port} (UDP/TCP)`,
                    },
                    {
                      label: "SNMP Interval",
                      val: `${serverInfo.server.config.snmp_poll_interval_sec}s`,
                    },
                    {
                      label: "Scan Interval",
                      val: `${serverInfo.server.config.subnet_scan_minutes}m`,
                    },
                    {
                      label: "SNMP Retention",
                      val: `${serverInfo.server.config.snmp_raw_retention_days}d raw`,
                    },
                    {
                      label: "Syslog Retention",
                      val: `${serverInfo.server.config.log_retention_days}d`,
                    },
                    {
                      label: "Backend MIBs",
                      val: `${serverInfo.server.config.loaded_mib_files} files`,
                    },
                  ].map((item) => (
                    <div
                      key={item.label}
                      className="rounded-lg border border-slate-800 bg-[#090D16] p-2.5 text-xs"
                    >
                      <div className="text-[11px] text-slate-400">{item.label}</div>
                      <div className="mt-0.5 font-mono-tabular font-semibold text-slate-200">
                        {item.val}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
          </>
        )}
      </main>
    </div>
  );
};

export default App;
