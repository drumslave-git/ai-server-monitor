import { render } from "@testing-library/react";
import { ChatPage } from "../../../src/pages/Chat";

/** The Chat page opened at a hash ("#/chat", "#/chat/new", "#/chat/s-disk"). */
export function renderChat(hash = "#/chat") {
  history.replaceState(null, "", hash);
  return render(<ChatPage />);
}

/** The JSON bodies and paths of the requests a handler received, in order. */
export type Seen = { path: string; body: unknown }[];
