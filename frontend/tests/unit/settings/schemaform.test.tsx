// src/components/SchemaForm.tsx: the form drawn from the backend's JSON schema. Every kind of field is rendered
// from a small invented schema and its emitted value is read back: ObjectFields has no state of its own, so a
// harness holds the value the way the Settings page does.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Json, JsonObject, JsonSchema } from "../../../src/api";
import { defaultFor, humanize, ObjectFields, resolve, type FormContext } from "../../../src/components/SchemaForm";
import { server } from "../../mocks/server";

type Rig = { value: Json; emitted: Json[] };

function Harness({ schema, initial, ctx, rig }: { schema: JsonSchema; initial: Json; ctx: FormContext; rig: Rig }) {
  const [value, setValue] = useState<Json>(initial);
  return (
    <ObjectFields
      schema={schema}
      value={value}
      path=""
      ctx={ctx}
      onChange={(v) => {
        rig.value = v;
        rig.emitted.push(v);
        setValue(v);
      }}
    />
  );
}

/** A form of `properties` holding `initial`; `rig.value` is the last value the form emitted. */
function setup(properties: Record<string, JsonSchema>, initial: JsonObject = {}, ctx: Partial<FormContext> = {}) {
  const rig: Rig = { value: initial, emitted: [] };
  const schema: JsonSchema = { type: "object", properties };
  const full: FormContext = { defs: {}, errors: {}, suggestions: {}, ...ctx };
  const view = render(<Harness schema={schema} initial={initial} ctx={full} rig={rig} />);
  return { rig, user: userEvent.setup(), ...view };
}

const byLabel = (name: string) => screen.getByLabelText(new RegExp(`^${name}`));

const str = (extra: Partial<JsonSchema> = {}): JsonSchema => ({ type: "string", ...extra });

describe("humanize", () => {
  it("makes a title of a key: capital first letter, spaces, known acronyms", () => {
    expect(humanize("max_output_tokens")).toBe("Max output tokens");
    expect(humanize("base_url")).toBe("Base URL");
    expect(humanize("llm_model")).toBe("LLM model");
    expect(humanize("api_key")).toBe("API key");
    expect(humanize("chat_ids")).toBe("Chat IDs");
    expect(humanize("mcp")).toBe("MCP");
    expect(humanize("df")).toBe("Disk usage");
    expect(humanize("n")).toBe("N");
  });

  it("keeps a word's own capitals at the start", () => {
    expect(humanize("iPhone_name")).toBe("iPhone name");
    expect(humanize("Name")).toBe("Name");
  });
});

describe("resolve", () => {
  const defs: Record<string, JsonSchema> = {
    Inner: { type: "object", description: "from def", properties: { a: { type: "string" } } },
    Outer: { $ref: "#/$defs/Inner", default: {} },
  };

  it("returns a node without a reference as it is", () => {
    const node: JsonSchema = { type: "string" };
    expect(resolve(node, defs)).toBe(node);
  });

  it("replaces a reference by its definition, and the node's own words win over the definition's", () => {
    expect(resolve({ $ref: "#/$defs/Inner" }, defs)).toEqual(defs.Inner);
    expect(resolve({ $ref: "#/$defs/Inner", description: "mine" }, defs)).toEqual({ ...defs.Inner, description: "mine" });
  });

  it("follows a reference to a reference", () => {
    expect(resolve({ $ref: "#/$defs/Outer" }, defs)).toEqual({ type: "object", description: "from def", properties: { a: { type: "string" } }, default: {} });
  });

  it("leaves only the node's own words for a definition that is missing", () => {
    expect(resolve({ $ref: "#/$defs/Nope", description: "x" }, defs)).toEqual({ description: "x" });
  });
});

describe("defaultFor", () => {
  const defs: Record<string, JsonSchema> = { Box: { type: "object", properties: { n: { type: "integer" }, on: { type: "boolean", default: true } } } };

  it("takes the schema's default, as a copy", () => {
    const schema: JsonSchema = { type: "object", default: { a: [1] } };
    const out = defaultFor(schema, {});
    expect(out).toEqual({ a: [1] });
    expect(out).not.toBe(schema.default);
  });

  it("makes an empty value of each type", () => {
    expect(defaultFor({ type: "string" }, {})).toBe("");
    expect(defaultFor({ type: "integer" }, {})).toBe(0);
    expect(defaultFor({ type: "number" }, {})).toBe(0);
    expect(defaultFor({ type: "boolean" }, {})).toBe(false);
    expect(defaultFor({ type: "array" }, {})).toEqual([]);
    expect(defaultFor({ type: "object" }, {})).toEqual({});
    expect(defaultFor({}, {})).toBe("");
  });

  it("builds an object from the defaults of its properties, through references", () => {
    expect(defaultFor({ $ref: "#/$defs/Box" }, defs)).toEqual({ n: 0, on: true });
  });

  it("takes the first choice that is not null of a nullable one", () => {
    expect(defaultFor({ anyOf: [{ type: "null" }, { type: "integer" }] }, {})).toBe(0);
    expect(defaultFor({ anyOf: [{ $ref: "#/$defs/Box" }, { type: "null" }] }, defs)).toEqual({ n: 0, on: true });
  });
});

describe("text field", () => {
  it("shows its label (lowercase), its help line and its value, and gives what is typed", async () => {
    const { rig, user } = setup({ base_url: str({ description: "Where the API lives." }) }, { base_url: "http://x.test" });
    const input = screen.getByLabelText(/^base url/);
    expect(input).toHaveValue("http://x.test");
    expect(screen.getByText("Where the API lives.")).toBeInTheDocument();
    await user.type(input, "/v1");
    expect(rig.value).toEqual({ base_url: "http://x.test/v1" });
    expect(input).toHaveValue("http://x.test/v1");
  });

  it("is empty for a value that is not there", () => {
    setup({ name: str() }, {});
    expect(byLabel("name")).toHaveValue("");
  });

  it("shows the placeholder its path has in the context", () => {
    setup({ name: str() }, {}, { placeholders: { name: "main model's endpoint" } });
    expect(byLabel("name")).toHaveAttribute("placeholder", "main model's endpoint");
  });

  it("offers the suggestions of its path as a list the input points at", () => {
    setup({ model: str() }, {}, { suggestions: { model: ["alpha-1", "beta-2"] } });
    const input = byLabel("model");
    const listId = input.getAttribute("list");
    expect(listId).toBeTruthy();
    const options = screen.getAllByRole("option", { hidden: true });
    expect(options.map((o) => o.getAttribute("value"))).toEqual(["alpha-1", "beta-2"]);
    expect(options[0].parentElement).toHaveAttribute("id", listId!);
  });

  it("has no list when there is no suggestion", () => {
    setup({ model: str() }, {}, { suggestions: { model: [] } });
    expect(byLabel("model")).not.toHaveAttribute("list");
    expect(screen.queryAllByRole("option", { hidden: true })).toHaveLength(0);
  });
});

