// src/components/EntityRefs.tsx: where one thing names another, as links. Each helper must point at the address
// the page of that kind has, and say the same words everywhere.
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  approvalHref,
  ApprovalRef,
  callHref,
  ChatRef,
  chatHref,
  ItemTarget,
  JobRef,
  jobHref,
  ModelRef,
  OwnerRef,
  RunRef,
  runHref,
  ServerRef,
  serverHref,
  SourceRef,
  ToolName,
} from "../../../src/components/EntityRefs";

describe("addresses", () => {
  it("point at the pages that own them", () => {
    expect(chatHref("abc")).toBe("#/chat/abc");
    expect(jobHref("example-job")).toBe("#/jobs/example-job");
    expect(runHref("example-job", 7)).toBe("#/jobs/example-job/runs/7");
    expect(approvalHref(41)).toBe("#/approvals/41");
    expect(serverHref("test-server")).toBe("#/settings/mcp/test-server");
    expect(callHref(12)).toBe("#/logs/llm/12");
  });

  it("encode a name that is not safe in an address", () => {
    expect(jobHref("a b/c")).toBe("#/jobs/a%20b%2Fc");
    expect(chatHref("x#y")).toBe("#/chat/x%23y");
    expect(serverHref("é?")).toBe("#/settings/mcp/%C3%A9%3F");
    expect(runHref("a b", "r 1")).toBe("#/jobs/a%20b/runs/r 1"); // the run id is a number in practice and is not encoded
  });
});

describe("ChatRef", () => {
  it("names a chat by the first eight characters of its id", () => {
    render(<ChatRef id="0123456789abcdef" />);
    expect(screen.getByRole("link", { name: "chat 01234567" })).toHaveAttribute("href", "#/chat/0123456789abcdef");
  });

  it("shows the words it is given instead", () => {
    render(<ChatRef id="abc">Disk question</ChatRef>);
    expect(screen.getByRole("link", { name: "Disk question" })).toHaveAttribute("href", "#/chat/abc");
  });
});

describe("JobRef", () => {
  it("names a job by its name, and by its slug when there is no name", () => {
    render(
      <>
        <JobRef slug="example-job" name="Example job" />
        <JobRef slug="bare-slug" />
      </>,
    );
    expect(screen.getByRole("link", { name: "Example job" })).toHaveAttribute("href", "#/jobs/example-job");
    expect(screen.getByRole("link", { name: "bare-slug" })).toHaveAttribute("href", "#/jobs/bare-slug");
  });

  it("opens a summary at the same address as a job (the page is the same)", () => {
    render(<JobRef slug="health" name="Health" summary />);
    expect(screen.getByRole("link", { name: "Health" })).toHaveAttribute("href", "#/jobs/health");
  });
});

describe("RunRef, ApprovalRef, ServerRef", () => {
  it("links a run to its job's run page", () => {
    render(<RunRef slug="example-job" id={7}>run 7</RunRef>);
    expect(screen.getByRole("link", { name: "run 7" })).toHaveAttribute("href", "#/jobs/example-job/runs/7");
  });

  it("names an approval by its number", () => {
    render(<ApprovalRef batch={41} />);
    expect(screen.getByRole("link", { name: "approval #41" })).toHaveAttribute("href", "#/approvals/41");
  });

  it("opens an MCP server's card in Settings", () => {
    render(<ServerRef name="test-server" />);
    expect(screen.getByRole("link", { name: "test-server" })).toHaveAttribute("href", "#/settings/mcp/test-server");
  });
});

