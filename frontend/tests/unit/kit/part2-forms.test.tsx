import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  Button,
  Checkbox,
  CheckboxBlank,
  Chip,
  Field,
  FieldError,
  FieldValue,
  FormGrid,
  FormGroup,
  FormGroups,
  FormWide,
  Input,
  Joined,
  ListPicker,
  List,
  ListItem,
  SaveBar,
  Segmented,
  Select,
  Switch,
  Tabs,
  Textarea,
  Toolbar,
} from "../../../src/kit";

describe("Switch", () => {
  it("is a switch named by label, with its state in aria-checked", () => {
    render(<Switch checked={false} onChange={() => {}} label="enabled" />);
    const sw = screen.getByRole("switch", { name: "enabled" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    expect(sw).toHaveAttribute("type", "button");
  });

  it("calls onChange with the opposite of the current value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = render(<Switch checked={false} onChange={onChange} label="enabled" />);
    await user.click(screen.getByRole("switch", { name: "enabled" }));
    expect(onChange).toHaveBeenLastCalledWith(true);
    rerender(<Switch checked={true} onChange={onChange} label="enabled" />);
    expect(screen.getByRole("switch", { name: "enabled" })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("switch", { name: "enabled" }));
    expect(onChange).toHaveBeenLastCalledWith(false);
  });

  it("is operated by the keyboard (Space and Enter)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Switch checked={false} onChange={onChange} label="enabled" />);
    await user.tab();
    expect(screen.getByRole("switch")).toHaveFocus();
    await user.keyboard(" ");
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it("disabled does not call onChange", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Switch checked={false} onChange={onChange} label="enabled" disabled />);
    expect(screen.getByRole("switch", { name: "enabled" })).toBeDisabled();
    await user.click(screen.getByRole("switch", { name: "enabled" }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("shows text beside the switch and keeps the label as its name", () => {
    render(<Switch checked label="enabled" text="Run on schedule" onChange={() => {}} title="Turn the schedule on or off" />);
    expect(screen.getByText("Run on schedule")).toBeInTheDocument();
    const sw = screen.getByRole("switch", { name: "enabled" });
    expect(sw).toHaveAttribute("title", "Turn the schedule on or off");
  });

  it("a controlled switch follows the state of its parent", async () => {
    const user = userEvent.setup();
    function Parent() {
      const [on, setOn] = useState(false);
      return <Switch checked={on} onChange={setOn} label="enabled" />;
    }
    render(<Parent />);
    const sw = screen.getByRole("switch", { name: "enabled" });
    await user.click(sw);
    expect(sw).toHaveAttribute("aria-checked", "true");
    await user.click(sw);
    expect(sw).toHaveAttribute("aria-checked", "false");
  });
});

describe("Checkbox", () => {
  it("is a checkbox named by its label and calls onChange with the new value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Checkbox checked={false} onChange={onChange} label="select all" />);
    const box = screen.getByRole("checkbox", { name: "select all" });
    expect(box).not.toBeChecked();
    await user.click(box);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("a click on the label ticks it too", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Checkbox checked={true} onChange={onChange} label="remember" />);
    await user.click(screen.getByText("remember"));
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("some marks it as partly selected, but not when it is checked", () => {
    const { rerender } = render(<Checkbox checked={false} some onChange={() => {}} label="rows" />);
    expect(screen.getByRole("checkbox", { name: "rows" })).toBePartiallyChecked();
    rerender(<Checkbox checked={true} some onChange={() => {}} label="rows" />);
    expect(screen.getByRole("checkbox", { name: "rows" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "rows" })).not.toBePartiallyChecked();
    rerender(<Checkbox checked={false} onChange={() => {}} label="rows" />);
    expect(screen.getByRole("checkbox", { name: "rows" })).not.toBePartiallyChecked();
  });

  it("hideLabel keeps the label for assistive tech only", () => {
    render(<Checkbox checked={false} onChange={() => {}} label="select row 3" hideLabel />);
    expect(screen.getByRole("checkbox", { name: "select row 3" })).toBeInTheDocument();
    expect(screen.getByText("select row 3")).toHaveClass("fd-sr");
  });

  it("disabled does not call onChange", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Checkbox checked={false} onChange={onChange} label="locked" disabled />);
    await user.click(screen.getByRole("checkbox", { name: "locked" }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("CheckboxBlank is hidden from assistive tech", () => {
    const { container } = render(<CheckboxBlank />);
    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});

describe("Field", () => {
  it("names the input inside it by its label", () => {
    render(
      <Field label="name">
        <Input defaultValue="test-main" />
      </Field>,
    );
    expect(screen.getByLabelText("name")).toHaveValue("test-main");
  });

  it("shows help and an error line", () => {
    render(
      <Field label="port" help="Between 1 and 65535" error="Not a number">
        <Input defaultValue="x" invalid />
      </Field>,
    );
    expect(screen.getByText("Between 1 and 65535")).toHaveClass("fd-field-help");
    expect(screen.getByText("Not a number")).toHaveClass("fd-field-error");
  });

  it("FieldError is the error line a field shows, also for a row that is not a Field", () => {
    render(<FieldError>Name already used</FieldError>);
    expect(screen.getByText("Name already used")).toHaveClass("fd-field-error");
  });

  it("shows no help or error element when none is given", () => {
    const { container } = render(
      <Field label="port">
        <Input />
      </Field>,
    );
    expect(container.querySelector(".fd-field-help")).toBeNull();
    expect(container.querySelector(".fd-field-error")).toBeNull();
  });

  it("group makes it a labelled group instead of a label", () => {
    render(
      <Field label="mode" group>
        <Segmented label="mode" value="a" onChange={() => {}} options={[{ value: "a", label: "a" }]} />
      </Field>,
    );
    expect(screen.getAllByRole("group", { name: "mode" })).toHaveLength(1);
  });

  it("htmlFor points the label at a control that is not the first input", () => {
    const { container } = render(
      <Field label="second" htmlFor="the-second">
        <span>
          <Input aria-label="unrelated" />
          <Input id="the-second" />
        </span>
      </Field>,
    );
    expect(container.querySelector("label")).toHaveAttribute("for", "the-second");
  });

  it("takes placement (width, span) as style", () => {
    const { container } = render(
      <Field label="wide" span="all" width="200px">
        <Input />
      </Field>,
    );
    const field = container.querySelector(".fd-field") as HTMLElement;
    expect(field.style.gridColumn).toBe("1 / -1");
    expect(field.style.width).toBe("200px");
  });
});

describe("Input, Textarea and Select", () => {
  it("a controlled Input calls onChange with what was typed", async () => {
    const user = userEvent.setup();
    function Parent() {
      const [value, setValue] = useState("");
      return (
        <>
          <Field label="name">
            <Input value={value} onChange={(e) => setValue(e.target.value)} />
          </Field>
          <output>{value}</output>
        </>
      );
    }
    render(<Parent />);
    await user.type(screen.getByLabelText("name"), "host-a");
    expect(screen.getByLabelText("name")).toHaveValue("host-a");
    expect(screen.getByRole("status")).toHaveTextContent("host-a");
  });

  it("invalid sets aria-invalid; valid has none", () => {
    render(
      <>
        <Input aria-label="bad" invalid />
        <Input aria-label="good" />
        <Textarea aria-label="bad text" invalid />
        <Select aria-label="bad select" invalid>
          <option>a</option>
        </Select>
        <Select aria-label="good select">
          <option>a</option>
        </Select>
      </>,
    );
    expect(screen.getByLabelText("bad")).toBeInvalid();
    expect(screen.getByLabelText("good")).not.toHaveAttribute("aria-invalid");
    expect(screen.getByLabelText("bad text")).toBeInvalid();
    expect(screen.getByLabelText("bad select")).toBeInvalid();
    expect(screen.getByLabelText("good select")).not.toHaveAttribute("aria-invalid");
  });

  it("Input passes type, placeholder, disabled and required through", () => {
    render(<Input aria-label="secret" type="password" placeholder="enter secret" disabled required />);
    const input = screen.getByLabelText("secret");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("placeholder", "enter secret");
    expect(input).toBeDisabled();
    expect(input).toBeRequired();
  });

  it("Input keeps room for its placeholder and takes flex and width", () => {
    render(<Input aria-label="named" placeholder="12345" flex="1" width="10rem" />);
    const input = screen.getByLabelText("named");
    expect(input.style.getPropertyValue("--fd-placeholder")).toBe("5ch");
    expect(input.style.flex).toContain("1");
    expect(input.style.width).toBe("10rem");
  });

  it("an Input without placeholder sets no placeholder room", () => {
    render(<Input aria-label="plain" />);
    expect(screen.getByLabelText("plain").style.getPropertyValue("--fd-placeholder")).toBe("");
  });

  it("a controlled Textarea calls onChange with its text, line breaks kept", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Textarea aria-label="notes" defaultValue="" onChange={(e) => onChange(e.target.value)} />);
    await user.type(screen.getByLabelText("notes"), "one{Enter}two");
    expect(onChange).toHaveBeenLastCalledWith("one\ntwo");
  });

  it("a Select shows its options and calls onChange with the chosen value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Select aria-label="model" defaultValue="a" onChange={(e) => onChange(e.target.value)}>
        <option value="a">Model A</option>
        <option value="b">Model B</option>
      </Select>,
    );
    expect(screen.getByRole("combobox", { name: "model" })).toHaveValue("a");
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Model A", "Model B"]);
    await user.selectOptions(screen.getByRole("combobox", { name: "model" }), "b");
    expect(onChange).toHaveBeenCalledWith("b");
    expect(screen.getByRole("combobox", { name: "model" })).toHaveValue("b");
  });
});

describe("Joined, FieldValue, Chip", () => {
  it("Joined keeps an input and its button together", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Joined>
        <Input aria-label="secret" />
        <Button onClick={onClick}>clear</Button>
      </Joined>,
    );
    const group = screen.getByLabelText("secret").parentElement as HTMLElement;
    expect(group).toHaveClass("fd-joined");
    await user.click(within(group).getByRole("button", { name: "clear" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("FieldValue shows a value that has no control", () => {
    render(<FieldValue>v1.2.3</FieldValue>);
    const value = screen.getByText("v1.2.3");
    expect(value).toHaveClass("fd-field-value");
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("Chip shows its text and its remove button is named by label", async () => {
    const user = userEvent.setup();
    const onRemove = vi.fn();
    render(
      <Chip label="remove host-a" onRemove={onRemove}>
        host-a
      </Chip>,
    );
    expect(screen.getByText("host-a")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "remove host-a" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it("each of several Chips removes only itself", async () => {
    const user = userEvent.setup();
    const onRemove = vi.fn();
    render(
      <>
        <Chip label="remove a" onRemove={() => onRemove("a")}>
          a
        </Chip>
        <Chip label="remove b" onRemove={() => onRemove("b")}>
          b
        </Chip>
      </>,
    );
    await user.click(screen.getByRole("button", { name: "remove b" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(onRemove).toHaveBeenCalledWith("b");
  });
});

describe("Segmented", () => {
  // Segmented moves the focus in a requestAnimationFrame callback. Run those callbacks after the event
  // (as a browser does) and keep what they throw, so a failure in them is a test result, not an uncaught error.
  let frameErrors: unknown[] = [];
  beforeEach(() => {
    frameErrors = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
      setTimeout(() => {
        try {
          callback(0);
        } catch (error) {
          frameErrors.push(error);
        }
      }, 0),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  const options = [
    { value: "one", label: "One" },
    { value: "two", label: "Two" },
    { value: "three", label: "Three" },
  ] as const;

  it("is a radiogroup named by label; the chosen option is checked", () => {
    render(<Segmented label="size" options={[...options]} value="two" onChange={() => {}} />);
    expect(screen.getByRole("radiogroup", { name: "size" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Two" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "One" })).not.toBeChecked();
  });

  it("only the chosen option is in the tab order", () => {
    render(<Segmented label="size" options={[...options]} value="two" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: "Two" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "One" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("radio", { name: "Three" })).toHaveAttribute("tabindex", "-1");
  });

  it("a click calls onChange with the option's value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Segmented label="size" options={[...options]} value="one" onChange={onChange} />);
    await user.click(screen.getByRole("radio", { name: "Three" }));
    expect(onChange).toHaveBeenCalledWith("three");
  });

  it("the arrow keys move the choice, wrapping at the ends", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = render(<Segmented label="size" options={[...options]} value="two" onChange={onChange} />);
    screen.getByRole("radio", { name: "Two" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(onChange).toHaveBeenLastCalledWith("three");
    await user.keyboard("{ArrowDown}");
    expect(onChange).toHaveBeenLastCalledWith("three");
    await user.keyboard("{ArrowLeft}");
    expect(onChange).toHaveBeenLastCalledWith("one");
    await user.keyboard("{ArrowUp}");
    expect(onChange).toHaveBeenLastCalledWith("one");
    // wraps
    rerender(<Segmented label="size" options={[...options]} value="three" onChange={onChange} />);
    screen.getByRole("radio", { name: "Three" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(onChange).toHaveBeenLastCalledWith("one");
    rerender(<Segmented label="size" options={[...options]} value="one" onChange={onChange} />);
    screen.getByRole("radio", { name: "One" }).focus();
    await user.keyboard("{ArrowLeft}");
    expect(onChange).toHaveBeenLastCalledWith("three");
  });

  it("other keys do nothing", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Segmented label="size" options={[...options]} value="one" onChange={onChange} />);
    screen.getByRole("radio", { name: "One" }).focus();
    await user.keyboard("a");
    expect(onChange).not.toHaveBeenCalled();
  });

  // (the handler must not read e.currentTarget inside requestAnimationFrame: it is null once the event is dispatched)
  it("the chosen option keeps the focus after an arrow key", async () => {
    const user = userEvent.setup();
    function Parent() {
      const [v, setV] = useState<"one" | "two" | "three">("one");
      return <Segmented label="size" options={[...options]} value={v} onChange={setV} />;
    }
    render(<Parent />);
    screen.getByRole("radio", { name: "One" }).focus();
    await user.keyboard("{ArrowRight}");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(frameErrors).toEqual([]);
    expect(screen.getByRole("radio", { name: "Two" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Two" })).toHaveFocus();
  });

  it("disabled ignores clicks and keys", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Segmented label="size" options={[...options]} value="one" onChange={onChange} disabled />);
    await user.click(screen.getByRole("radio", { name: "Two" }));
    fireEvent.keyDown(screen.getByRole("radiogroup"), { key: "ArrowRight" });
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("Tabs", () => {
  it("is a tablist named by label with the current tab selected", () => {
    render(<Tabs label="views" value="b" tabs={[{ key: "a", label: "Alpha" }, { key: "b", label: "Beta" }]} onChange={() => {}} />);
    expect(screen.getByRole("tablist", { name: "views" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Beta" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Alpha" })).toHaveAttribute("aria-selected", "false");
  });

  it("a click calls onChange with the tab's key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Tabs label="views" value="a" tabs={[{ key: "a", label: "Alpha" }, { key: "b", label: "Beta" }]} onChange={onChange} />);
    await user.click(screen.getByRole("tab", { name: "Beta" }));
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("a tab with an href is a link to it and does not call onChange", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Tabs
        label="views"
        value="a"
        tabs={[
          { key: "a", label: "Alpha", href: "#/logs/alpha" },
          { key: "b", label: "Beta", href: "#/logs/beta" },
        ]}
        onChange={onChange}
      />,
    );
    const beta = screen.getByRole("tab", { name: "Beta" });
    expect(beta.tagName).toBe("A");
    expect(beta).toHaveAttribute("href", "#/logs/beta");
    await user.click(beta);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("works without onChange", async () => {
    const user = userEvent.setup();
    render(<Tabs label="views" value="a" tabs={[{ key: "a", label: "Alpha" }, { key: "b", label: "Beta" }]} />);
    await user.click(screen.getByRole("tab", { name: "Beta" }));
    expect(screen.getByRole("tab", { name: "Alpha" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("form layout helpers", () => {
  it("FormGrid holds its fields", () => {
    const { container } = render(
      <FormGrid>
        <Field label="a">
          <Input />
        </Field>
        <Field label="b">
          <Input />
        </Field>
      </FormGrid>,
    );
    expect(container.querySelector(".fd-form")).toContainElement(screen.getByLabelText("a"));
    expect(container.querySelector(".fd-form")).toContainElement(screen.getByLabelText("b"));
  });

  it("FormGroup is a section named by its title, with a heading, its fields and its notes", () => {
    render(
      <FormGroups>
        <FormGroup title="Connection" notes={["Needs a restart.", "Not for production."]}>
          <Field label="host">
            <Input />
          </Field>
          <FormWide>
            <Field label="extra json">
              <Textarea />
            </Field>
          </FormWide>
        </FormGroup>
        <FormGroup title="Limits">
          <Field label="timeout">
            <Input />
          </Field>
        </FormGroup>
      </FormGroups>,
    );
    const group = screen.getByRole("region", { name: "Connection" });
    expect(within(group).getByRole("heading", { name: "Connection", level: 3 })).toBeInTheDocument();
    expect(within(group).getByLabelText("host")).toBeInTheDocument();
    expect(within(group).getByLabelText("extra json").closest(".fd-form-wide")).not.toBeNull();
    expect(within(group).getByText("Needs a restart.")).toBeInTheDocument();
    expect(within(group).getByText("Not for production.")).toBeInTheDocument();
    // a group does not take another group's fields or notes
    expect(within(screen.getByRole("region", { name: "Limits" })).queryByLabelText("host")).toBeNull();
    expect(within(screen.getByRole("region", { name: "Limits" })).queryByText("Needs a restart.")).toBeNull();
  });

  it("Toolbar without side is one row; with side it has the side part and the main part", () => {
    const { container, rerender } = render(
      <Toolbar>
        <Input aria-label="filter" />
      </Toolbar>,
    );
    expect(container.querySelector(".fd-toolbar-side")).toBeNull();
    expect(screen.getByLabelText("filter")).toBeInTheDocument();
    rerender(
      <Toolbar side={<Input aria-label="search" />}>
        <Input aria-label="filter" />
      </Toolbar>,
    );
    const side = container.querySelector(".fd-toolbar-side") as HTMLElement;
    const main = container.querySelector(".fd-toolbar-main") as HTMLElement;
    expect(within(side).getByLabelText("search")).toBeInTheDocument();
    expect(within(main).getByLabelText("filter")).toBeInTheDocument();
    expect(within(main).queryByLabelText("search")).toBeNull();
  });

  it("SaveBar holds the buttons of a form's unsaved changes", async () => {
    const user = userEvent.setup();
    const save = vi.fn();
    render(
      <SaveBar>
        <Button onClick={save}>save</Button>
        <Button>discard</Button>
      </SaveBar>,
    );
    await user.click(screen.getByRole("button", { name: "save" }));
    expect(save).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "discard" })).toBeInTheDocument();
  });
});

describe("ListPicker", () => {
  const options = [
    { value: "general", label: "General" },
    { value: "model", label: "Model" },
  ];

  it("has a select naming the current item and the list as children", () => {
    render(
      <ListPicker label="section" value="model" options={options} onChange={() => {}}>
        <List label="sections">
          <ListItem title="General" href="#/settings/general" />
          <ListItem title="Model" href="#/settings/model" selected />
        </List>
      </ListPicker>,
    );
    expect(screen.getByRole("combobox", { name: "section" })).toHaveValue("model");
    expect(screen.getByRole("navigation", { name: "sections" })).toBeInTheDocument();
  });

  it("choosing in the select calls onChange with the value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ListPicker label="section" value="model" options={options} onChange={onChange}>
        <List>
          <ListItem title="General" href="#/settings/general" />
        </List>
      </ListPicker>,
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "section" }), "general");
    expect(onChange).toHaveBeenCalledWith("general");
  });
});
