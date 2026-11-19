import React, { useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Cpu,
  DownloadCloud,
  KeyRound,
  Network,
  Play,
  Plus,
  RefreshCw,
  Router,
  Search,
  Server,
  Settings,
  ShieldCheck,
  Terminal,
  Thermometer,
  Trash2,
  X,
  Zap,
} from "lucide-react";

type TabKey = "pulse" | "snmp" | "scanner" | "syslog" | "settings";

interface GitCommitInfo {
  sha: string;
  subject: string;
  author: string;
  date: string;
}

interface SystemUpdateStatus {
  update_available: boolean;
  current_sha: string;
  remote_sha: string;
  commits_behind: number;
  commits: GitCommitInfo[];
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

interface AllConfigPayload {
  onboarding: {
    completed: boolean;
    skipped: boolean;
  };
  snmp_profiles: SNMPProfile[];
  snmp_targets: SNMPTargetConfig[];
  subnets: SubnetConfig[];
  unifi_controllers: UniFiControllerConfig[];
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

interface OverviewPayload {
  gateway: GatewayMetrics | null;
  wan_series: Array<{
    time: string;
    rx_bps: number;
    tx_bps: number;
    latency_ms: number;
  }>;
  unifi_devices: Array<{
    mac: string;
    ip_address: string;
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

function formatUptime(sec: number | undefined | null): string {
  const s = Number(sec || 0);
  if (s <= 0) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

const DualAreaSparkChart: React.FC<{
  points: Array<{ label: string; inVal: number; outVal: number; p95In?: number }>;
  height?: number;
}> = ({ points, height = 210 }) => {
  if (!points.length) {
    return (
      <div
        style={{ height }}
        className="flex items-center justify-center rounded-lg border border-slate-800/80 bg-[#0B101B] text-xs text-slate-500"
      >
        Waiting for time-series samples...
      </div>
    );
  }

  const width = 800;
  const maxVal = Math.max(
    1000,
    ...points.map((p) => Math.max(p.inVal, p.outVal, p.p95In || 0)),
  );

  const toCoords = (vals: number[]) =>
    vals
      .map((v, i) => {
        const x = points.length === 1 ? width / 2 : (i / (points.length - 1)) * width;
        const y = height - 20 - (Math.min(v, maxVal) / maxVal) * (height - 40);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");

  const inLine = toCoords(points.map((p) => p.inVal));
  const outLine = toCoords(points.map((p) => p.outVal));
  const inArea = `0,${height - 20} ${inLine} ${width},${height - 20}`;
  const outArea = `0,${height - 20} ${outLine} ${width},${height - 20}`;

  return (
    <div className="relative w-full overflow-hidden rounded-lg border border-slate-800/80 bg-[#0B101B] p-3">
      <div className="mb-2 flex items-center justify-between text-xs text-slate-400">
        <div className="flex items-center gap-4">
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
            RX / Inbound
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-sky-400" />
            TX / Outbound
          </span>
        </div>
        <span className="font-mono-tabular text-slate-300">Peak: {formatBps(maxVal)}</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="h-44 w-full overflow-visible">
        <defs>
          <linearGradient id="rxGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#10B981" stopOpacity="0.32" />
            <stop offset="100%" stopColor="#10B981" stopOpacity="0.0" />
          </linearGradient>
          <linearGradient id="txGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#38BDF8" stopOpacity="0.26" />
            <stop offset="100%" stopColor="#38BDF8" stopOpacity="0.0" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((r) => (
          <line
            key={r}
            x1={0}
            y1={(height - 20) * r}
            x2={width}
            y2={(height - 20) * r}
            stroke="#1E293B"
            strokeDasharray="4 4"
            strokeWidth="1"
          />
        ))}
        <polygon points={inArea} fill="url(#rxGrad)" />
        <polygon points={outArea} fill="url(#txGrad)" />
        <polyline
          fill="none"
          stroke="#10B981"
          strokeWidth="2"
          points={inLine}
          vectorEffect="non-scaling-stroke"
        />
        <polyline
          fill="none"
          stroke="#38BDF8"
          strokeWidth="1.8"
          points={outLine}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    </div>
  );
};

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<TabKey>("pulse");
  const [wsConnected, setWsConnected] = useState(false);

  // Update Notification & Manual Install Prompt State
  const [updateInfo, setUpdateInfo] = useState<SystemUpdateStatus | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [installingUpdate, setInstallingUpdate] = useState(false);
  const [promptDismissedSha, setPromptDismissedSha] = useState<string | null>(null);
  const updateDialogRef = useRef<HTMLDialogElement | null>(null);

  // First-Time Setup Onboarding Modal & Dynamic Config State
  const [config, setConfig] = useState<AllConfigPayload | null>(null);
  const onboardingDialogRef = useRef<HTMLDialogElement | null>(null);

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
  const [savingOnboarding, setSavingOnboarding] = useState(false);

  // Settings Tab Form States (Multiple SNMP Profiles, Hosts, Subnets, UniFi)
  const [newProfileName, setNewProfileName] = useState("");
  const [newProfileVersion, setNewProfileVersion] = useState("v2c");
  const [newProfilePort, setNewProfilePort] = useState(161);
  const [newProfileCommunity, setNewProfileCommunity] = useState("public");
  const [newProfileV3User, setNewProfileV3User] = useState("");
  const [newProfileV3AuthProto, setNewProfileV3AuthProto] = useState("SHA");
  const [newProfileV3AuthKey, setNewProfileV3AuthKey] = useState("");
  const [newProfileV3PrivProto, setNewProfileV3PrivProto] = useState("AES");
  const [newProfileV3PrivKey, setNewProfileV3PrivKey] = useState("");

  const [newHostIp, setNewHostIp] = useState("");
  const [newHostLabel, setNewHostLabel] = useState("");
  const [newHostProfileId, setNewHostProfileId] = useState<number>(1);

  const [newSubnetCidr, setNewSubnetCidr] = useState("");
  const [newSubnetLabel, setNewSubnetLabel] = useState("");
  const [newSubnetVlan, setNewSubnetVlan] = useState("");

  const [newUnifiName, setNewUnifiName] = useState("UDM Pro");
  const [newUnifiHost, setNewUnifiHost] = useState("");
  const [newUnifiApiKey, setNewUnifiApiKey] = useState("");
  const [newUnifiSite, setNewUnifiSite] = useState("default");

  // Domain Telemetry State
  const [overview, setOverview] = useState<OverviewPayload | null>(null);
  const [interfaces, setInterfaces] = useState<SNMPInterface[]>([]);
  const [selectedDeviceIp, setSelectedDeviceIp] = useState<string>("");
  const [selectedIfIndex, setSelectedIfIndex] = useState<number>(1);
  const [trendRange, setTrendRange] = useState<string>("24h");
  const [trendSourceTable, setTrendSourceTable] = useState<string>("snmp_metrics_raw");
  const [trendSeries, setTrendSeries] = useState<SNMPTrendPoint[]>([]);

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

  const openUpdateModal = () => {
    const dlg = updateDialogRef.current;
    if (dlg && !dlg.open) dlg.showModal();
  };

  const closeUpdateModal = () => {
    const dlg = updateDialogRef.current;
    if (dlg && dlg.open) dlg.close();
    if (updateInfo?.remote_sha) setPromptDismissedSha(updateInfo.remote_sha);
  };

  const openOnboardingModal = () => {
    const dlg = onboardingDialogRef.current;
    if (dlg && !dlg.open) dlg.showModal();
  };

  const closeOnboardingModal = () => {
    const dlg = onboardingDialogRef.current;
    if (dlg && dlg.open) dlg.close();
  };

  const fetchConfig = async () => {
    try {
      const r = await fetch("/api/config");
      if (r.ok) {
        const data: AllConfigPayload = await r.json();
        setConfig(data);
        if (data.snmp_profiles.length > 0 && !newHostProfileId) {
          setNewHostProfileId(data.snmp_profiles[0].id);
        }
        if (!data.onboarding.completed) {
          openOnboardingModal();
        }
      }
    } catch {
      // ignore
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
        setConfig(data);
        closeOnboardingModal();
        fetchOverview();
        fetchScanner();
        fetchInterfaces();
      }
    } finally {
      setSavingOnboarding(false);
    }
  };

  const handleAddProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProfileName.trim()) return;
    const r = await fetch("/api/config/snmp-profiles", {
      method: "POST",
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
      setConfig(await r.json());
      setNewProfileName("");
      setNewProfileV3AuthKey("");
      setNewProfileV3PrivKey("");
    }
  };

  const handleDeleteProfile = async (id: number) => {
    const r = await fetch(`/api/config/snmp-profiles/${id}`, { method: "DELETE" });
    if (r.ok) setConfig(await r.json());
  };

  const handleAddHost = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newHostIp.trim()) return;
    const r = await fetch("/api/config/snmp-targets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_ip: newHostIp.trim(),
        label: newHostLabel.trim(),
        profile_id: newHostProfileId || null,
        enabled: true,
      }),
    });
    if (r.ok) {
      setConfig(await r.json());
      setNewHostIp("");
      setNewHostLabel("");
      setTimeout(fetchOverview, 2000);
    }
  };

  const handleDeleteHost = async (id: number) => {
    const r = await fetch(`/api/config/snmp-targets/${id}`, { method: "DELETE" });
    if (r.ok) {
      setConfig(await r.json());
      fetchOverview();
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
      setConfig(await r.json());
      setNewSubnetCidr("");
      setNewSubnetLabel("");
      setNewSubnetVlan("");
      fetchScanner();
    }
  };

  const handleDeleteSubnet = async (id: number) => {
    const r = await fetch(`/api/config/subnets/${id}`, { method: "DELETE" });
    if (r.ok) {
      setConfig(await r.json());
      fetchScanner();
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
      setConfig(await r.json());
      setNewUnifiHost("");
      setNewUnifiApiKey("");
      setTimeout(fetchOverview, 2000);
    }
  };

  const handleDeleteUnifi = async (id: number) => {
    const r = await fetch(`/api/config/unifi/${id}`, { method: "DELETE" });
    if (r.ok) setConfig(await r.json());
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
        if (
          data.update_available &&
          data.remote_sha !== promptDismissedSha &&
          data.status !== "updating"
        ) {
          openUpdateModal();
        }
      }
    } catch {
      // ignore
    } finally {
      if (forceFetch) setCheckingUpdate(false);
    }
  };

  const handleInstallUpdate = async () => {
    setInstallingUpdate(true);
    try {
      const resp = await fetch("/api/system/update/install", { method: "POST" });
      if (resp.ok) {
        const targetSha = updateInfo?.remote_sha;
        const pollTimer = setInterval(async () => {
          try {
            const r = await fetch("/api/system/update");
            if (r.ok) {
              const d: SystemUpdateStatus = await r.json();
              if (!d.update_available || (targetSha && d.current_sha === targetSha)) {
                clearInterval(pollTimer);
                window.location.reload();
              }
            }
          } catch {
            // restarting
          }
        }, 4000);
      }
    } catch {
      setInstallingUpdate(false);
    }
  };

  const fetchOverview = async () => {
    try {
      const r = await fetch("/api/overview");
      if (r.ok) setOverview(await r.json());
    } catch {
      // ignore
    }
  };

  const fetchInterfaces = async () => {
    try {
      const r = await fetch("/api/snmp/interfaces");
      if (r.ok) {
        const data = await r.json();
        const list: SNMPInterface[] = data.interfaces || [];
        setInterfaces(list);
        if (!selectedDeviceIp && list.length > 0) {
          setSelectedDeviceIp(list[0].device_ip);
          setSelectedIfIndex(list[0].if_index);
        }
      }
    } catch {
      // ignore
    }
  };

  const fetchTrends = async (devIp: string, ifIdx: number, rk: string) => {
    if (!devIp) return;
    try {
      const r = await fetch(
        `/api/snmp/trends?device_ip=${encodeURIComponent(devIp)}&if_index=${ifIdx}&range=${rk}`,
      );
      if (r.ok) {
        const data = await r.json();
        setTrendSeries(data.series || []);
        setTrendSourceTable(data.source_table || "snmp_metrics_raw");
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
    fetchOverview();
    fetchInterfaces();
    fetchScanner();
    fetchLogs();
    fetchUpdateStatus(false);

    const interval = setInterval(() => {
      fetchOverview();
      if (activeTab === "snmp") fetchInterfaces();
      if (activeTab === "scanner") fetchScanner();
      if (activeTab === "syslog") fetchLogs();
    }, 10000);

    const updateTimer = setInterval(() => {
      fetchUpdateStatus(true);
    }, 120000);

    return () => {
      clearInterval(interval);
      clearInterval(updateTimer);
    };
  }, [activeTab, logsPaused, logSeverity, logSourceIp, logSearch]);

  useEffect(() => {
    if (selectedDeviceIp && selectedIfIndex) {
      fetchTrends(selectedDeviceIp, selectedIfIndex, trendRange);
    }
  }, [selectedDeviceIp, selectedIfIndex, trendRange]);

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
          if (msg.type === "unifi_update") fetchOverview();
          if (msg.type === "snmp_update") {
            fetchOverview();
            fetchInterfaces();
          }
          if (msg.type === "scanner_update") {
            setScanningNow(false);
            fetchScanner();
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

  const gw = overview?.gateway;
  const filteredHosts = selectedSubnet
    ? hosts.filter((h) => h.subnet_cidr === selectedSubnet)
    : hosts;

  return (
    <div className="min-h-screen bg-[#090D16] text-slate-100">
      {/* Top Navigation Bar */}
      <header className="sticky top-0 z-30 border-b border-slate-800/90 bg-[#090D16]/95 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-emerald-500/30 bg-emerald-500/10 text-emerald-400">
              <Activity className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-base font-semibold tracking-tight text-white">NetDash</h1>
                <span className="rounded border border-slate-700 bg-slate-800/80 px-1.5 py-0.5 font-mono-tabular text-[11px] text-slate-300">
                  {updateInfo?.current_sha || "v1.1"}
                </span>
              </div>
              <p className="text-xs text-slate-400">
                Network Observability • SNMP • Nmap Matrix • Syslog
              </p>
            </div>
          </div>

          {/* Primary View Switcher */}
          <nav className="flex flex-wrap items-center gap-1 rounded-lg border border-slate-800 bg-[#111827] p-1">
            {[
              { id: "pulse", label: "Pulse Overview", icon: Activity },
              { id: "snmp", label: "SNMP Trends & Ports", icon: Server },
              { id: "scanner", label: "Subnet Scanner Matrix", icon: Network },
              { id: "syslog", label: "Live Syslog Stream", icon: Terminal },
              { id: "settings", label: "Targets & SNMP Settings", icon: Settings },
            ].map((tab) => {
              const Icon = tab.icon;
              const active = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id as TabKey)}
                  className={`flex items-center gap-2 rounded-md px-3 py-1.5 text-xs font-medium transition ${
                    active
                      ? "bg-slate-800 text-white shadow-xs"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </nav>

          {/* Right Status & Update Controls */}
          <div className="flex items-center gap-2.5">
            {updateInfo?.update_available ? (
              <button
                onClick={openUpdateModal}
                className="flex items-center gap-1.5 rounded-md border border-amber-500/50 bg-amber-500/15 px-3 py-1.5 text-xs font-medium text-amber-300 transition hover:bg-amber-500/25"
              >
                <DownloadCloud className="h-3.5 w-3.5 animate-bounce" />
                <span>
                  Update Available ({updateInfo.commits_behind} new commit
                  {updateInfo.commits_behind === 1 ? "" : "s"})
                </span>
              </button>
            ) : (
              <button
                onClick={() => fetchUpdateStatus(true)}
                disabled={checkingUpdate}
                className="flex items-center gap-1.5 rounded-md border border-slate-800 bg-[#111827] px-2.5 py-1.5 text-xs text-slate-300 transition hover:border-slate-700 hover:text-white disabled:opacity-50"
                title="Check GitHub repository for new commits"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${checkingUpdate ? "animate-spin" : ""}`} />
                <span>{checkingUpdate ? "Checking..." : "Check Updates"}</span>
              </button>
            )}

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
                Targets & SNMP Settings.
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
          {/* Subnets to Scan */}
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

          {/* SNMP Hosts & Community */}
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
                <input
                  type="text"
                  placeholder="SNMP Community (public)"
                  value={obSnmpCommunity}
                  onChange={(e) => setObSnmpCommunity(e.target.value)}
                  className="w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 font-mono-tabular text-xs text-white"
                />
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
                {updateInfo?.commits_behind || 1} new commit(s) ready to download & install
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
              <span className="text-slate-400">Installed Commit: </span>
              <span className="font-mono-tabular font-semibold text-slate-200">
                {updateInfo?.current_sha}
              </span>
            </div>
            <span className="text-slate-500">→</span>
            <div>
              <span className="text-slate-400">Available Commit: </span>
              <span className="font-mono-tabular font-semibold text-emerald-400">
                {updateInfo?.remote_sha}
              </span>
            </div>
          </div>

          {updateInfo?.commits && updateInfo.commits.length > 0 && (
            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-400">
                Pending Commits (origin/main)
              </p>
              <div className="max-h-48 space-y-1.5 overflow-y-auto rounded-lg border border-slate-800 bg-[#090D16] p-2.5">
                {updateInfo.commits.map((c) => (
                  <div
                    key={c.sha}
                    className="flex items-start justify-between gap-3 border-b border-slate-800/60 pb-1.5 text-xs last:border-b-0 last:pb-0"
                  >
                    <div className="flex items-start gap-2">
                      <span className="font-mono-tabular text-emerald-400">{c.sha}</span>
                      <span className="text-slate-200">{c.subject}</span>
                    </div>
                    <span className="shrink-0 text-[11px] text-slate-500">{c.date}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

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

      {/* Main Content Container */}
      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6">
        {/* ==================== VIEW 1: PULSE OVERVIEW ==================== */}
        {activeTab === "pulse" && (
          <>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>Gateway WAN Throughput</span>
                  <Router className="h-4 w-4 text-emerald-400" />
                </div>
                <div className="mt-3 flex items-baseline justify-between">
                  <div>
                    <div className="flex items-center gap-1 font-mono-tabular text-lg font-semibold text-white">
                      <ArrowDownRight className="h-4 w-4 text-emerald-400" />
                      {formatBps(gw?.wan_rx_bps)}
                    </div>
                    <div className="flex items-center gap-1 font-mono-tabular text-xs text-sky-400">
                      <ArrowUpRight className="h-3.5 w-3.5" />
                      {formatBps(gw?.wan_tx_bps)}
                    </div>
                  </div>
                  <div className="text-right font-mono-tabular text-xs">
                    <div className="text-slate-300">{gw?.wan_ip || "Optional UniFi"}</div>
                    <div className="text-slate-500">
                      {gw ? `${gw.wan_latency_ms.toFixed(1)} ms` : "Not Linked"}
                    </div>
                  </div>
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
                  <button
                    onClick={() => setActiveTab("settings")}
                    className="rounded border border-slate-700 bg-slate-800/80 px-2 py-1 font-sans text-xs text-emerald-400 hover:bg-slate-700"
                  >
                    + Add Host
                  </button>
                </div>
              </div>

              <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>Discovered Subnet Hosts</span>
                  <Network className="h-4 w-4 text-amber-400" />
                </div>
                <div className="mt-3 flex items-baseline justify-between font-mono-tabular">
                  <div>
                    <div className="text-lg font-semibold text-white">
                      {overview?.scanner_summary?.online_hosts || 0} Online
                    </div>
                    <div className="text-xs text-slate-400">
                      Across {config?.subnets?.length || 0} Subnet(s)
                    </div>
                  </div>
                  <button
                    onClick={() => setActiveTab("settings")}
                    className="rounded border border-slate-700 bg-slate-800/80 px-2 py-1 font-sans text-xs text-emerald-400 hover:bg-slate-700"
                  >
                    + Add Subnet
                  </button>
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

            {/* Infrastructure Tables */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-white">
                    Polled SNMP Targets ({overview?.snmp_devices?.length || 0})
                  </h3>
                  <button
                    onClick={() => setActiveTab("settings")}
                    className="flex items-center gap-1 rounded border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs text-slate-200 hover:bg-slate-700"
                  >
                    <Plus className="h-3.5 w-3.5" /> Manage SNMP Hosts & Secrets
                  </button>
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
                            <button
                              onClick={() => setActiveTab("settings")}
                              className="text-emerald-400 underline"
                            >
                              Manage SNMP Hosts & Secrets
                            </button>{" "}
                            to add devices.
                          </td>
                        </tr>
                      ) : (
                        overview!.snmp_devices.map((s) => (
                          <tr key={s.device_ip} className="hover:bg-slate-800/30">
                            <td className="py-2.5 text-white">{s.device_ip}</td>
                            <td className="py-2.5 font-sans text-slate-300">
                              {s.sys_name || "—"}
                            </td>
                            <td className="py-2.5 text-slate-300">{s.interface_count}</td>
                            <td className="py-2.5 text-slate-400">
                              {s.last_poll_duration_ms.toFixed(0)} ms
                            </td>
                            <td className="py-2.5 text-right">
                              <span
                                className={`inline-block rounded px-1.5 py-0.5 text-[11px] ${
                                  s.is_reachable
                                    ? "bg-emerald-500/15 text-emerald-400"
                                    : "bg-rose-500/15 text-rose-400"
                                }`}
                              >
                                {s.is_reachable ? "reachable" : "timeout"}
                              </span>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-white">
                    UniFi Infrastructure ({overview?.unifi_devices?.length || 0})
                  </h3>
                  <button
                    onClick={() => setActiveTab("settings")}
                    className="flex items-center gap-1 rounded border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs text-slate-200 hover:bg-slate-700"
                  >
                    <Plus className="h-3.5 w-3.5" /> Configure UniFi (Optional)
                  </button>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="border-b border-slate-800 text-slate-400">
                        <th className="pb-2 font-medium">Device</th>
                        <th className="pb-2 font-medium">IP / Model</th>
                        <th className="pb-2 font-medium">CPU / RAM</th>
                        <th className="pb-2 font-medium">PoE / Clients</th>
                        <th className="pb-2 text-right font-medium">Status</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                      {(overview?.unifi_devices || []).length === 0 ? (
                        <tr>
                          <td colSpan={5} className="py-6 text-center font-sans text-slate-500">
                            UniFi integration is optional. Add a controller in{" "}
                            <button
                              onClick={() => setActiveTab("settings")}
                              className="text-emerald-400 underline"
                            >
                              Targets & SNMP Settings
                            </button>{" "}
                            if desired.
                          </td>
                        </tr>
                      ) : (
                        overview!.unifi_devices.map((d) => (
                          <tr key={d.mac} className="hover:bg-slate-800/30">
                            <td className="py-2.5 font-sans font-medium text-white">{d.name}</td>
                            <td className="py-2.5 text-slate-300">
                              {d.ip_address} <span className="text-slate-500">({d.model})</span>
                            </td>
                            <td className="py-2.5 text-slate-300">
                              {d.cpu_usage_pct.toFixed(0)}% / {d.mem_usage_pct.toFixed(0)}%
                            </td>
                            <td className="py-2.5 text-slate-300">
                              {d.poe_total_watts > 0 ? `${d.poe_total_watts.toFixed(1)}W` : "—"} •{" "}
                              {d.num_sta} sta
                            </td>
                            <td className="py-2.5 text-right">
                              <span
                                className={`inline-block rounded px-1.5 py-0.5 text-[11px] ${
                                  d.status === "online"
                                    ? "bg-emerald-500/15 text-emerald-400"
                                    : "bg-rose-500/15 text-rose-400"
                                }`}
                              >
                                {d.status}
                              </span>
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

        {/* ==================== VIEW 2: SNMP TRENDS & PORTS ==================== */}
        {activeTab === "snmp" && (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-800 bg-[#111827] p-4">
              <div className="flex flex-wrap items-center gap-3">
                <div>
                  <label className="block text-[11px] text-slate-400">Device IP</label>
                  <select
                    value={selectedDeviceIp}
                    onChange={(e) => setSelectedDeviceIp(e.target.value)}
                    className="mt-1 rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                  >
                    {Array.from(new Set(interfaces.map((i) => i.device_ip))).map((ip) => (
                      <option key={ip} value={ip}>
                        {ip}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block text-[11px] text-slate-400">Interface (IF-MIB)</label>
                  <select
                    value={selectedIfIndex}
                    onChange={(e) => setSelectedIfIndex(Number(e.target.value))}
                    className="mt-1 rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                  >
                    {interfaces
                      .filter((i) => !selectedDeviceIp || i.device_ip === selectedDeviceIp)
                      .map((i) => (
                        <option key={i.if_index} value={i.if_index}>
                          #{i.if_index} — {i.if_name} ({formatBps(i.in_bps)} RX)
                        </option>
                      ))}
                  </select>
                </div>

                <button
                  onClick={() => setActiveTab("settings")}
                  className="mt-4 flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs font-medium text-emerald-400 hover:bg-slate-700"
                >
                  <Plus className="h-3.5 w-3.5" /> Add / Edit SNMP Hosts & Secrets
                </button>
              </div>

              <div>
                <label className="block text-right text-[11px] text-slate-400">
                  TimescaleDB Rollup Window (Source: <code>{trendSourceTable}</code>)
                </label>
                <div className="mt-1 flex items-center gap-1 rounded-lg border border-slate-800 bg-[#090D16] p-1">
                  {["1h", "6h", "24h", "7d", "30d", "1y", "all"].map((rk) => (
                    <button
                      key={rk}
                      onClick={() => setTrendRange(rk)}
                      className={`rounded px-2.5 py-1 font-mono-tabular text-xs font-medium uppercase transition ${
                        trendRange === rk
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

            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <h3 className="mb-3 text-sm font-semibold text-white">
                Interface Throughput Trend ({selectedDeviceIp || "No Device"} • ifIndex #
                {selectedIfIndex})
              </h3>
              <DualAreaSparkChart
                points={trendSeries.map((pt) => ({
                  label: pt.time,
                  inVal: pt.avg_in_bps,
                  outVal: pt.avg_out_bps,
                  p95In: pt.p95_in_bps,
                }))}
              />
            </div>

            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <h3 className="mb-3 text-sm font-semibold text-white">
                64-Bit Interface Counters (<code>ifXTable</code>)
              </h3>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
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
                    {interfaces.map((intf) => (
                      <tr
                        key={`${intf.device_ip}-${intf.if_index}`}
                        onClick={() => {
                          setSelectedDeviceIp(intf.device_ip);
                          setSelectedIfIndex(intf.if_index);
                        }}
                        className="cursor-pointer hover:bg-slate-800/40"
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

        {/* ==================== VIEW 3: SUBNET SCANNER MATRIX ==================== */}
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
                <button
                  onClick={() => setActiveTab("settings")}
                  className="flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-xs font-medium text-emerald-400 hover:bg-slate-700"
                >
                  <Plus className="h-3.5 w-3.5" /> Add / Manage Subnets
                </button>
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
                    /24 IP Occupancy Grid ({selectedSubnet})
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
                        ? "bg-emerald-500 text-slate-950 font-semibold"
                        : "bg-amber-500/40 text-amber-200"
                      : "bg-[#090D16] text-slate-600 border border-slate-800/70";
                    return (
                      <div
                        key={octet}
                        title={
                          match
                            ? `${fullIp} • ${match.hostname || "host"} (${match.mac_address || "no MAC"})`
                            : `${fullIp} (Unused)`
                        }
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
                Discovered Hosts ({filteredHosts.length})
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
                      <tr key={h.ip_address} className="hover:bg-slate-800/30">
                        <td className="py-2.5 font-semibold text-white">{h.ip_address}</td>
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

        {/* ==================== VIEW 4: LIVE SYSLOG STREAM ==================== */}
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
                            <td className="py-2 pr-3 whitespace-nowrap text-slate-300">
                              {l.hostname || l.source_ip}
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

        {/* ==================== VIEW 5: TARGETS & SNMP SETTINGS ==================== */}
        {activeTab === "settings" && (
          <div className="space-y-6">
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

            {/* 2. Multiple SNMP Credential Profiles & Secrets */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="flex items-center gap-2">
                <KeyRound className="h-4 w-4 text-sky-400" />
                <h2 className="text-sm font-semibold text-white">
                  SNMP Query Profiles & Credentials (Multiple v2c / v3 Secrets)
                </h2>
              </div>
              <p className="mt-0.5 text-xs text-slate-400">
                Create multiple reusable SNMPv2c community profiles or SNMPv3 AuthPriv secret
                profiles, then assign them to any monitored host below.
              </p>

              <form onSubmit={handleAddProfile} className="mt-4 space-y-3 rounded-lg border border-slate-800 bg-[#090D16] p-4">
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
                      <input
                        type="password"
                        placeholder="public"
                        value={newProfileCommunity}
                        onChange={(e) => setNewProfileCommunity(e.target.value)}
                        className="mt-1 w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 font-mono-tabular text-xs text-white"
                      />
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
                      <input
                        type="password"
                        placeholder="Auth secret key"
                        value={newProfileV3AuthKey}
                        onChange={(e) => setNewProfileV3AuthKey(e.target.value)}
                        className="mt-1 w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 font-mono-tabular text-xs text-white"
                      />
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
                      <input
                        type="password"
                        placeholder="Priv secret key"
                        value={newProfileV3PrivKey}
                        onChange={(e) => setNewProfileV3PrivKey(e.target.value)}
                        className="mt-1 w-full rounded-md border border-slate-700 bg-[#111827] px-3 py-1.5 font-mono-tabular text-xs text-white"
                      />
                    </div>
                  </div>
                )}

                <div className="flex justify-end">
                  <button
                    type="submit"
                    className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-sky-500"
                  >
                    <Plus className="h-3.5 w-3.5" /> Save SNMP Profile
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
                    {(config?.snmp_profiles || []).map((p) => (
                      <tr key={p.id}>
                        <td className="py-2.5 font-sans font-semibold text-white">{p.name}</td>
                        <td className="py-2.5 uppercase text-sky-400">{p.version}</td>
                        <td className="py-2.5 text-slate-300">{p.port}</td>
                        <td className="py-2.5 text-slate-400">
                          {p.version === "v3"
                            ? `User: ${p.v3_user} (${p.v3_auth_proto}/${p.v3_priv_proto})`
                            : `Community: ••••••••`}
                        </td>
                        <td className="py-2.5 text-right">
                          <button
                            onClick={() => handleDeleteProfile(p.id)}
                            className="inline-flex items-center gap-1 rounded px-2 py-1 text-rose-400 hover:bg-rose-500/15"
                          >
                            <Trash2 className="h-3.5 w-3.5" /> Delete
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* 3. Monitored SNMP Target Hosts */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <h2 className="text-sm font-semibold text-white">
                Monitored SNMP Hosts (Assign Any SNMP Profile)
              </h2>
              <p className="mt-0.5 text-xs text-slate-400">
                Add routers, switches, firewalls, or Linux hosts and link each one to any SNMP
                profile above.
              </p>

              <form onSubmit={handleAddHost} className="mt-4 flex flex-wrap gap-2.5">
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
                <button
                  type="submit"
                  className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                >
                  <Plus className="h-3.5 w-3.5" /> Add SNMP Host
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
                        <td className="py-2.5 font-semibold text-white">{t.device_ip}</td>
                        <td className="py-2.5 font-sans text-slate-300">{t.label || "—"}</td>
                        <td className="py-2.5 font-sans text-sky-400">
                          {t.profile_name || "Default"} ({t.profile_version || "v2c"})
                        </td>
                        <td className="py-2.5 text-right">
                          <button
                            onClick={() => handleDeleteHost(t.id)}
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
                <input
                  type="password"
                  placeholder="UniFi X-API-KEY Secret"
                  value={newUnifiApiKey}
                  onChange={(e) => setNewUnifiApiKey(e.target.value)}
                  className="rounded-md border border-slate-700 bg-[#090D16] px-3 py-1.5 font-mono-tabular text-xs text-white"
                  required
                />
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
          </div>
        )}
      </main>
    </div>
  );
};

export default App;
