import { Activity, FileText, MessageSquare, ScrollText, Settings, ShieldCheck, type LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type StatusResponse } from "./api";
import { AutomationBar, AutomationProvider } from "./automation";
import { useConnected, useStream } from "./live";
import { ApprovalsPage } from "./pages/Approvals";
import { ChatPage } from "./pages/Chat";
import { DashboardPage } from "./pages/Dashboard";
import { LogsPage } from "./pages/Logs";
import { ReportsPage } from "./pages/Reports";
import { SettingsPage } from "./pages/Settings";

const PAGES: { key: string; title: string; icon: LucideIcon }[] = [
  { key: "dashboard", title: "Dashboard", icon: Activity },
  { key: "reports", title: "Reports", icon: FileText },
  { key: "approvals", title: "Approvals", icon: ShieldCheck },
  { key: "chat", title: "Chat", icon: MessageSquare },
  { key: "logs", title: "Logs", icon: ScrollText },
  { key: "settings", title: "Settings", icon: Settings },
];

function parseHash(): string[] {
  return location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
}

function Version() {
  const [info, setInfo] = useState<{ version: string; commit: string | null } | null>(null);
  useEffect(() => {
    api.get<{ version: string; commit?: string | null }>("/api/info").then(
      (i) => setInfo({ version: i.version, commit: i.commit ?? null }),
      () => undefined,
    );
  }, []);
  if (!info) return null;
  return (
    <span className="app-version" title={`Server version${info.commit ? ` (commit ${info.commit})` : ""}`}>
      v{info.version}
      {info.commit ? ` · ${info.commit}` : ""}
    </span>
  );
}

export function App() {
  const [route, setRoute] = useState(parseHash);
  const connected = useConnected();
  const [pending, setPending] = useState(0);

  useEffect(() => {
    const onHash = () => {
      setRoute(parseHash());
      window.scrollTo({ top: 0 });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const loadPending = () =>
    api.get<StatusResponse>("/api/status").then(
      (s) => setPending(s.pending_approvals),
      () => undefined,
    );
  useEffect(() => {
    void loadPending();
  }, []);
  useStream(["approval", "action"], () => void loadPending());

  const page = route[0] || "dashboard";
  const params = route.slice(1);
  // Tabs, history and screen readers tell pages apart by the title.
  const title = PAGES.find((p) => p.key === page)?.title ?? "Dashboard";
  useEffect(() => {
    document.title = `${title} · AI Server Monitor`;
  }, [title]);

  return (
    <AutomationProvider>
    <div className="app">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand">
            <span className={`live-dot ${connected ? "on" : ""}`} title={connected ? "Live updates on" : "Reconnecting"} />
            <span>AI Server Monitor</span>
          </div>
          <Version />
        </div>
        <nav aria-label="Main">
          {PAGES.map(({ key, title, icon: Icon }) => (
            <a key={key} href={`#/${key}`} className={page === key ? "active" : undefined} aria-current={page === key ? "page" : undefined}>
              <Icon size={18} strokeWidth={1.5} aria-hidden />
              <span>{title}</span>
              {key === "approvals" && pending > 0 && <span className="count">{pending}</span>}
            </a>
          ))}
        </nav>
        {!connected && <p className="sidebar-foot">Reconnecting…</p>}
      </aside>
      <main className="content">
        <AutomationBar />
        {page === "reports" ? (
          <ReportsPage params={params} />
        ) : page === "approvals" ? (
          <ApprovalsPage params={params} />
        ) : page === "chat" ? (
          <ChatPage />
        ) : page === "logs" ? (
          <LogsPage params={params} />
        ) : page === "settings" ? (
          <SettingsPage />
        ) : (
          <DashboardPage />
        )}
      </main>
    </div>
    </AutomationProvider>
  );
}
