import { useCallback, useEffect, useState } from "react";
import { api, type Schemas } from "../api";
import { Button, Row, Status, TestLine } from "../kit";

type State = Schemas["McpOAuthState"];

/** Sends the browser to the server's login page (an object so that tests can look at where it goes). */
export const leave = { to: (url: string) => window.location.assign(url) };

/** The sign-in line of an MCP server that uses OAuth (Auth = oauth): whether a login is saved, the
 *  last error, and the buttons. Connect sends the browser to the server's login; the server sends it
 *  back to the app's callback, which lands on this card again (#/settings/mcp/<name>). */
export function McpSignIn({ name }: { name: string }) {
  const [state, setState] = useState<State | null>(null);
  const [busy, setBusy] = useState<"connect" | "disconnect" | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const all = await api.get<State[]>("/api/mcp/oauth");
      setState(all.find((s) => s.name === name) ?? null);
    } catch (e) {
      setFailed(`Could not read the sign-in state: ${(e as Error).message}`);
    }
  }, [name]);
  useEffect(() => {
    void load();
  }, [load]);

  const connect = async () => {
    setBusy("connect");
    setFailed(null);
    try {
      const { authorize_url } = await api.post<Schemas["McpOAuthStart"]>(`/api/mcp/oauth/${encodeURIComponent(name)}/connect`, {});
      leave.to(authorize_url);
    } catch (e) {
      setFailed((e as Error).message);
      setBusy(null);
      void load();
    }
  };
  const disconnect = async () => {
    setBusy("disconnect");
    setFailed(null);
    try {
      await api.del(`/api/mcp/oauth/${encodeURIComponent(name)}`);
    } catch (e) {
      setFailed((e as Error).message);
    }
    setBusy(null);
    await load();
  };

  const connected = state?.connected === true;
  const error = failed ?? state?.error ?? null;
  return (
    <>
      <Row>
        <Status tone={connected ? "ok" : "off"}>{connected ? "connected" : "not connected"}</Status>
        {(!connected || error) && (
          <Button disabled={busy !== null} onClick={() => void connect()}>
            {busy === "connect" ? "connecting…" : connected ? "connect again" : "connect"}
          </Button>
        )}
        {connected && (
          <Button disabled={busy !== null} onClick={() => void disconnect()}>
            {busy === "disconnect" ? "disconnecting…" : "disconnect"}
          </Button>
        )}
      </Row>
      {error && <TestLine ok={false}>{error}</TestLine>}
    </>
  );
}