describe("errors of the server", () => {
  it("show under the field and mark the control invalid, for every kind of control", () => {
    const six = { type: "string", enum: ["a", "b", "c", "d", "e", "f"] } as JsonSchema;
    setup(
      {
        alpha: str(),
        bravo: { type: "integer" },
        charlie: str({ format: "password" }),
        delta: str({ format: "duration" }),
        echo: str({ format: "multiline" }),
        foxtrot: { type: "array", items: { type: "string" } },
        golf: { type: "object", additionalProperties: true },
        hotel: six,
        india: { anyOf: [{ type: "number" }, { type: "string", const: "auto" }] },
        fine: str(),
      },
      { alpha: "", bravo: 1, charlie: "", delta: "5m", echo: "", foxtrot: [], golf: {}, hotel: "a", india: "auto", fine: "" },
      { errors: Object.fromEntries(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"].map((k) => [k, `Bad ${k}`])) },
    );
    for (const key of ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"]) {
      expect(screen.getByText(`Bad ${key}`)).toBeInTheDocument();
      expect(byLabel(key)).toHaveAttribute("aria-invalid", "true");
    }
    expect(byLabel("fine")).not.toHaveAttribute("aria-invalid");
  });

  it("show under a switch and a set of choices too (they are not one input)", () => {
    setup(
      { on: { type: "boolean" }, mode: { type: "string", enum: ["a", "b"] }, auto: { anyOf: [{ type: "boolean" }, { type: "string", const: "auto" }] } },
      { on: false, mode: "a", auto: "auto" },
      { errors: { on: "Bad on", mode: "Bad mode", auto: "Bad auto" } },
    );
    expect(screen.getByText("Bad on")).toBeInTheDocument();
    expect(screen.getByText("Bad mode")).toBeInTheDocument();
    expect(screen.getByText("Bad auto")).toBeInTheDocument();
  });
});

describe("number field", () => {
  it("shows the number with the limits of the schema, and gives a number (not a string)", async () => {
    const { rig, user } = setup({ steps: { type: "integer", minimum: 1, maximum: 60 } }, { steps: 5 });
    const input = byLabel("steps");
    expect(input).toHaveValue(5);
    expect(input).toHaveAttribute("min", "1");
    expect(input).toHaveAttribute("max", "60");
    expect(input).toHaveAttribute("step", "1");
    await user.clear(input);
    await user.type(input, "12");
    expect(rig.value).toEqual({ steps: 12 });
  });

  it("steps by any amount for a number that is not an integer", () => {
    setup({ ratio: { type: "number" } }, { ratio: 0.5 });
    expect(byLabel("ratio")).toHaveAttribute("step", "any");
  });

  it("gives null when it is emptied, and is empty for a null", async () => {
    const { rig, user } = setup({ steps: { type: "integer" }, other: { type: "integer" } }, { steps: 5, other: null });
    expect(byLabel("other")).toHaveValue(null);
    await user.clear(byLabel("steps"));
    expect(rig.value).toEqual({ steps: null, other: null });
  });
});

describe("a value that may be null", () => {
  it("is drawn as its type, with the node's own description, and an empty box gives null", async () => {
    const { rig, user } = setup({ limit: { anyOf: [{ type: "integer", minimum: 1 }, { type: "null" }], description: "At most this many." } }, { limit: 3 });
    const input = byLabel("limit");
    expect(input).toHaveAttribute("type", "number");
    expect(input).toHaveAttribute("min", "1");
    expect(input).toHaveValue(3);
    expect(screen.getByText("At most this many.")).toBeInTheDocument();
    await user.clear(input);
    expect(rig.value).toEqual({ limit: null });
  });
});

describe("number or auto", () => {
  const schema: JsonSchema = { anyOf: [{ type: "number", minimum: 0, maximum: 2 }, { type: "string", const: "auto" }], description: "Randomness." };

  it("is an empty box that says auto while the value is auto", () => {
    setup({ temperature: schema }, { temperature: "auto" });
    const input = byLabel("temperature");
    expect(input).toHaveValue(null);
    expect(input).toHaveAttribute("placeholder", "auto");
    expect(input).toHaveAttribute("min", "0");
    expect(input).toHaveAttribute("max", "2");
    expect(input).toHaveAttribute("step", "any");
  });

  it("shows a number, gives a number, and goes back to auto when it is emptied", async () => {
    const { rig, user } = setup({ temperature: schema }, { temperature: 0.7 });
    const input = byLabel("temperature");
    expect(input).toHaveValue(0.7);
    await user.clear(input);
    expect(rig.value).toEqual({ temperature: "auto" });
    await user.type(input, "1");
    expect(rig.value).toEqual({ temperature: 1 });
  });

  it("steps by 1 when the number is an integer", () => {
    setup({ top_k: { anyOf: [{ type: "integer", minimum: 0 }, { type: "string", const: "auto" }] } }, { top_k: "auto" });
    expect(byLabel("top k")).toHaveAttribute("step", "1");
  });
});

describe("switch", () => {
  it("shows its state and flips it", async () => {
    const { rig, user } = setup({ enabled: { type: "boolean", description: "Turn it on." } }, { enabled: false });
    const toggle = screen.getByRole("switch", { name: "Enabled" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("group", { name: "enabled" })).toBeInTheDocument();
    expect(screen.getByText("Turn it on.")).toBeInTheDocument();
    await user.click(toggle);
    expect(rig.value).toEqual({ enabled: true });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    expect(rig.value).toEqual({ enabled: false });
  });

  it("counts a value that is not there as off", () => {
    setup({ enabled: { type: "boolean" } }, {});
    expect(screen.getByRole("switch", { name: "Enabled" })).toHaveAttribute("aria-checked", "false");
  });
});

describe("boolean or auto", () => {
  const schema: JsonSchema = { anyOf: [{ type: "boolean" }, { type: "string", const: "auto" }] };

  it.each([
    ["auto", "auto"],
    [true, "on"],
    [false, "off"],
  ] as const)("shows %s as %s", (value, chosen) => {
    setup({ thinking: schema }, { thinking: value });
    expect(screen.getAllByRole("radio").map((r) => r.textContent)).toEqual(["auto", "on", "off"]);
    expect(screen.getByRole("radio", { name: chosen })).toHaveAttribute("aria-checked", "true");
  });

  it("gives auto, true or false for what is clicked", async () => {
    const { rig, user } = setup({ thinking: schema }, { thinking: "auto" });
    await user.click(screen.getByRole("radio", { name: "on" }));
    expect(rig.value).toEqual({ thinking: true });
    await user.click(screen.getByRole("radio", { name: "off" }));
    expect(rig.value).toEqual({ thinking: false });
    await user.click(screen.getByRole("radio", { name: "auto" }));
    expect(rig.value).toEqual({ thinking: "auto" });
  });

  it("moves with the arrow keys, round the ends", async () => {
    vi.stubGlobal("requestAnimationFrame", () => 0); // (the focus move after the key is tested apart)
    try {
      const { rig, user } = setup({ thinking: schema }, { thinking: "auto" });
      screen.getByRole("radio", { name: "auto" }).focus();
      await user.keyboard("{ArrowLeft}");
      expect(rig.value).toEqual({ thinking: false });
      await user.keyboard("{ArrowRight}");
      expect(rig.value).toEqual({ thinking: "auto" });
      await user.keyboard("{ArrowRight}");
      expect(rig.value).toEqual({ thinking: true });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("moves the focus to the chosen option after an arrow key (the frame callback reads a cleared currentTarget)", async () => {
    // (the frame runs after the event: the handler must not read e.currentTarget in it, React has cleared it by then)
    const frames: (() => void)[] = [];
    vi.stubGlobal("requestAnimationFrame", (f: () => void) => frames.push(f));
    try {
      const { user } = setup({ thinking: schema }, { thinking: "auto" });
      screen.getByRole("radio", { name: "auto" }).focus();
      await user.keyboard("{ArrowRight}");
      frames.forEach((f) => f());
      expect(screen.getByRole("radio", { name: "on" })).toHaveFocus();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("choice", () => {
  it("is a set of buttons for up to five values", async () => {
    const values = ["a", "b", "c", "d", "e"];
    const { rig, user } = setup({ mode: { type: "string", enum: values } }, { mode: "b" });
    expect(screen.getByRole("radiogroup", { name: "Mode" })).toBeInTheDocument();
    expect(screen.getAllByRole("radio").map((r) => r.textContent)).toEqual(values);
    expect(screen.getByRole("radio", { name: "b" })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "e" }));
    expect(rig.value).toEqual({ mode: "e" });
  });

  it("is a select for more than five", async () => {
    const values = ["a", "b", "c", "d", "e", "f"];
    const { rig, user } = setup({ mode: { type: "string", enum: values } }, { mode: "b" });
    const select = byLabel("mode");
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(values);
    expect(select).toHaveValue("b");
    await user.selectOptions(select, "f");
    expect(rig.value).toEqual({ mode: "f" });
    expect(select).toHaveValue("f");
  });

  it("gives a number for a choice of numbers (the schema says integer, the form sends text)", async () => {
    // (no setting of the backend has an integer enum today; the buttons and the select speak text, the form gets the value)
    const { rig, user } = setup({ level: { type: "integer", enum: [1, 2, 3] } }, { level: 1 });
    await user.click(screen.getByRole("radio", { name: "2" }));
    expect(rig.value).toEqual({ level: 2 });
  });

  it("gives a number from the select too, when the choice is long", async () => {
    const { rig, user } = setup({ level: { type: "integer", enum: [1, 2, 3, 4, 5, 6] } }, { level: 1 });
    await user.selectOptions(byLabel("level"), "5");
    expect(rig.value).toEqual({ level: 5 });
  });
});

describe("secret", () => {
  // (the button stands inside the field's <label>, so the label text finds it too: ask for the input)
  const keyInput = () => screen.getByLabelText(/^api key/, { selector: "input" });
  const clearButton = () => screen.getByText("clear");
  it("never shows a stored secret: the box is empty, says it is saved, and has a clear button", () => {
    setup({ api_key: str({ format: "password" }) }, { api_key: "********" });
    const input = keyInput();
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "new-password");
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "Saved — type to replace");
    expect(clearButton()).toBeInTheDocument();
  });

  it("replaces the stored secret by what is typed (and then has no clear button)", async () => {
    const { rig, user } = setup({ api_key: str({ format: "password" }) }, { api_key: "********" });
    await user.type(keyInput(), "n");
    expect(rig.value).toEqual({ api_key: "n" });
    expect(keyInput()).toHaveValue("n");
    expect(screen.queryByText("clear")).not.toBeInTheDocument();
    await user.type(keyInput(), "ew");
    expect(rig.value).toEqual({ api_key: "new" });
  });

  it("clears the stored secret to an empty one, which then says it is not set", async () => {
    const { rig, user } = setup({ api_key: str({ format: "password" }) }, { api_key: "********" });
    await user.click(clearButton());
    expect(rig.value).toEqual({ api_key: "" });
    expect(keyInput()).toHaveAttribute("placeholder", "Not set");
    expect(screen.queryByText("clear")).not.toBeInTheDocument();
  });

  it("says not set for an empty secret and shows no mask", () => {
    setup({ token: str({ format: "password" }) }, { token: "" });
    expect(screen.getByLabelText(/^token/, { selector: "input" })).toHaveAttribute("placeholder", "Not set");
    expect(screen.queryByText("clear")).not.toBeInTheDocument();
  });
});

describe("duration", () => {
  const pattern = "^\\s*(\\d+(\\.\\d+)?\\s*(ms|s|m|h|d|w)\\s*)+$|^\\s*\\d+(\\.\\d+)?\\s*$";
  const schema = str({ format: "duration", pattern });

  it("is a text box with the units as its placeholder; a good value is not marked", () => {
    setup({ timeout: schema }, { timeout: "3m" });
    const input = byLabel("timeout");
    expect(input).toHaveValue("3m");
    expect(input).toHaveAttribute("placeholder", "e.g. 30s, 5m, 6h, 30d");
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("marks a value that is no duration while it is typed, and stops when it is one", async () => {
    const { rig, user } = setup({ timeout: schema }, { timeout: "3" });
    const input = byLabel("timeout");
    expect(input).not.toHaveAttribute("aria-invalid"); // a bare number is a count of seconds
    await user.type(input, "x");
    expect(rig.value).toEqual({ timeout: "3x" });
    expect(input).toHaveAttribute("aria-invalid", "true");
    await user.type(input, "{Backspace}m");
    expect(rig.value).toEqual({ timeout: "3m" });
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("marks an empty value (a duration is needed)", () => {
    setup({ timeout: schema }, { timeout: "" });
    expect(byLabel("timeout")).toHaveAttribute("aria-invalid", "true");
  });

  it("accepts anything when the schema has no pattern", () => {
    setup({ timeout: str({ format: "duration" }) }, { timeout: "whenever" });
    expect(byLabel("timeout")).not.toHaveAttribute("aria-invalid");
  });
});

describe("multi-line text", () => {
  it("is a text area of ten lines that gives what is typed", async () => {
    const { rig, user } = setup({ instructions: str({ format: "multiline" }) }, { instructions: "Be brief." });
    const area = byLabel("instructions");
    expect(area.tagName).toBe("TEXTAREA");
    expect(area).toHaveAttribute("rows", "10");
    expect(area).toHaveValue("Be brief.");
    await user.type(area, "{Enter}Be kind.");
    expect(rig.value).toEqual({ instructions: "Be brief.\nBe kind." });
  });
});

describe("list of texts and numbers", () => {
  it("shows the entries comma-separated and gives them when the box is left, not before", async () => {
    const { rig, user } = setup({ tags: { type: "array", items: { type: "string" } } }, { tags: ["a", "b"] });
    const input = byLabel("tags");
    expect(input).toHaveValue("a, b");
    expect(input).toHaveAttribute("placeholder", "Comma-separated");
    await user.clear(input);
    await user.type(input, "x, y  z,,w");
    expect(rig.emitted).toHaveLength(0);
    await user.tab();
    expect(rig.value).toEqual({ tags: ["x", "y", "z", "w"] });
  });

  it("gives numbers for a list of numbers and leaves out what is not one", async () => {
    const { rig, user } = setup({ ids: { type: "array", items: { type: "integer" } } }, { ids: [1001, 1002] });
    const input = byLabel("ids");
    expect(input).toHaveValue("1001, 1002");
    await user.clear(input);
    await user.type(input, "5, nope, 7.5");
    await user.tab();
    expect(rig.value).toEqual({ ids: [5, 7.5] });
  });

  it("gives an empty list for an empty box", async () => {
    const { rig, user } = setup({ tags: { type: "array", items: { type: "string" } } }, { tags: ["a"] });
    await user.clear(byLabel("tags"));
    await user.tab();
    expect(rig.value).toEqual({ tags: [] });
  });

  it("shows a new list when the value changes from outside, and keeps a half-typed one when it does not", async () => {
    const schema: JsonSchema = { type: "object", properties: { tags: { type: "array", items: { type: "string" } } } };
    const ctx: FormContext = { defs: {}, errors: {}, suggestions: {} };
    const user = userEvent.setup();
    const { rerender } = render(<ObjectFields schema={schema} value={{ tags: ["a"] }} onChange={() => undefined} path="" ctx={ctx} />);
    const input = byLabel("tags");
    await user.type(input, ", half");
    rerender(<ObjectFields schema={schema} value={{ tags: ["a"] }} onChange={() => undefined} path="" ctx={ctx} />);
    expect(input).toHaveValue("a, half");
    rerender(<ObjectFields schema={schema} value={{ tags: ["x", "y"] }} onChange={() => undefined} path="" ctx={ctx} />);
    expect(input).toHaveValue("x, y");
  });
});

describe("names with a text each", () => {
  const schema: JsonSchema = { type: "object", additionalProperties: { type: "string" } };

  it("shows a row of name and value for each entry", () => {
    setup({ query: schema }, { query: { limit: "10", sort: "asc" } });
    const group = screen.getByRole("group", { name: "query" });
    expect(within(group).getAllByPlaceholderText("name").map((i) => (i as HTMLInputElement).value)).toEqual(["limit", "sort"]);
    expect(within(group).getAllByPlaceholderText("value").map((i) => (i as HTMLInputElement).value)).toEqual(["10", "asc"]);
  });

  it("adds a row with a name of its own, and removes one", async () => {
    const { rig, user } = setup({ query: schema }, { query: { limit: "10" } });
    const group = within(screen.getByRole("group", { name: "query" }));
    await user.click(group.getByRole("button", { name: "add" }));
    expect(rig.value).toEqual({ query: { limit: "10", query_1: "" } });
    await user.click(group.getAllByRole("button", { name: "remove" })[0]);
    expect(rig.value).toEqual({ query: { query_1: "" } });
  });

  it("removes a row with an icon button (it fits beside the two inputs on a phone), still named remove", () => {
    setup({ query: schema }, { query: { limit: "10" } });
    const group = within(screen.getByRole("group", { name: "query" }));
    const remove = group.getByRole("button", { name: "remove" });
    expect(remove).toHaveClass("fd-btn-icon");
    expect(remove).toHaveTextContent("");
    expect(remove.closest(".fd-joined")).toContainElement(group.getByPlaceholderText("name"));
  });

  it("adds the first query_N that no row has: it never takes a name that is there", async () => {
    const { rig, user } = setup({ query: schema }, { query: { query_1: "a", query_3: "c" } });
    const group = within(screen.getByRole("group", { name: "query" }));
    await user.click(group.getByRole("button", { name: "add" }));
    expect(rig.value).toEqual({ query: { query_1: "a", query_3: "c", query_2: "" } });
    await user.click(group.getByRole("button", { name: "add" }));
    expect(rig.value).toEqual({ query: { query_1: "a", query_3: "c", query_2: "", query_4: "" } });
    const names = group.getAllByPlaceholderText("name") as HTMLInputElement[];
    expect(names.map((i) => i.getAttribute("aria-invalid"))).not.toContain("true");
  });

  it("starts at query_1 for an empty set", async () => {
    const { rig, user } = setup({ query: schema }, { query: {} });
    await user.click(screen.getByRole("button", { name: "add" }));
    expect(rig.value).toEqual({ query: { query_1: "" } });
  });

  it("renames an entry and changes its value", async () => {
    const { rig, user } = setup({ query: schema }, { query: { limit: "10" } });
    const group = within(screen.getByRole("group", { name: "query" }));
    await user.type(group.getByPlaceholderText("value"), "0");
    expect(rig.value).toEqual({ query: { limit: "100" } });
    await user.type(group.getByPlaceholderText("name"), "s");
    expect(rig.value).toEqual({ query: { limits: "100" } });
  });

  it("shows two rows with the same name as invalid, keeps both, and sends nothing with a clash", async () => {
    const { rig, user } = setup({ query: schema }, { query: { limit: "10", sort: "asc" } });
    const group = within(screen.getByRole("group", { name: "query" }));
    const names = () => group.getAllByPlaceholderText("name") as HTMLInputElement[];
    // rename "sort" to "limit": one letter at a time, the last one makes the clash
    await user.clear(names()[1]);
    await user.type(names()[1], "limit");
    expect(names().map((i) => i.value)).toEqual(["limit", "limit"]);
    expect(group.getAllByPlaceholderText("value").map((i) => (i as HTMLInputElement).value)).toEqual(["10", "asc"]);
    expect(names().map((i) => i.getAttribute("aria-invalid"))).toEqual(["true", "true"]);
    // each of the two rows says it under itself, with the element and style a field's error has
    const messages = group.getAllByText("Name already used");
    expect(messages).toHaveLength(2);
    for (const [i, m] of messages.entries()) {
      expect(m).toHaveClass("fd-field-error");
      expect(m.parentElement).toContainElement(names()[i]);
    }
    expect(rig.value).toEqual({ query: { limit: "10", limi: "asc" } }); // the last value without a clash
    // another name ends it: both rows go in, nothing was dropped
    await user.type(names()[1], "s");
    expect(names().map((i) => i.getAttribute("aria-invalid"))).not.toContain("true");
    expect(group.queryByText("Name already used")).not.toBeInTheDocument();
    expect(names().map((i) => i.value)).toEqual(["limit", "limits"]);
    expect(rig.value).toEqual({ query: { limit: "10", limits: "asc" } });
  });

  it("removing one of two rows with the same name ends the clash", async () => {
    const { rig, user } = setup({ query: schema }, { query: { a: "1", b: "2" } });
    const group = within(screen.getByRole("group", { name: "query" }));
    await user.clear(group.getAllByPlaceholderText("name")[1]);
    await user.type(group.getAllByPlaceholderText("name")[1], "a");
    await user.click(group.getAllByRole("button", { name: "remove" })[0]);
    expect(rig.value).toEqual({ query: { a: "2" } });
    expect(group.getAllByPlaceholderText("name")).toHaveLength(1);
  });
});

describe("other objects (JSON)", () => {
  const schema: JsonSchema = { type: "object", additionalProperties: true };

  it("shows the value as formatted JSON", () => {
    setup({ extra_body: schema }, { extra_body: { keep_alive: "5m" } });
    expect(byLabel("extra body")).toHaveValue('{\n  "keep_alive": "5m"\n}');
  });

  it("gives the parsed value of what is typed", async () => {
    const { rig, user } = setup({ extra_body: schema }, { extra_body: {} });
    const area = byLabel("extra body");
    await user.clear(area);
    await user.paste('{"num_ctx": 4096}');
    expect(rig.value).toEqual({ extra_body: { num_ctx: 4096 } });
    expect(screen.queryByText("Not valid JSON yet")).not.toBeInTheDocument();
  });

  it("says when it is not valid yet, gives nothing then, and takes the value when it becomes valid", async () => {
    const { rig, user } = setup({ extra_body: schema }, { extra_body: { a: 1 } });
    const area = byLabel("extra body");
    await user.click(area);
    await user.clear(area); // empty: an empty object
    const before = rig.emitted.length;
    await user.paste('{"a": ');
    expect(screen.getByText("Not valid JSON yet")).toBeInTheDocument();
    expect(area).toHaveAttribute("aria-invalid", "true");
    expect(rig.emitted).toHaveLength(before);
    await user.paste("2}");
    expect(screen.queryByText("Not valid JSON yet")).not.toBeInTheDocument();
    expect(area).not.toHaveAttribute("aria-invalid");
    expect(rig.value).toEqual({ extra_body: { a: 2 } });
  });

  it("takes an empty box as an empty object", async () => {
    const { rig, user } = setup({ extra_body: schema }, { extra_body: { a: 1 } });
    await user.clear(byLabel("extra body"));
    expect(rig.value).toEqual({ extra_body: {} });
  });
});

describe("group", () => {
  const proxy: JsonSchema = {
    type: "object",
    description: "Where to connect.",
    properties: { host: str(), port: { type: "integer" } },
  };

  it("is a panel with its title, its description and its fields, and gives the whole object back", async () => {
    const { rig, user } = setup({ proxy_settings: proxy }, { proxy_settings: { host: "a.test", port: 80 } });
    expect(screen.getByRole("heading", { level: 2, name: "Proxy settings" })).toBeInTheDocument();
    expect(screen.getByText("Where to connect.")).toBeInTheDocument();
    expect(byLabel("host")).toHaveValue("a.test");
    await user.type(byLabel("host"), "x");
    expect(rig.value).toEqual({ proxy_settings: { host: "a.testx", port: 80 } });
  });

  it("shows the error of the group, and of one of its fields at that field", () => {
    setup({ proxy_settings: proxy }, { proxy_settings: { host: "", port: 1 } }, { errors: { proxy_settings: "Group is wrong", "proxy_settings.host": "Host is wrong" } });
    expect(screen.getByRole("alert")).toHaveTextContent("Group is wrong");
    expect(screen.getByText("Host is wrong")).toBeInTheDocument();
    expect(byLabel("host")).toHaveAttribute("aria-invalid", "true");
    expect(byLabel("port")).not.toHaveAttribute("aria-invalid");
  });

  it("resolves a reference to a definition", () => {
    setup({ proxy_settings: { $ref: "#/$defs/Proxy" } }, { proxy_settings: { host: "a.test", port: 80 } }, { defs: { Proxy: proxy } });
    expect(screen.getByRole("heading", { level: 2, name: "Proxy settings" })).toBeInTheDocument();
    expect(byLabel("host")).toHaveValue("a.test");
  });
});

describe("optional group", () => {
  const proxy: JsonSchema = {
    type: "object",
    properties: { host: str({ default: "localhost" }), port: { type: "integer", default: 8080 } },
  };
  const schema: JsonSchema = { anyOf: [proxy, { type: "null" }], description: "An optional proxy." };

  it("says it is not configured, with a button to configure it", () => {
    setup({ proxy: schema }, { proxy: null });
    expect(screen.getByRole("heading", { level: 2, name: "Proxy" })).toBeInTheDocument();
    expect(screen.getByText("An optional proxy.")).toBeInTheDocument();
    expect(screen.getByText("Not configured.")).toBeInTheDocument();
    expect(screen.queryByLabelText(/^host/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "remove" })).not.toBeInTheDocument();
  });

  it("configures it with the defaults of its fields, then removes it again", async () => {
    const { rig, user } = setup({ proxy: schema }, { proxy: null });
    await user.click(screen.getByRole("button", { name: "configure" }));
    expect(rig.value).toEqual({ proxy: { host: "localhost", port: 8080 } });
    expect(byLabel("host")).toHaveValue("localhost");
    expect(screen.queryByText("Not configured.")).not.toBeInTheDocument();
    await user.type(byLabel("host"), "2");
    expect(rig.value).toEqual({ proxy: { host: "localhost2", port: 8080 } });
    await user.click(screen.getByRole("button", { name: "remove" }));
    expect(rig.value).toEqual({ proxy: null });
    expect(screen.getByText("Not configured.")).toBeInTheDocument();
  });

  it("shows its error", () => {
    setup({ proxy: schema }, { proxy: null }, { errors: { proxy: "Proxy is wrong" } });
    expect(screen.getByRole("alert")).toHaveTextContent("Proxy is wrong");
  });
});

describe("list of objects", () => {
  const item: JsonSchema = {
    type: "object",
    properties: { name: str(), enabled: { type: "boolean", default: true }, url: str() },
  };
  const schema: JsonSchema = { type: "array", items: item, description: "The servers." };
  const alpha = { name: "alpha", enabled: true, url: "http://a.test" };
  const bravo = { name: "bravo", enabled: false, url: "http://b.test" };

  const scrollIntoView = vi.fn();
  beforeEach(() => {
    scrollIntoView.mockClear();
    Object.defineProperty(Element.prototype, "scrollIntoView", { value: scrollIntoView, configurable: true, writable: true });
  });
  afterEach(() => {
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  });

  const toggle = (title: string) => screen.getByRole("button", { name: title });

  it("has its title, its note and an add button, and says None when it is empty", () => {
    setup({ servers: schema }, { servers: [] });
    expect(screen.getByText("Servers")).toBeInTheDocument();
    expect(screen.getByText("The servers.")).toBeInTheDocument();
    expect(screen.getByText("None.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "add" })).toBeInTheDocument();
  });

  it("shows a card per entry: its name, on or off, and its address, closed", () => {
    setup({ servers: schema }, { servers: [alpha, bravo] });
    expect(screen.queryByText("None.")).not.toBeInTheDocument();
    expect(toggle("alpha")).toHaveAttribute("aria-expanded", "false");
    expect(toggle("bravo")).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("on")).toBeInTheDocument();
    expect(screen.getByText("off")).toBeInTheDocument();
    expect(screen.getByText("http://a.test")).toBeInTheDocument();
    expect(screen.getByText("http://b.test")).toBeInTheDocument();
  });

  it("names an entry without a name by its place, and shows no status when it has no switch", () => {
    setup({ servers: { type: "array", items: { type: "object", properties: { label: str() } } } }, { servers: [{ label: "x" }, { label: "y" }] });
    expect(toggle("#1")).toBeInTheDocument();
    expect(toggle("#2")).toBeInTheDocument();
    expect(screen.queryByText("on")).not.toBeInTheDocument();
    expect(screen.queryByText("off")).not.toBeInTheDocument();
  });

  it("opens and closes a card", async () => {
    const { user } = setup({ servers: schema }, { servers: [alpha] });
    await user.click(toggle("alpha"));
    expect(toggle("alpha")).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle("alpha"));
    expect(toggle("alpha")).toHaveAttribute("aria-expanded", "false");
  });

  it("gives the whole list when a field of an entry changes", async () => {
    const { rig, user } = setup({ servers: schema }, { servers: [alpha, bravo] });
    await user.click(toggle("bravo"));
    const urls = screen.getAllByLabelText(/^url/);
    await user.type(urls[1], "/sse");
    expect(rig.value).toEqual({ servers: [alpha, { ...bravo, url: "http://b.test/sse" }] });
  });

  it("adds an entry made of the defaults, and opens its card", async () => {
    const { rig, user } = setup({ servers: schema }, { servers: [alpha] });
    await user.click(screen.getByRole("button", { name: "add" }));
    expect(rig.value).toEqual({ servers: [alpha, { name: "", enabled: true, url: "" }] });
    expect(toggle("alpha")).toHaveAttribute("aria-expanded", "false");
    expect(toggle("#2")).toHaveAttribute("aria-expanded", "true");
  });

  it("removes an entry", async () => {
    const { rig, user } = setup({ servers: schema }, { servers: [alpha, bravo] });
    await user.click(screen.getAllByRole("button", { name: "remove" })[0]);
    expect(rig.value).toEqual({ servers: [bravo] });
    expect(screen.queryByRole("button", { name: "alpha" })).not.toBeInTheDocument();
  });

  it("keeps the other cards as they were when one is removed (an entry's card state goes with the entry)", async () => {
    const { user } = setup({ servers: schema }, { servers: [alpha, bravo] });
    await user.click(toggle("bravo"));
    await user.click(screen.getAllByRole("button", { name: "remove" })[0]);
    expect(toggle("bravo")).toHaveAttribute("aria-expanded", "true");
  });

  it("opens the card of an entry that has an error, and shows the error at its field", () => {
    setup({ servers: schema }, { servers: [alpha, bravo] }, { errors: { "servers.1.url": "Address is wrong" } });
    expect(toggle("alpha")).toHaveAttribute("aria-expanded", "false");
    expect(toggle("bravo")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Address is wrong")).toBeInTheDocument();
  });

  it("opens the card of the entry a link points at and scrolls it into view", () => {
    setup({ servers: schema }, { servers: [alpha, bravo] }, { focusEntry: "bravo" });
    expect(toggle("bravo")).toHaveAttribute("aria-expanded", "true");
    expect(toggle("alpha")).toHaveAttribute("aria-expanded", "false");
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("scrolls nothing when no entry has the name the link points at", () => {
    setup({ servers: schema }, { servers: [alpha] }, { focusEntry: "nothing" });
    expect(toggle("alpha")).toHaveAttribute("aria-expanded", "false");
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("shows the error of the list itself", () => {
    setup({ servers: schema }, { servers: [] }, { errors: { servers: "At least one is needed" } });
    expect(screen.getByRole("alert")).toHaveTextContent("At least one is needed");
  });

  it("inside a group is a panel of its own with the same buttons", async () => {
    const outer: JsonSchema = { type: "object", properties: { servers: schema } };
    const { rig, user } = setup({ outer }, { outer: { servers: [] } }, { errors: { "outer.servers": "Inside is wrong" } });
    expect(screen.getByRole("heading", { level: 2, name: "Servers" })).toBeInTheDocument();
    expect(screen.getByText("None.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Inside is wrong");
    await user.click(screen.getByRole("button", { name: "add" }));
    expect(rig.value).toEqual({ outer: { servers: [{ name: "", enabled: true, url: "" }] } });
  });

  it("is empty for a value that is not there", () => {
    setup({ servers: schema }, {});
    expect(screen.getByText("None.")).toBeInTheDocument();
  });
});

describe("rows of a group", () => {
  const properties: Record<string, JsonSchema> = { alpha: str(), bravo: str(), charlie: str(), delta: str() };
  const values = { alpha: "1", bravo: "2", charlie: "3", delta: "4" };

  it("puts the named fields into named sections, in the order of the group, and the others after them", () => {
    setup(properties, values, {
      groups: {
        "": [
          { title: "Second", fields: ["charlie", "alpha"], notes: ["Charlie and alpha belong together."] },
          { title: "First", fields: ["bravo"], wide: ["bravo"] },
          { title: "Nothing here", fields: ["missing"] },
        ],
      },
    });
    const second = screen.getByRole("region", { name: "Second" });
    const first = screen.getByRole("region", { name: "First" });
    expect(within(second).getByLabelText(/^charlie/)).toBeInTheDocument();
    expect(within(second).getByLabelText(/^alpha/)).toBeInTheDocument();
    expect(within(second).getByText("Charlie and alpha belong together.")).toBeInTheDocument();
    expect(within(first).getByLabelText(/^bravo/)).toBeInTheDocument();
    // a section without a field that exists is left out
    expect(screen.queryByRole("region", { name: "Nothing here" })).not.toBeInTheDocument();
    // the field no group names is still there, after the sections
    const delta = byLabel("delta");
    expect(within(second).queryByLabelText(/^delta/)).not.toBeInTheDocument();
    expect(second.compareDocumentPosition(delta) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(second.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("leaves out the fields whose path is hidden", () => {
    setup(properties, values, { hidden: ["bravo"], groups: { "": [{ title: "Pair", fields: ["alpha", "bravo"] }] } });
    expect(screen.queryByLabelText(/^bravo/)).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Pair" })).getByLabelText(/^alpha/)).toBeInTheDocument();
  });

  it("shows a note above the field of its path", () => {
    setup(properties, values, { notes: { charlie: "Charlie is special." } });
    const note = screen.getByText("Charlie is special.");
    expect(note.compareDocumentPosition(byLabel("charlie")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(note.compareDocumentPosition(byLabel("bravo")) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  });

  it("shows a note above a group of its path", () => {
    setup({ proxy: { type: "object", properties: { host: str() } } }, { proxy: { host: "" } }, { notes: { proxy: "About the proxy." } });
    expect(screen.getByText("About the proxy.")).toBeInTheDocument();
  });

  it("shows the note of a field that stands in a named section too", () => {
    // (Settings sets no note of a field today)
    setup(properties, values, { notes: { alpha: "Alpha is special." }, groups: { "": [{ title: "Pair", fields: ["alpha", "bravo"] }] } });
    const note = screen.getByText("Alpha is special.");
    expect(within(screen.getByRole("region", { name: "Pair" })).getByText("Alpha is special.")).toBe(note);
    expect(note.compareDocumentPosition(byLabel("alpha")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("applies the sections of a nested object by its path", () => {
    setup({ model: { type: "object", properties: { alpha: str(), bravo: str() } } }, { model: { alpha: "", bravo: "" } }, {
      groups: { model: [{ title: "Connection", fields: ["alpha"] }] },
    });
    expect(within(screen.getByRole("region", { name: "Connection" })).getByLabelText(/^alpha/)).toBeInTheDocument();
    expect(byLabel("bravo")).toBeInTheDocument();
  });
});

describe("connection test of a group", () => {
  const conn: JsonSchema = {
    type: "object",
    properties: { api_url: str(), enabled: { type: "boolean" } },
    test: "telegram",
    test_modes: { send: "Send test message" },
  } as JsonSchema;
  const value = { conn: { api_url: "http://bot.test", enabled: true } };

  type Seen = { path: string; body: unknown };

  /** The test endpoint answers `reply` (a function to wait on something first) and records what it was sent. */
  function answerTests(reply: () => Response | Promise<Response>) {
    const seen: Seen[] = [];
    server.use(
      http.post("/api/settings/test/:kind", async ({ request }) => {
        const url = new URL(request.url);
        seen.push({ path: url.pathname + url.search, body: await request.json() });
        return reply();
      }),
    );
    return seen;
  }

  it("has a test button and a button for each extra mode, in the panel's head", () => {
    setup({ conn }, value);
    expect(screen.getByRole("button", { name: "test" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "send test message" })).toBeInTheDocument();
  });

  it("sends the values in the form and shows ok with the server's message", async () => {
    const seen = answerTests(() => HttpResponse.json({ ok: true, message: "Bot is reachable.", details: {} }));
    const { user } = setup({ conn }, value);
    await user.click(screen.getByRole("button", { name: "test" }));
    expect(await screen.findByText("Bot is reachable.")).toBeInTheDocument();
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(seen).toEqual([{ path: "/api/settings/test/telegram", body: value.conn }]);
  });

  it("runs a mode by its name in the query", async () => {
    const seen = answerTests(() => HttpResponse.json({ ok: true, message: "Message sent.", details: {} }));
    const { user } = setup({ conn }, value);
    await user.click(screen.getByRole("button", { name: "send test message" }));
    expect(await screen.findByText("Message sent.")).toBeInTheDocument();
    expect(seen).toEqual([{ path: "/api/settings/test/telegram?mode=send", body: value.conn }]);
  });

  it("shows failed with the message when the test fails", async () => {
    answerTests(() => HttpResponse.json({ ok: false, message: "Unauthorized.", details: {} }));
    const { user } = setup({ conn }, value);
    await user.click(screen.getByRole("button", { name: "test" }));
    expect(await screen.findByText("Unauthorized.")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
  });

  it("says the test could not run when the request fails", async () => {
    answerTests(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    const { user } = setup({ conn }, value);
    await user.click(screen.getByRole("button", { name: "test" }));
    expect(await screen.findByText("The test could not run: boom")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
  });

  it("shows testing while it runs, and no second test can start", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const seen = answerTests(async () => {
      await gate;
      return HttpResponse.json({ ok: true, message: "Done.", details: {} });
    });
    const { user } = setup({ conn }, value);
    await user.click(screen.getByRole("button", { name: "test" }));
    const running = await screen.findByRole("button", { name: "testing…" });
    expect(running).toHaveAttribute("aria-disabled", "true");
    expect(screen.getAllByText("testing…").length).toBeGreaterThan(0);
    await user.click(running);
    await user.click(screen.getByRole("button", { name: "send test message" }));
    release();
    expect(await screen.findByText("Done.")).toBeInTheDocument();
    expect(seen).toHaveLength(1);
    expect(screen.getByRole("button", { name: "test" })).not.toHaveAttribute("aria-disabled");
  });

  it("takes the result away when the values it was run with are edited", async () => {
    answerTests(() => HttpResponse.json({ ok: true, message: "Bot is reachable.", details: {} }));
    const { user } = setup({ conn }, value);
    await user.click(screen.getByRole("button", { name: "test" }));
    await screen.findByText("Bot is reachable.");
    await user.type(byLabel("api url"), "/x");
    expect(screen.queryByText("Bot is reachable.")).not.toBeInTheDocument();
  });

  it("drops the answer of a test that was edited while it ran", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const seen = answerTests(async () => {
      await gate;
      return HttpResponse.json({ ok: true, message: "Late answer.", details: {} });
    });
    const { user } = setup({ conn }, value);
    await user.click(screen.getByRole("button", { name: "test" }));
    await waitFor(() => expect(seen).toHaveLength(1));
    await user.type(byLabel("api url"), "/x");
    expect(screen.queryAllByText("testing…").filter((e) => e.closest("[role=status]"))).toHaveLength(0);
    release();
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText("Late answer.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "test" })).not.toHaveAttribute("aria-disabled");
  });

  it("has no test button on a group that has no test", () => {
    setup({ plain: { type: "object", properties: { api_url: str() } } }, { plain: { api_url: "" } });
    expect(screen.queryByRole("button", { name: "test" })).not.toBeInTheDocument();
  });

  it("tests an optional group only while it is configured", async () => {
    const optional: JsonSchema = { anyOf: [conn, { type: "null" }], test: "telegram" };
    const seen = answerTests(() => HttpResponse.json({ ok: true, message: "Reachable.", details: {} }));
    const { user } = setup({ conn: optional }, { conn: null });
    expect(screen.queryByRole("button", { name: "test" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "configure" }));
    await user.click(screen.getByRole("button", { name: "test" }));
    expect(await screen.findByText("Reachable.")).toBeInTheDocument();
    expect(seen[0].path).toBe("/api/settings/test/telegram");
  });
});

describe("connection test of a list entry (MCP servers)", () => {
  const server1 = { name: "alpha", url: "http://a.test", tool_filter: [] as string[], tool_overrides: [] as JsonObject[] };
  const server2 = { name: "bravo", url: "http://b.test", tool_filter: [] as string[], tool_overrides: [] as JsonObject[] };
  const list: JsonSchema = {
    type: "array",
    items: {
      type: "object",
      properties: {
        name: str(),
        url: str(),
        tool_filter: { type: "array", items: { type: "string" } },
        tool_overrides: { type: "array", items: { type: "object", properties: { name: str(), policy: str(), kind: str() } } },
      },
    },
    test: "mcp",
  } as JsonSchema;
  const tools = [
    { name: "list_things", title: null, description: "List the things.", read_only: true },
    { name: "change_thing", title: null, description: "Change one thing.", read_only: false },
  ];

  function answerTests(reply: Record<string, unknown>) {
    const seen: { path: string; body: unknown }[] = [];
    server.use(
      http.post("/api/settings/test/:kind", async ({ request }) => {
        const url = new URL(request.url);
        seen.push({ path: url.pathname + url.search, body: await request.json() });
        return HttpResponse.json(reply);
      }),
    );
    return seen;
  }

  it("tests the entry it belongs to, by its place in the list", async () => {
    const seen = answerTests({ ok: true, message: "Connected: 2 tools.", details: { tools } });
    const { user } = setup({ servers: list }, { servers: [server1, server2] });
    await user.click(screen.getAllByRole("button", { name: "test" })[1]);
    expect(await screen.findByText("Connected: 2 tools.")).toBeInTheDocument();
    expect(seen).toEqual([{ path: "/api/settings/test/mcp?index=1", body: server2 }]);
    // the result stands under that card only
    expect(screen.getAllByText("ok")).toHaveLength(1);
  });

  it("lists the tools the server offers, and a failed test lists none", async () => {
    answerTests({ ok: false, message: "Connection refused.", details: { tools } });
    const { user } = setup({ servers: list }, { servers: [server1] });
    await user.click(screen.getByRole("button", { name: "test" }));
    expect(await screen.findByText("Connection refused.")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /^Use / })).not.toBeInTheDocument();
  });

  it("chooses tools in the picker, and the choice does not take the result away", async () => {
    answerTests({ ok: true, message: "Connected: 2 tools.", details: { tools } });
    const { rig, user } = setup({ servers: list }, { servers: [server1] });
    await user.click(screen.getByRole("button", { name: "test" }));
    await screen.findByText("Connected: 2 tools.");
    expect(screen.getByText("0 of 2 tools on")).toBeInTheDocument();

    await user.click(screen.getByRole("switch", { name: "Use list_things" }));
    expect(rig.value).toEqual({ servers: [{ ...server1, tool_filter: ["list_things"] }] });
    expect(screen.getByText("Connected: 2 tools.")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 tools on")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Select all tools of this server" }));
    expect(rig.value).toEqual({ servers: [{ ...server1, tool_filter: ["list_things", "change_thing"] }] });
    expect(screen.getByText("Connected: 2 tools.")).toBeInTheDocument();
  });

  it("sets what a tool is treated as, and keeps the result", async () => {
    answerTests({ ok: true, message: "Connected: 2 tools.", details: { tools } });
    const { rig, user } = setup({ servers: list }, { servers: [{ ...server1, tool_filter: ["change_thing"] }] });
    await user.click(screen.getByRole("button", { name: "test" }));
    await screen.findByText("Connected: 2 tools.");
    await user.selectOptions(screen.getByRole("combobox", { name: "Policy of change_thing" }), "auto");
    expect(rig.value).toEqual({
      servers: [{ ...server1, tool_filter: ["change_thing"], tool_overrides: [{ name: "change_thing", policy: "auto", kind: "auto" }] }],
    });
    expect(screen.getByText("Connected: 2 tools.")).toBeInTheDocument();
  });

  it("takes the result and the picker away when another field of the entry changes", async () => {
    answerTests({ ok: true, message: "Connected: 2 tools.", details: { tools } });
    const { user } = setup({ servers: list }, { servers: [server1] });
    await user.click(screen.getByRole("button", { name: "test" }));
    await screen.findByText("Connected: 2 tools.");
    await user.type(screen.getByLabelText(/^url/), "/mcp");
    expect(screen.queryByText("Connected: 2 tools.")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Use list_things" })).not.toBeInTheDocument();
  });
});
