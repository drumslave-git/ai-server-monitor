// The global "pause automation" switch: shared state, the switch in the rail, the notice shown
// while paused, and the hook.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "./api";
import { Button, ErrorText, Notice, Status } from "./kit";
import { useStream } from "./live";

export type AutomationState = {
  paused: boolean;
  since: number | null;
  /** Who paused it: "ui", "api" or "telegram". */
  by: string | null;
  /** Approved actions waiting for the hourly limit: they run again after the resume. */
  queued_actions: number;
};

type Value = { state: AutomationState | null; busy: boolean; error: string | null; pause: () => void; resume: () => void };

const Ctx = createContext<Value>({ state: null, busy: false, error: null, pause: () => undefined, resume: () => undefined });

export function AutomationProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AutomationState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api.get<AutomationState>("/api/automation").then(
        (s) => setState(s),
        () => undefined,
      ),
    [],
  );
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load]);
  useStream(["automation", "approval", "action"], () => void load());

  const change = useCallback(async (what: "pause" | "resume") => {
    setBusy(true);
    setError(null);
    try {
      setState(await api.post<AutomationState>(`/api/automation/${what}`, {}));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  const value = useMemo<Value>(
    () => ({ state, busy, error, pause: () => void change("pause"), resume: () => void change("resume") }),
    [state, busy, error, change],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const useAutomation = () => useContext(Ctx);

/** "17:42", with the day when it was not today. */
export function sinceText(ts: number): string {
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? time : `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${time}`;
}

/** At the foot of the rail on every page: whether automation runs, and the button that stops or resumes it. */
export function AutomationSwitch() {
  const { state, busy, pause, resume } = useAutomation();
  if (!state) return null;
  return (
    <>
      <Status tone={state.paused ? "warn" : "ok"} plain>
        {state.paused ? "automation paused" : "automation running"}
      </Status>
      {state.paused ? (
        <Button block disabled={busy} onClick={resume}>
          resume automation
        </Button>
      ) : (
        <Button block disabled={busy} onClick={pause} title="Stop every scheduled job and automation">
          pause automation
        </Button>
      )}
    </>
  );
}

/** On top of every page while automation is paused: what is stopped, and the way back. */
export function AutomationNotice() {
  const { state, busy, error, resume } = useAutomation();
  if (!state) return null;
  if (!state.paused) return error ? <ErrorText>{error}</ErrorText> : null;
  return (
    <Notice
      title={`Automation paused${state.since ? ` since ${sinceText(state.since)}` : ""}`}
      action={
        <Button variant="primary" disabled={busy} onClick={resume}>
          resume
        </Button>
      }
    >
      Scheduled jobs and automations are stopped: no scheduled agent runs. Chat, approvals and the Telegram bot keep working.
      {state.queued_actions > 0 && ` ${state.queued_actions} approved action${state.queued_actions > 1 ? "s" : ""} wait${state.queued_actions > 1 ? "" : "s"} and will run after the resume.`}
      {error && <ErrorText>{error}</ErrorText>}
    </Notice>
  );
}
