import { Activity, Download, FileText, MessageSquare, Settings, ShieldCheck, type LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type StatusResponse } from "./api";
import { useConnected, useStream } from "./live";
import { ApprovalsPage } from "./pages/Approvals";
import { ArrPage } from "./pages/Arr";
import { ChatPage } from "./pages/Chat";
import { DashboardPage } from "./pages/Dashboard";
import { ReportsPage } from "./pages/Reports";
import { SettingsPage } from "./pages/Settings";

const PAGES: { key: string; title: string; icon: LucideIcon }[] = [
  { key: "dashboard", title: "Dashboard", icon: Activity },
  { key: "reports", title: "Reports", icon: FileText },
  { key: "approvals", title: "Approvals", icon: ShieldCheck },
  { key: "arr", title: "*arr", icon: Download },
  { key: "chat", title: "Chat", icon: MessageSquare },
  { key: "settings", title: "Settings", icon: Settings },
];

function parseHash(): string[] {
  return location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
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

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className={`live-dot ${connected ? "on" : ""}`} title={connected ? "Live" : "Reconnecting"} />
          <span>AI Server Monitor</span>
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
        <p className="sidebar-foot">{connected ? "Live updates on" : "Reconnecting…"}</p>
      </aside>
      <main className="content">
        {page === "reports" ? (
          <ReportsPage params={params} />
        ) : page === "approvals" ? (
          <ApprovalsPage />
        ) : page === "arr" ? (
          <ArrPage />
        ) : page === "chat" ? (
          <ChatPage params={params} />
        ) : page === "settings" ? (
          <SettingsPage />
        ) : (
          <DashboardPage />
        )}
      </main>
    </div>
  );
}
