import { MessageCircleQuestion } from "lucide-react";

/** A job run (or one of its findings) to explain in a new chat conversation. */
export type ExplainRequest = { runId: number; finding: number | null };

let pending: ExplainRequest | null = null;

/** The Chat page takes the request once when it opens (a reload does not repeat it). */
export function takeExplain(): ExplainRequest | null {
  const request = pending;
  pending = null;
  return request;
}

/** Opens a new chat in which the main model explains the run or finding from its source data. */
export function ExplainButton({ runId, finding = null }: { runId: number; finding?: number | null }) {
  const what = finding === null ? "this run" : "this finding";
  return (
    <button
      type="button"
      className="btn btn-sm btn-quiet btn-explain"
      title={`Open a new chat that explains ${what} in detail`}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        pending = { runId, finding };
        location.hash = "#/chat/new";
      }}
    >
      <MessageCircleQuestion size={14} aria-hidden />
      Explain
    </button>
  );
}
