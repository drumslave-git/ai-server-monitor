// Stand-alone harness for the Logs page (not part of the app build): the page inside the real
// shell styles, routed by the hash like the app does. Serve it with a Vite config that proxies
// /api to scripts/dev/trace_demo.py, then open /dev/logs.html#/logs/llm.
import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { LogsPage } from "../src/pages/Logs";
import "../src/styles.css";

function Shell() {
  const [route, setRoute] = useState(() => location.hash.replace(/^#\/?/, "").split("/").filter(Boolean));
  useEffect(() => {
    const on = () => setRoute(location.hash.replace(/^#\/?/, "").split("/").filter(Boolean));
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">AI Server Monitor</div>
        <nav aria-label="Main">
          <a href="#/logs" className="active">Logs</a>
        </nav>
      </aside>
      <main className="content">
        <LogsPage params={route.slice(1)} />
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Shell />);
