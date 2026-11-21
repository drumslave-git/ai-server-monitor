// The text helpers of the Logs pages: durations, counts, tokens, time stamps, message bodies.
import { describe, expect, it } from "vitest";
import { approxTokens, clockTime, messageText, ms, num, prettyJson, purposeLabel, stamp, tokens } from "../../../src/pages/logs/format";

describe("ms", () => {
  it("shows a dash for a missing value", () => {
    expect(ms(null)).toBe("—");
    expect(ms(undefined)).toBe("—");
  });

  it("says less than a millisecond for a fraction", () => {
    expect(ms(0.4)).toBe("<1 ms");
    expect(ms(0)).toBe("<1 ms");
  });

  it("rounds milliseconds below a second", () => {
    expect(ms(1)).toBe("1 ms");
    expect(ms(250.6)).toBe("251 ms");
    expect(ms(999)).toBe("999 ms");
  });

  it("shows seconds with two decimals below ten seconds and one decimal above", () => {
    expect(ms(1000)).toBe("1.00 s");
    expect(ms(1500)).toBe("1.50 s");
    expect(ms(9999)).toBe("10.00 s");
    expect(ms(12_500)).toBe("12.5 s");
  });

  it("shows minutes and seconds from a minute on", () => {
    expect(ms(60_000)).toBe("1m 0s");
    expect(ms(125_000)).toBe("2m 5s");
  });

  it("never shows 60 seconds: a value that rounds up to a minute reads as the minute", () => {
    expect(ms(119_600)).toBe("2m 0s");
    expect(ms(59_960)).toBe("1m 0s");
    expect(ms(59_949)).toBe("59.9 s");
    expect(ms(179_499)).toBe("2m 59s");
  });
});

describe("num", () => {
  it("shows a dash for a missing value and groups digits otherwise", () => {
    expect(num(null)).toBe("—");
    expect(num(undefined)).toBe("—");
    expect(num(0)).toBe("0");
    expect(num(1234567)).toBe((1234567).toLocaleString());
  });
});

describe("tokens", () => {
  const base = { prompt_tokens: 1200, completion_tokens: 300, usage_estimated: false };

  it("reads 'in → out'", () => {
    expect(tokens(base)).toBe(`${(1200).toLocaleString()} → 300`);
  });

  it("marks an estimate", () => {
    expect(tokens({ ...base, usage_estimated: true })).toMatch(/ → 300 \(est\.\)$/);
  });

  it("is a dash when neither count is known", () => {
    expect(tokens({ prompt_tokens: null, completion_tokens: null, usage_estimated: true })).toBe("—");
  });

  it("keeps the known side when only one count is known", () => {
    expect(tokens({ prompt_tokens: 50, completion_tokens: null, usage_estimated: false })).toBe("50 → —");
  });
});

describe("approxTokens", () => {
  it("counts four characters to a token and never less than one", () => {
    expect(approxTokens(400)).toBe(100);
    expect(approxTokens(10)).toBe(3);
    expect(approxTokens(0)).toBe(1);
    expect(approxTokens(1)).toBe(1);
  });
});

describe("stamp and clockTime", () => {
  const ts = Date.UTC(2026, 10, 20, 13, 5, 9) / 1000;

  it("stamp is the day, a comma and the time with seconds", () => {
    expect(stamp(ts)).toMatch(/^[^,]*\d[^,]*, .*\d{2}.*\d{2}.*\d{2}/);
    expect(stamp(ts)).toContain(new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }));
  });

  it("clockTime is the time of day with seconds in 24 hours", () => {
    expect(clockTime(ts)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(clockTime(ts)).toBe(new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false }));
  });
});

describe("messageText", () => {
  it("returns a string as it is", () => {
    expect(messageText("hello")).toBe("hello");
  });

  it("joins the text of the parts of a list, one per line", () => {
    expect(messageText([{ type: "text", text: "one" }, { type: "image_url" }, { type: "text", text: "two" }])).toBe("one\n\ntwo");
  });

  it("is empty for nothing", () => {
    expect(messageText(null)).toBe("");
    expect(messageText(undefined)).toBe("");
  });

  it("prints any other value as JSON", () => {
    expect(messageText({ a: 1 })).toBe('{\n  "a": 1\n}');
  });
});

describe("prettyJson", () => {
  it("indents JSON text", () => {
    expect(prettyJson('{"a":[1,2]}')).toBe('{\n  "a": [\n    1,\n    2\n  ]\n}');
  });

  it("returns text that is not JSON as it is", () => {
    expect(prettyJson("plain words")).toBe("plain words");
  });
});

describe("purposeLabel", () => {
  it("names the purposes in use", () => {
    expect(purposeLabel("chat")).toBe("Chat");
    expect(purposeLabel("chat_title")).toBe("Chat title");
    expect(purposeLabel("subagent")).toBe("Worker sub-task");
    expect(purposeLabel("job")).toBe("Job");
    expect(purposeLabel("test")).toBe("Connection test");
    expect(purposeLabel("other")).toBe("Other");
  });

  it("gives the purposes of removed features one neutral name", () => {
    expect(purposeLabel("benchmark")).toBe("Earlier feature");
  });
});