describe("SourceRef", () => {
  it("opens the chat of a chat source and of a run button of a chat", () => {
    render(
      <>
        <SourceRef source="chat:0123456789abcdef" />
        <SourceRef source="user:run-button:fedcba9876543210" />
      </>,
    );
    expect(screen.getByRole("link", { name: "chat 01234567" })).toHaveAttribute("href", "#/chat/0123456789abcdef");
    expect(screen.getByRole("link", { name: "chat fedcba98" })).toHaveAttribute("href", "#/chat/fedcba9876543210");
  });

  it("opens the job of a job source, named by its slug", () => {
    render(<SourceRef source="job:example-job" />);
    expect(screen.getByRole("link", { name: "example-job" })).toHaveAttribute("href", "#/jobs/example-job");
  });

  it("shows any other source as plain text", () => {
    render(<p>from <SourceRef source="scheduler" /></p>);
    expect(screen.getByText("from scheduler")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("ItemTarget", () => {
  it("names the server of an MCP call as a link, then the tool", () => {
    const { container } = render(<ItemTarget item={{ target: "ignored", args: { server: "test-server", tool: "change_thing" } }} />);
    expect(screen.getByRole("link", { name: "test-server" })).toHaveAttribute("href", "#/settings/mcp/test-server");
    expect(container).toHaveTextContent("test-server · change_thing");
    expect(container).not.toHaveTextContent("ignored");
  });

  it("shows the target as it is when the call names no server and tool", () => {
    const { container, rerender } = render(<ItemTarget item={{ target: "container web-1", args: null }} />);
    expect(container).toHaveTextContent("container web-1");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    rerender(<ItemTarget item={{ target: "container web-2", args: { server: "test-server" } }} />);
    expect(container).toHaveTextContent("container web-2");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    rerender(<ItemTarget item={{ target: "container web-3", args: { server: 1, tool: 2 } }} />);
    expect(container).toHaveTextContent("container web-3");
  });

  it("shows nothing for an item without a target", () => {
    const { container } = render(<ItemTarget item={{ target: null, args: null }} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ToolName", () => {
  it("splits an MCP tool at the first double underscore: the server is a link", () => {
    const { container } = render(<ToolName name="test-server__list_things" />);
    expect(screen.getByRole("link", { name: "test-server" })).toHaveAttribute("href", "#/settings/mcp/test-server");
    expect(container).toHaveTextContent("test-server · list_things");
  });

  it("keeps a further double underscore in the tool's name", () => {
    const { container } = render(<ToolName name="srv__a__b" />);
    expect(screen.getByRole("link", { name: "srv" })).toBeInTheDocument();
    expect(container).toHaveTextContent("srv · a__b");
  });

  it("shows any other tool by its name, also one that starts with the separator", () => {
    const { container, rerender } = render(<ToolName name="bash" />);
    expect(container).toHaveTextContent("bash");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    rerender(<ToolName name="__odd" />);
    expect(container).toHaveTextContent("__odd");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("OwnerRef", () => {
  it("opens the chat of a chat call, with its step", () => {
    render(<OwnerRef kind="chat" id="0123456789abcdef" step={3} />);
    expect(screen.getByRole("link", { name: "chat 01234567 · step 3" })).toHaveAttribute("href", "#/chat/0123456789abcdef");
  });

  it("opens the job of a job call, with its step", () => {
    render(<OwnerRef kind="job" id="example-job" step={2} />);
    expect(screen.getByRole("link", { name: "job example-job · step 2" })).toHaveAttribute("href", "#/jobs/example-job");
  });

  it("leaves the step out when there is none", () => {
    render(
      <>
        <OwnerRef kind="chat" id="0123456789abcdef" />
        <OwnerRef kind="job" id="example-job" step={null} />
      </>,
    );
    expect(screen.getByRole("link", { name: "chat 01234567" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "job example-job" })).toBeInTheDocument();
  });

  it("says a chat or job call without an id as text (there is nothing to open)", () => {
    const { container } = render(
      <>
        <p><OwnerRef kind="chat" id={null} step={1} /></p>
        <p><OwnerRef kind="job" id={null} /></p>
      </>,
    );
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    const lines = within(container).getAllByText(/^(chat|job)/);
    expect(lines.map((l) => l.textContent?.trim())).toEqual(["chat  · step 1", "job"]);
  });

  it("says a kind of a removed feature as an earlier feature", () => {
    const { container } = render(<OwnerRef kind="collector" id="x" step={4} />);
    expect(container).toHaveTextContent("Earlier feature");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("ModelRef", () => {
  it("opens the settings of the main model", () => {
    render(<ModelRef model="main" />);
    expect(screen.getByRole("link", { name: "main model" })).toHaveAttribute("href", "#/settings/llm");
  });

  it("opens the settings of the worker model", () => {
    render(<ModelRef model="worker" />);
    expect(screen.getByRole("link", { name: "worker model" })).toHaveAttribute("href", "#/settings/worker");
  });
});
