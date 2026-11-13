import { useEffect, useState } from "react";

type Info = {
  version: string;
  uptime_s: number;
  llm: { model: string; tool_mode: { effective: string | null } };
  jobs: { name: string; runs: number; last_error: string | null }[];
};

// Placeholder until the real dashboard (milestone M6).
export function App() {
  const [info, setInfo] = useState<Info | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/info")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setInfo)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", maxWidth: 640, margin: "2rem auto", padding: "0 16px" }}>
      <h1>Server Monitor</h1>
      {error && <p>Backend unreachable: {error}</p>}
      {info && (
        <>
          <p>
            v{info.version} · up {Math.round(info.uptime_s)}s · model {info.llm.model} · tools{" "}
            {info.llm.tool_mode.effective ?? "probing…"}
          </p>
          <ul>
            {info.jobs.map((j) => (
              <li key={j.name}>
                {j.name}: {j.runs} runs{j.last_error ? ` — ${j.last_error}` : ""}
              </li>
            ))}
          </ul>
        </>
      )}
    </main>
  );
}
