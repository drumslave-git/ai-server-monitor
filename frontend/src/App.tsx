import { useEffect, useState } from "react";
import { OverviewPage } from "./pages/Overview";
import { SettingsPage } from "./pages/Settings";

const PAGES = [
  { key: "overview", title: "Overview" },
  { key: "settings", title: "Settings" },
] as const;

function currentPage(): string {
  return location.hash.replace(/^#\/?/, "").split("/")[0] || "overview";
}

export function App() {
  const [page, setPage] = useState(currentPage);

  useEffect(() => {
    const onHash = () => setPage(currentPage());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">AI Server Monitor</span>
        <nav>
          {PAGES.map((p) => (
            <a key={p.key} href={`#/${p.key}`} className={page === p.key ? "active" : undefined}>
              {p.title}
            </a>
          ))}
        </nav>
      </header>
      <main className="content">{page === "settings" ? <SettingsPage /> : <OverviewPage />}</main>
    </div>
  );
}
