// The settings document the Settings page renders: values, the JSON schema the backend's pydantic models
// produce (same shapes: $defs and $ref, anyOf with a "auto" constant, duration and password formats, an
// array of objects with a `test` extra...), and the section titles. Everything is invented.
import type { JsonObject, JsonSchema, LlmTestResult, ModelInfo, Schemas, SettingsDoc } from "../../../src/api";
import type { ApiResponse } from "../types";

/** The values as the API answers them: secrets that are set come back as the mask. */
export const settingsValues: Schemas["Settings"] = {
  general: {
    log_level: "info",
    language: "en",
    root_font_size: 16,
    docker_url: "tcp://docker-proxy.test:2375",
    docker_hub_user: "test-user",
    docker_hub_token: "********",
    timezone: "UTC",
    instructions: "Keep answers short.",
  },
  llm: {
    base_url: "http://llm.test:11434/v1",
    api_key: "********",
    model: "test-main",
    max_output_tokens: 16384,
    reasoning_effort: "auto",
    temperature: "auto",
    top_p: "auto",
    top_k: "auto",
    timeout: "3m",
    max_retries: 2,
    extra_body: {},
    structured_output: "json_schema",
  },
  worker: {
    base_url: "",
    api_key: "",
    model: "test-worker",
    max_output_tokens: 8192,
    reasoning_effort: "none",
    temperature: 0.2,
    top_p: "auto",
    top_k: 40,
    timeout: "1m",
    max_retries: 2,
    extra_body: { keep_alive: "5m" },
    structured_output: "json_schema",
  },
  chat: {
    max_steps: 15,
    worker_max_steps: 20,
    command_timeout: "5m",
    history_messages: 12,
    max_tool_result_chars: 6000,
    tool_timeout: "1h",
  },
  jobs: { max_concurrent_runs: 1, timeout: "15m", max_steps: 12 },
  storage: { retention: "30d", prune_interval: "1h" },
  logs: { llm_trace: true, store_bodies: true, retention: "7d", max_total_mb: 200, keep_app_log: true },
  outputs: {
    telegram: {
      enabled: false,
      token: "",
      allowed_chat_ids: [1001, 1002],
      new_session_after: "6h",
      api_url: "",
      web_url: "",
    },
  },
  mcp: {
    servers: [
      {
        name: "test-server",
        enabled: true,
        url: "http://mcp-a.test:8080/mcp",
        transport: "auto",
        token: "********",
        headers: [{ name: "X-Api-Key", value: "********" }],
        timeout: "30s",
        tool_filter: ["list_things"],
        default_policy: "approve",
        tool_overrides: [],
      },
      {
        name: "second-server",
        enabled: false,
        url: "http://mcp-b.test/sse",
        transport: "sse",
        token: "",
        headers: [],
        timeout: "30s",
        tool_filter: [],
        default_policy: "suggest",
        tool_overrides: [],
      },
    ],
    dry_run: false,
  },
  approvals: { approval_ttl: "1d", shell_changes: "approve" },
};

export const sectionTitles: Record<string, string> = {
  general: "General",
  llm: "Main model",
  worker: "Worker model",
  chat: "Chat",
  jobs: "Jobs",
  storage: "Data retention",
  logs: "Logs",
  approvals: "Approvals",
  outputs: "Outputs",
  mcp: "MCP servers",
};

const DURATION_PATTERN = "^\\s*(\\d+(\\.\\d+)?\\s*(ms|s|m|h|d|w)\\s*)+$|^\\s*\\d+(\\.\\d+)?\\s*$";

const secret = (description: string): JsonSchema => ({ type: "string", format: "password", writeOnly: true, default: "", description } as JsonSchema);
const duration = (def: string, description: string): JsonSchema => ({
  type: "string",
  format: "duration",
  pattern: DURATION_PATTERN,
  examples: ["30s", "5m", "6h", "30d"],
  default: def,
  description,
});
const text = (def: string, description: string, extra: Partial<JsonSchema> = {}): JsonSchema => ({ type: "string", default: def, description, ...extra });
const int = (def: number, description: string, minimum?: number, maximum?: number): JsonSchema => ({ type: "integer", default: def, description, minimum, maximum });
const flag = (def: boolean, description: string): JsonSchema => ({ type: "boolean", default: def, description });
const choice = (values: string[], def: string, description: string): JsonSchema => ({ type: "string", enum: values, default: def, description });
/** "a number, or auto" as pydantic writes `Annotated[float, ...] | Literal["auto"]`. */
const numberOrAuto = (type: "number" | "integer", description: string, minimum?: number, maximum?: number): JsonSchema => ({
  anyOf: [{ type, minimum, maximum }, { type: "string", const: "auto" }],
  default: "auto",
  description,
});

