import { useState } from "react";
import { api, ApiError } from "../../api";
import { Panel } from "../../ui";
import { shortDate } from "./format";
import type { SnapshotInfo } from "./types";

/** Capture this server's current collector data as a private snapshot (kept in the database,
 *  never written to the repo) that scenarios can run against as `private:<name>`. */
export function SnapshotsPanel({ snapshots, reload }: { snapshots: SnapshotInfo[]; reload: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const capture = async () => {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const out = await api.post<{ reference: string; collectors: string[] }>("/api/bench/snapshots", { name: name.trim() });
      setDone(`Captured ${out.collectors.length} collectors as ${out.reference}`);
      setName("");
      reload();
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : (e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const remove = async (n: string) => {
    if (!confirm(`Delete the snapshot ${n}?`)) return;
    await api.del(`/api/bench/snapshots/${encodeURIComponent(n)}`);
    reload();
  };

  return (
    <Panel title="Private snapshots" aside={<span className="muted small">optional</span>}>
      <p className="help">
        Freeze what this server looks like right now (collector data and a little history) so scenarios can run against it as <span className="mono">snapshot: "private:name"</span>. It is stored in this
        monitor's database only and never leaves it.
      </p>
      <form
        className="snapshot-form"
        onSubmit={(e) => {
          e.preventDefault();
          void capture();
        }}
      >
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="snapshot name, e.g. home" pattern="[A-Za-z0-9][A-Za-z0-9_\-]{0,39}" aria-label="Snapshot name" />
        <button className="btn" disabled={busy || !name.trim()}>Capture this server</button>
      </form>
      {error && <p className="error">{error}</p>}
      {done && <p className="ok-text">{done}</p>}
      {snapshots.length > 0 && (
        <ul className="snapshot-list">
          {snapshots.map((s) => (
            <li key={s.name}>
              <span className="mono">private:{s.name}</span>
              <span className="muted small"> · {shortDate(s.created)} · {(s.size / 1024).toFixed(0)} KB</span>
              <button className="btn btn-quiet" onClick={() => void remove(s.name)}>Delete</button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
