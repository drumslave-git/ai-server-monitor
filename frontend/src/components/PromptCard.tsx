import { useState } from "react";
import { api, type Schemas } from "../api";
import { Approval, Button, Checkbox, Column, ErrorText, Field, Input, Radio, Text, Textarea } from "../kit";

export type ChatCard = Schemas["ChatCard"];
type Answer = Schemas["Answer"];

/** A question of a card (the server stores them as plain objects). */
type Question = { text: string; kind: "single" | "multi" | "text"; options: string[] };

/** What the owner has put in for one question so far. */
type Draft = { chosen: string[]; own: boolean; text: string };

const EMPTY: Draft = { chosen: [], own: false, text: "" };

const questionsOf = (card: ChatCard): Question[] =>
  (card.questions as Partial<Question>[]).map((q) => ({
    text: String(q.text ?? ""),
    kind: q.kind === "multi" || q.kind === "text" ? q.kind : "single",
    options: Array.isArray(q.options) ? q.options.map(String) : [],
  }));

/** The answer a draft stands for, or null while the question is not answered. */
function answerOf(q: Question, d: Draft): Answer | null {
  const text = d.text.trim();
  if (q.kind === "text") return text ? { chosen: [], text } : null;
  const own = d.own ? text : "";
  if (!d.chosen.length && !own) return null;
  return { chosen: d.chosen, text: own };
}

/** "smart, scrub, the cables": an answer in words. */
const said = (a: Answer): string => [...(a.chosen ?? []), ...(a.text ? [a.text] : [])].join(", ");

const VIA: Record<string, string> = { web: "answered in the web chat", telegram: "answered in Telegram" };

/** The questions the model asked the owner (`ask_owner`): the controls while the card is open, what was
 *  answered afterwards. The run behind it continues when the answers are sent, from here or from Telegram. */
export function PromptCard({ card, onAnswered }: { card: ChatCard; onAnswered?: () => void }) {
  const questions = questionsOf(card);
  const [drafts, setDrafts] = useState<Draft[]>(() => questions.map(() => EMPTY));
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = card.status === "open";

  if (!open)
    return (
      <Approval title={questions.length === 1 ? "Question" : "Questions"} decided inset note={VIA[card.answered_via ?? ""] ?? "answered"}>
        {questions.map((q, i) => (
          <Column key={i} gap={1}>
            <Text block>{q.text}</Text>
            <Text block kind="data">
              {card.answers?.[i] ? said(card.answers[i]) : "—"}
            </Text>
          </Column>
        ))}
      </Approval>
    );

  const change = (i: number, patch: Partial<Draft>) => setDrafts((all) => all.map((d, k) => (k === i ? { ...d, ...patch } : d)));
  const answers = questions.map((q, i) => answerOf(q, drafts[i]));
  const complete = answers.every((a) => a !== null);

  const send = async () => {
    if (!complete || sending) return;
    setSending(true);
    setError(null);
    try {
      await api.post(`/api/chat/cards/${card.id}/answer`, { answers });
      onAnswered?.();
    } catch (e) {
      setError((e as Error).message);
      // (answered already, in Telegram: the thread shows it as it is now)
      if ((e as { status?: number }).status === 409) onAnswered?.();
    } finally {
      setSending(false);
    }
  };

  return (
    <Approval
      title={questions.length === 1 ? "Question" : "Questions"}
      note="The answer goes back to the model."
      actions={
        <Button variant="primary" icon="send" disabled={!complete || sending} onClick={() => void send()}>
          send answers
        </Button>
      }
    >
      {questions.map((q, i) => (
        <QuestionControls key={i} card={card.id} index={i} q={q} draft={drafts[i]} many={questions.length > 1} onChange={(p) => change(i, p)} />
      ))}
      {error && <ErrorText>{error}</ErrorText>}
    </Approval>
  );
}

function QuestionControls({
  card,
  index,
  q,
  draft,
  many,
  onChange,
}: {
  card: number;
  index: number;
  q: Question;
  draft: Draft;
  many: boolean;
  onChange: (patch: Partial<Draft>) => void;
}) {
  const label = many ? `${index + 1}. ${q.text}` : q.text;
  const name = `card-${card}-${index}`;
  if (q.kind === "text")
    return (
      <Field label={label}>
        <Textarea rows={2} value={draft.text} onChange={(e) => onChange({ text: e.target.value })} />
      </Field>
    );
  const toggle = (option: string, on: boolean) =>
    onChange({ chosen: on ? [...draft.chosen, option] : draft.chosen.filter((o) => o !== option) });
  return (
    <Field label={label} group help={q.kind === "multi" ? "any of these" : undefined}>
      {q.options.map((option) =>
        q.kind === "multi" ? (
          <Checkbox key={option} label={option} checked={draft.chosen.includes(option)} onChange={(on) => toggle(option, on)} />
        ) : (
          <Radio key={option} name={name} label={option} checked={draft.chosen[0] === option && !draft.own} onChange={() => onChange({ chosen: [option], own: false })} />
        ),
      )}
      {q.kind === "multi" ? (
        <Checkbox label="own answer" checked={draft.own} onChange={(on) => onChange({ own: on })} />
      ) : (
        <Radio name={name} label="own answer" checked={draft.own} onChange={() => onChange({ chosen: [], own: true })} />
      )}
      {draft.own && <Input aria-label={`Own answer to: ${q.text}`} autoFocus value={draft.text} onChange={(e) => onChange({ text: e.target.value })} />}
    </Field>
  );
}