const llmProperties = (baseUrlHelp: string): Record<string, JsonSchema> => ({
  base_url: text("", baseUrlHelp, { pattern: "^(https?://\\S+)?$" }),
  api_key: secret("API key, if the endpoint needs one."),
  model: text("", "Model name as the endpoint knows it."),
  max_output_tokens: int(16384, "Most tokens per answer, counting a thinking model's reasoning.", 64),
  reasoning_effort: choice(["auto", "none", "low", "medium", "high"], "auto", "How much a thinking model reasons before answering."),
  temperature: numberOrAuto("number", "Randomness of answers. auto = not sent.", 0, 2),
  top_p: numberOrAuto("number", "Nucleus sampling. auto = not sent.", 0, 1),
  top_k: numberOrAuto("integer", "Only this many most likely words are considered. auto = not sent.", 0),
  timeout: duration("3m", "Per-request timeout."),
  max_retries: int(2, "How many times a failed request is retried.", 0, 10),
  extra_body: { type: "object", additionalProperties: true, description: "Extra provider-specific request fields (JSON)." },
  structured_output: choice(["json_schema", "tool_call"], "json_schema", "How the short helper calls get their structured answer."),
});

const defs: Record<string, JsonSchema> = {
  GeneralSettings: {
    type: "object",
    properties: {
      log_level: choice(["debug", "info", "warning", "error"], "info", "Log verbosity of the service."),
      language: text("en", "Language of chat answers."),
      root_font_size: { type: "number", exclusiveMinimum: 0, default: 16, description: "Text size of the web UI in pixels." } as JsonSchema,
      docker_url: text("", "Docker API the shell's docker commands use. Empty = $DOCKER_HOST."),
      docker_hub_user: text("", "Docker Hub user name for the image check."),
      docker_hub_token: secret("Docker Hub access token of that user. Kept secret."),
      timezone: text("", "Time zone of cron schedules, e.g. Europe/Berlin. Empty = the host's own."),
      instructions: text("", "Your standing instructions for the model.", { format: "multiline" }),
    },
  },
  ApprovalsSettings: {
    type: "object",
    description: "Actions waiting for your decision.",
    properties: {
      approval_ttl: duration("1d", "Pending approvals expire after this."),
      shell_changes: choice(["off", "approve", "auto"], "approve", "What happens to commands that change the host."),
    },
  },
  LLMSettings: {
    type: "object",
    description: "The big model: it answers chat questions and decides when to hand a sub-task to the worker model.",
    properties: llmProperties("OpenAI-compatible API base URL. Empty = LLM not configured."),
  },
  WorkerLLMSettings: {
    type: "object",
    description: "A smaller, faster model for sub-tasks the main model hands off.",
    properties: llmProperties("OpenAI-compatible API base URL. Empty = the main model's endpoint and API key."),
  },
  JobsSettings: {
    type: "object",
    description: "Scheduled checks by the language model.",
    properties: {
      max_concurrent_runs: int(1, "How many job runs may use the models at the same time.", 1, 8),
      timeout: duration("15m", "Longest one job run may take."),
      max_steps: int(12, "Most tool-use rounds of one job run.", 1, 60),
    },
  },
  ChatSettings: {
    type: "object",
    properties: {
      max_steps: int(15, "Most tool-use rounds of the main model per question.", 1, 60),
      worker_max_steps: int(20, "Most tool-use rounds of one sub-task on the worker model.", 1, 60),
      command_timeout: duration("5m", "How long one shell command may run."),
      history_messages: int(12, "How many earlier messages the model sees.", 0, 100),
      max_tool_result_chars: int(6000, "Longest tool result passed to the model.", 500, 50000),
      tool_timeout: duration("1h", "Longest one tool call may run."),
    },
  },
  StorageSettings: {
    type: "object",
    properties: {
      retention: duration("30d", "How long chat messages and job runs are kept."),
      prune_interval: duration("1h", "How often old data is deleted."),
    },
  },
  LogsSettings: {
    type: "object",
    description: "What is kept of every request to the language model, and of the app's own log.",
    properties: {
      llm_trace: flag(true, "Record every request to the language model."),
      store_bodies: flag(true, "Keep the full text of prompts and answers."),
      retention: duration("7d", "How long recorded calls stay."),
      max_total_mb: int(200, "Upper limit for the recorded calls on disk.", 10, 10000),
      keep_app_log: flag(true, "Save warnings and errors of the app log in the database."),
    },
  },
  TelegramSettings: {
    type: "object",
    description: "The bot for chatting with the monitor.",
    properties: {
      enabled: flag(false, "Turn the Telegram bot on."),
      token: secret("Bot token."),
      allowed_chat_ids: { type: "array", items: { type: "integer" }, default: [], description: "Only these chats may talk to the bot." },
      new_session_after: duration("6h", "Start a fresh chat after this long without a message."),
      api_url: text("", "Bot API server. Empty = the public one.", { pattern: "^(https?://\\S+)?$" }),
      web_url: text("", "Address of this web UI as you open it.", { pattern: "^(https?://\\S+)?$" }),
    },
  },
  OutputsSettings: {
    type: "object",
    properties: {
      telegram: { $ref: "#/$defs/TelegramSettings", test: "telegram", test_modes: { send: "Send test message" } },
    },
  },
  McpHeader: {
    type: "object",
    properties: {
      name: text("", "Header name, e.g. X-Api-Key."),
      value: secret("Header value (kept secret)."),
    },
  },
  McpToolOverride: {
    type: "object",
    properties: {
      name: text("", "Tool name as the server calls it."),
      policy: choice(["inherit", "suggest", "approve", "auto"], "inherit", "inherit = the server's default policy."),
      kind: choice(["auto", "read", "write"], "auto", "auto = read-only only if the server says so."),
    },
  },
  McpServerSettings: {
    type: "object",
    properties: {
      name: text("", "Short name shown in chat and in tool names.", { pattern: "^[A-Za-z0-9][A-Za-z0-9-]{0,23}$" }),
      enabled: flag(true, "Off = never connected, its tools are not offered."),
      url: text("", "MCP endpoint of a remote server.", { pattern: "^https?://\\S+$" }),
      transport: choice(["auto", "streamable_http", "sse"], "auto", "auto = chosen by the URL."),
      token: secret("Bearer token, if the server needs one."),
      headers: { type: "array", items: { $ref: "#/$defs/McpHeader" }, default: [], description: "Extra HTTP headers." },
      timeout: duration("30s", "Connect timeout and per-call timeout."),
      tool_filter: { type: "array", items: { type: "string" }, default: [], description: "The tools the agents may use." },
      default_policy: choice(["suggest", "approve", "auto"], "approve", "For tools that change things."),
      tool_overrides: { type: "array", items: { $ref: "#/$defs/McpToolOverride" }, default: [], description: "Per-tool exceptions." },
    },
  },
  McpSettings: {
    type: "object",
    properties: {
      servers: { type: "array", items: { $ref: "#/$defs/McpServerSettings" }, default: [], test: "mcp", description: "MCP servers whose tools the chat may use." },
      dry_run: flag(false, "Pretend: record write calls but send nothing to the servers."),
    },
  },
};

