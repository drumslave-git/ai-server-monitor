import { useEffect, useState } from "react";
import { api, type StatusResponse } from "./api";
import { AutomationNotice, AutomationProvider, AutomationSwitch } from "./automation";
import { HardwareMeters } from "./hardware";
import { Main, Rail, Shell, type IconName, type NavItem } from "./kit";
import { useConnected, useStream } from "./live";
import { ApprovalsPage } from "./pages/Approvals";
import { ChatPage } from "./pages/Chat";
import { DashboardPage } from "./pages/Dashboard";
import { JobsPage } from "./pages/Jobs";
import { LogsPage } from "./pages/Logs";
import { McpToolsPage } from "./pages/McpTools";
import { SettingsPage } from "./pages/Settings";

const PAGES: { key: string & IconName; title: string; label: string }[] = [
  { key: "dashboard", title: "Dashboard", label: "dashboard" },
  { key: "jobs", title: "Jobs", label: "jobs" },
  { key: "approvals", title: "Approvals", label: "approvals" },
  { key: "chat", title: "Chat", label: "chat" },
  { key: "mcp", title: "MCP tools", label: "mcp tools" },
  { key: "logs", title: "Logs", label: "logs" },
  { key: "settings", title: "Settings", label: "settings" },
];

function parseHash(): string[] {
  return location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
}

/** "v0.8.5 · c2448e1", once the server has said so. */
function useVersion(): string | null {
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    api.get<{ version: string; commit?: string | null }>("/api/info").then(
      (i) => setVersion(`v${i.version}${i.commit ? ` · ${i.commit}` : ""}`),
      () => undefined,
    );
  }, []);
  return version;
}

export function App() {
  const [route, setRoute] = useState(parseHash);
  const connected = useConnected();
  const version = useVersion();
  const [pending, setPending] = useState(0);

  useEffect(() => {
    const onHash = () => {
      setRoute(parseHash());
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

  const page = PAGES.some((p) => p.key === route[0]) ? route[0] : "dashboard";
  const params = route.slice(1);
  // Tabs, history and screen readers tell pages apart by the title.
  const title = PAGES.find((p) => p.key === page)?.title ?? "Dashboard";
  useEffect(() => {
    document.title = `${title} · AI Server Monitor`;
  }, [title]);

  const items: NavItem[] = PAGES.map((p) => ({
    key: p.key,
    label: p.label,
    href: `#/${p.key}`,
    icon: p.key,
    count: p.key === "approvals" ? pending : undefined,
  }));

  return (
    <AutomationProvider>
      <Shell>
        <Rail items={items} active={page} live={connected} version={version} meters={<HardwareMeters />}>
          <AutomationSwitch />
        </Rail>
        <Main resetOn={route.join("/")}>
          <AutomationNotice />
          {page === "jobs" ? (
            <JobsPage params={params} />
          ) : page === "approvals" ? (
            <ApprovalsPage params={params} />
          ) : page === "chat" ? (
            <ChatPage />
          ) : page === "mcp" ? (
            <McpToolsPage />
          ) : page === "logs" ? (
            <LogsPage params={params} />
          ) : page === "settings" ? (
            <SettingsPage />
          ) : (
            <DashboardPage />
          )}
        </Main>
      </Shell>
    </AutomationProvider>
  );
}
