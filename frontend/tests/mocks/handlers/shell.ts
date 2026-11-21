// The calls the shell of the app makes on every page, whichever page it shows.
import { http, HttpResponse } from "msw";
import { automationRunning, hardware, info, settingsShell, status } from "../fixtures/shell";

export const shellHandlers = [
  http.get("/api/info", () => HttpResponse.json(info)),
  http.get("/api/status", () => HttpResponse.json(status)),
  http.get("/api/settings", () => HttpResponse.json(settingsShell)),
  http.get("/api/automation", () => HttpResponse.json(automationRunning)),
  http.get("/api/hardware", () => HttpResponse.json(hardware)),
  // The live stream: an open, empty event stream (it never carries an event in tests).
  http.get("/api/stream", () => new HttpResponse(": open\nretry: 3600000\n\n", { headers: { "Content-Type": "text/event-stream" } })),
];