export const settingsSchema: JsonSchema = {
  type: "object",
  properties: Object.fromEntries(
    [
      ["general", "GeneralSettings"],
      ["llm", "LLMSettings"],
      ["worker", "WorkerLLMSettings"],
      ["chat", "ChatSettings"],
      ["jobs", "JobsSettings"],
      ["storage", "StorageSettings"],
      ["logs", "LogsSettings"],
      ["outputs", "OutputsSettings"],
      ["mcp", "McpSettings"],
      ["approvals", "ApprovalsSettings"],
    ].map(([key, def]) => [key, { $ref: `#/$defs/${def}` }]),
  ),
  $defs: defs,
};

/** A fresh settings document (the caller may change it). */
export function makeSettingsDoc(change?: (doc: SettingsDoc) => void): SettingsDoc {
  const doc: SettingsDoc = {
    values: structuredClone(settingsValues) as unknown as JsonObject,
    schema: structuredClone(settingsSchema),
    sections: { ...sectionTitles },
  };
  change?.(doc);
  return doc;
}

/** What a model that is not an Ollama one answers: nothing to show. */
export const modelInfoUnavailable: ApiResponse<"/api/settings/llm-model-info", "post"> = { available: false, parameters: {}, capabilities: [], context_length: null };

/** What an Ollama model reports about itself. */
export const modelInfoOllama: ModelInfo = {
  available: true,
  parameters: { temperature: 0.7, top_k: 20, top_p: 0.9 },
  capabilities: ["completion", "tools", "thinking"],
  context_length: 32768,
};

export const llmTestOk: LlmTestResult = {
  ok: true,
  reply: "pong",
  latency_s: 0.42,
  tool_call: true,
  model_listed: true,
  models: null,
  structured_output: { ok: true, text: "Structured answer OK." },
};

export const connectionTestOk: ApiResponse<"/api/settings/test/{kind}", "post"> = {
  ok: true,
  message: "Connected: 2 tools.",
  details: {
    tools: [
      { name: "list_things", title: null, description: "List the things.", read_only: true },
      { name: "change_thing", title: null, description: "Change one thing.", read_only: false },
    ],
  },
};
