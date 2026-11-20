import { IconButton } from "../kit";

/** A job run (or one of its findings) to explain in a new chat conversation. */
export type ExplainRequest = { runId: number; finding: number | null };

let pending: ExplainRequest | null = null;

/** The Chat page takes the request once when it opens (a reload does not repeat it). */
export function takeExplain(): ExplainRequest | null {
  const request = pending;
  pending = null;
  return request;
}

/** Opens a new chat in which the main model explains the run or finding from its source data.
 *  small: beside each finding. */
export function ExplainButton({ runId, finding = null, small }: { runId: number; finding?: number | null; small?: boolean }) {
  const what = finding === null ? "this run" : "this finding";
  return (
    <IconButton
      icon="explain"
      small={small}
      label="explain"
      hint={`Explain ${what} in a new chat`}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        pending = { runId, finding };
        location.hash = "#/chat/new";
      }}
    />
  );
}
