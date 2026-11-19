import React, { useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  Cpu,
  DownloadCloud,
  HardDrive,
  Network,
  Play,
  Radio,
  RefreshCw,
  Router,
  Search,
  Server,
  ShieldCheck,
  Terminal,
  Thermometer,
  Wifi,
  X,
  Zap,
} from "lucide-react";

type TabKey = "pulse" | "snmp" | "scanner" | "syslog";

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

  // Domain State
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

  // Open or close the native <dialog> update modal prompt
  const openUpdateModal = () => {
    const dlg = updateDialogRef.current;
    if (dlg && !dlg.open) {
      dlg.showModal();
    }
  };

  const closeUpdateModal = () => {
    const dlg = updateDialogRef.current;
    if (dlg && dlg.open) {
      dlg.close();
    }
    if (updateInfo?.remote_sha) {
      setPromptDismissedSha(updateInfo.remote_sha);
    }
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
      // ignore transient network errors during container restart
    } finally {
      if (forceFetch) setCheckingUpdate(false);
    }
  };

  const handleInstallUpdate = async () => {
    setInstallingUpdate(true);
    try {
      const resp = await fetch("/api/system/update/install", { method: "POST" });
      if (resp.ok) {
        // Poll until backend restarts with the new commit SHA
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
            // Container is restarting
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
      if (r.ok) {
        const data: OverviewPayload = await r.json();
        setOverview(data);
      }
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
        if (!selectedSubnet && cfg.length > 0) {
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

  // Live WebSocket connection
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
          if (msg.type === "snmp_update") fetchInterfaces();
          if (msg.type === "scanner_update") {
            setScanningNow(false);
            fetchScanner();
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
      {/* Top Navigation & System Bar */}
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
                  {updateInfo?.current_sha || "v1.0"}
                </span>
              </div>
              <p className="text-xs text-slate-400">
                UDM Pro • Async SNMP • Nmap Matrix • Vector Syslog
              </p>
            </div>
          </div>

          {/* Primary View Switcher */}
          <nav className="flex items-center gap-1 rounded-lg border border-slate-800 bg-[#111827] p-1">
            {[
              { id: "pulse", label: "Pulse Overview", icon: Activity },
              { id: "snmp", label: "SNMP Trends & Ports", icon: Server },
              { id: "scanner", label: "Subnet Scanner Matrix", icon: Network },
              { id: "syslog", label: "Live Syslog Stream", icon: Terminal },
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
        <div className="border-b border-slate-800 px-5 py-4 flex items-center justify-between">
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
                    className="flex items-start justify-between gap-3 border-b border-slate-800/60 pb-1.5 last:border-b-0 last:pb-0 text-xs"
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
              <RefreshCw className="h-4 w-4 animate-spin shrink-0" />
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
            {/* Top Metric Cards */}
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>UDM Pro WAN Throughput</span>
                  <Router className="h-4 w-4 text-emerald-400" />
                </div>
                <div className="mt-3 flex items-baseline justify-between">
                  <div>
                    <div className="flex items-center gap-1 text-lg font-semibold font-mono-tabular text-white">
                      <ArrowDownRight className="h-4 w-4 text-emerald-400" />
                      {formatBps(gw?.wan_rx_bps)}
                    </div>
                    <div className="flex items-center gap-1 text-xs font-mono-tabular text-sky-400">
                      <ArrowUpRight className="h-3.5 w-3.5" />
                      {formatBps(gw?.wan_tx_bps)}
                    </div>
                  </div>
                  <div className="text-right text-xs font-mono-tabular">
                    <div className="text-slate-300">{gw?.wan_ip || "WAN Standby"}</div>
                    <div className="text-slate-500">
                      {gw ? `${gw.wan_latency_ms.toFixed(1)} ms` : "Configure UNIFI_API_KEY"}
                    </div>
                  </div>
                </div>
              </div>

              <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>Gateway Health & Temps</span>
                  <Cpu className="h-4 w-4 text-sky-400" />
                </div>
                <div className="mt-3 flex items-baseline justify-between font-mono-tabular">
                  <div>
                    <div className="text-lg font-semibold text-white">
                      CPU {gw ? `${gw.cpu_usage_pct.toFixed(1)}%` : "—"}
                    </div>
                    <div className="text-xs text-slate-400">
                      RAM {gw ? `${gw.mem_usage_pct.toFixed(1)}%` : "—"}
                    </div>
                  </div>
                  <div className="text-right text-xs">
                    <div className="flex items-center justify-end gap-1 text-amber-400">
                      <Thermometer className="h-3.5 w-3.5" />
                      {gw?.temp_celsius != null ? `${gw.temp_celsius.toFixed(0)}°C` : "—"}
                    </div>
                    <div className="text-slate-500">Up {formatUptime(gw?.wan_uptime_sec)}</div>
                  </div>
                </div>
              </div>

              <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>PoE Budget & Active Clients</span>
                  <Zap className="h-4 w-4 text-amber-400" />
                </div>
                <div className="mt-3 flex items-baseline justify-between font-mono-tabular">
                  <div>
                    <div className="text-lg font-semibold text-white">
                      {gw ? `${gw.total_poe_watts.toFixed(1)} W` : "0.0 W"}
                    </div>
                    <div className="text-xs text-slate-400">Total Switch PoE Draw</div>
                  </div>
                  <div className="text-right text-xs">
                    <div className="text-emerald-400 font-semibold">
                      {(gw?.active_lan_clients || 0) + (gw?.active_wlan_clients || 0)} Clients
                    </div>
                    <div className="text-slate-500">
                      {overview?.scanner_summary?.online_hosts || 0} ARP/Nmap Online
                    </div>
                  </div>
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

            {/* Real-Time WAN Throughput Chart */}
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="mb-3 flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold text-white">
                    WAN Throughput & Latency (Last 60 Minutes)
                  </h2>
                  <p className="text-xs text-slate-400">
                    1-minute rollup from TimescaleDB hypertable <code>unifi_gateway_metrics</code>
                  </p>
                </div>
                <span className="rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 font-mono-tabular text-xs text-emerald-400">
                  {gw?.wan_status?.toUpperCase() || "READY"}
                </span>
              </div>
              <DualAreaSparkChart
                points={(overview?.wan_series || []).map((pt) => ({
                  label: pt.time,
                  inVal: pt.rx_bps,
                  outVal: pt.tx_bps,
                }))}
              />
            </div>

            {/* Infrastructure Tables: UniFi Devices & SNMP Targets */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                <h3 className="mb-3 text-sm font-semibold text-white">
                  Adopted UniFi Infrastructure ({overview?.unifi_devices?.length || 0})
                </h3>
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
                          <td colSpan={5} className="py-6 text-center text-slate-500">
                            No UniFi devices polled yet. Verify UNIFI_HOST & UNIFI_API_KEY in .env.
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

              <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
                <h3 className="mb-3 text-sm font-semibold text-white">
                  Polled SNMP Targets ({overview?.snmp_devices?.length || 0})
                </h3>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="border-b border-slate-800 text-slate-400">
                        <th className="pb-2 font-medium">Target IP</th>
                        <th className="pb-2 font-medium">sysName</th>
                        <th className="pb-2 font-medium">Interfaces</th>
                        <th className="pb-2 font-medium">Poll Time</th>
                        <th className="pb-2 text-right font-medium">State</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60 font-mono-tabular">
                      {(overview?.snmp_devices || []).length === 0 ? (
                        <tr>
                          <td colSpan={5} className="py-6 text-center text-slate-500">
                            Waiting for initial 60s SNMP poll cycle...
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
                Interface Throughput Trend ({selectedDeviceIp || "No Device"} • ifIndex #{selectedIfIndex})
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
              <div className="flex items-center gap-3">
                <span className="text-xs text-slate-400">Allowed Subnet CIDR:</span>
                <div className="flex flex-wrap gap-1.5">
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
                </div>
              </div>

              <button
                onClick={triggerScanNow}
                disabled={scanningNow}
                className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
              >
                <Play className="h-3.5 w-3.5" />
                <span>{scanningNow ? "Scanning Subnet..." : "Trigger Nmap Sweep Now"}</span>
              </button>
            </div>

            {/* 254-Host Visual Occupancy Matrix for /24 Subnets */}
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
                      <span className="h-2.5 w-2.5 rounded-xs bg-amber-500/60" /> Historical / Offline
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span className="h-2.5 w-2.5 rounded-xs bg-slate-800" /> Unused
                    </span>
                  </div>
                </div>
                <div className="grid grid-cols-16 gap-1 sm:grid-cols-32">
                  {Array.from({ length: 254 }, (_, i) => i + 1).map((octet) => {
                    const prefix = selectedSubnet.split("/")[0].split(".").slice(0, 3).join(".");
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
                Discovered Hosts & ARP / UniFi Correlation ({filteredHosts.length})
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
                              <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${sevColor}`}>
                                {l.severity}
                              </span>
                            </td>
                            <td className="py-2 pr-3 whitespace-nowrap text-slate-300">
                              {l.hostname || l.source_ip}
                            </td>
                            <td className="py-2 pr-3 whitespace-nowrap text-sky-400">
                              {l.app_name}
                            </td>
                            <td className="py-2 text-slate-200 break-all">{l.message}</td>
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
      </main>
    </div>
  );
};

export default App;
