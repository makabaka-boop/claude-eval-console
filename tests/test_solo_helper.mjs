import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

let listener = null;
globalThis.chrome = {
  runtime: {
    onMessage: {
      addListener(callback) {
        listener = callback;
      },
    },
  },
  tabs: {
    async query() {
      return [{ id: 42, active: true, url: "https://solo2.jzxhnh.com/app/submissions" }];
    },
  },
  scripting: {
    async executeScript({ func, args }) {
      const previousDocument = globalThis.document;
      globalThis.document = { cookie: "solo_qa_csrf=csrf-test" };
      try {
        return [{ result: await func(...args) }];
      } finally {
        globalThis.document = previousDocument;
      }
    },
  },
};

const trace = new Blob(['{"type":"result","result":"done"}\n'], { type: "application/x-ndjson" });
const traceBytes = Buffer.from(await trace.arrayBuffer());
const traceDigest = createHash("sha256").update(traceBytes).digest("hex");
const localStates = [];
const requests = [];
const createdOrder = [];
let createdCount = 0;

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

globalThis.fetch = async (url, options = {}) => {
  const href = String(url);
  requests.push({
    href,
    method: options.method || "GET",
    credentials: options.credentials || "",
    headers: Object.fromEntries(new Headers(options.headers || {}).entries()),
  });
  const payloadMatch = href.match(/\/api\/solo-qa\/turns\/(abc123abc123|def456def456)\/([12])\/payload$/);
  if (payloadMatch) {
    const runId = payloadMatch[1];
    const turnNumber = Number(payloadMatch[2]);
    const suffix = runId === "abc123abc123" ? "one" : "two";
    return jsonResponse({
      key: `${runId}:${turnNumber}`,
      values: {
        "User Prompt": `完成真实提交链路 ${suffix}-${turnNumber}`,
        "SessionID": `session-${suffix}`,
        "TurnID/PromptID": `turn-${suffix}-${turnNumber}`,
        "当前对话轮次排序": turnNumber,
      },
      payload_sha256: "a".repeat(64),
      trajectory: {
        name: "trace.jsonl",
        size: trace.size,
        sha256: traceDigest,
        url: `http://127.0.0.1:8765/api/solo-qa/turns/${runId}/${turnNumber}/trajectory`,
      },
      solo_qa: { state: "not_submitted", remote_id: "" },
    });
  }
  if (/\/api\/solo-qa\/turns\/(abc123abc123|def456def456)\/[12]\/trajectory$/.test(href)) {
    return new Response(trace, { status: 200 });
  }
  if (href.endsWith("/api/solo-qa/state")) {
    localStates.push(JSON.parse(options.body));
    return jsonResponse({ state: localStates.at(-1).state });
  }
  if (href.includes("/api/v1/submissions?page=1&page_size=20&keyword=")) {
    return jsonResponse({ items: [], meta: { total: 0 } });
  }
  if (href.endsWith("/api/v1/submissions/form-schema")) {
    return jsonResponse({
      fingerprint: "schema-test",
      attachment_max_mb: 20,
      fields: [
        { field_key: "user_prompt", label: "User Prompt", field_type: "textarea", is_required: true },
        { field_key: "session_id", label: "SessionID", field_type: "text", is_required: true },
        { field_key: "turn_id", label: "TurnID/PromptID", field_type: "text", is_required: true },
        { field_key: "round_no", label: "当前对话轮次排序", field_type: "number", is_required: true },
        { field_key: "trace_file", label: "轨迹文件", field_type: "attachment", is_required: true },
      ],
    });
  }
  if (href.endsWith("/api/v1/submissions/upload")) {
    assert.equal(options.method, "POST");
    assert.ok(options.body instanceof FormData);
    return jsonResponse({ name: "trace.jsonl", path: "uploads/trace.jsonl", size: trace.size });
  }
  if (href.endsWith("/api/v1/submissions") && options.method === "POST") {
    const body = JSON.parse(options.body);
    assert.equal(body.schema_fingerprint, "schema-test");
    assert.match(body.data.user_prompt, /^完成真实提交链路 (one|two)-[12]$/);
    assert.equal(body.data.trace_file[0].path, "uploads/trace.jsonl");
    createdOrder.push(`${body.data.session_id}:${body.data.round_no}`);
    createdCount += 1;
    return jsonResponse({ id: 122 + createdCount, status: "SUBMITTED", message: "提交成功" });
  }
  throw new Error(`unexpected request: ${href}`);
};

await import("../chrome-solo-qa-helper/background.js");
assert.equal(typeof listener, "function");

const response = await new Promise((resolve) => {
  const asynchronous = listener(
    {
      type: "SOLO_QA_SUBMIT",
      payload: {
        turn_keys: ["abc123abc123:2", "def456def456:1", "abc123abc123:1"],
      },
    },
    { url: "http://127.0.0.1:8765/#exports" },
    resolve,
  );
  assert.equal(asynchronous, true);
});

assert.equal(response.ok, true);
assert.equal(response.data.results[0].turn_key, "abc123abc123:1");
assert.equal(response.data.results[0].outcome, "submitted");
assert.equal(response.data.results[0].remote_id, "123");
assert.equal(response.data.results[1].turn_key, "abc123abc123:2");
assert.equal(response.data.results[1].outcome, "submitted");
assert.equal(response.data.results[1].remote_id, "124");
assert.equal(response.data.results[2].turn_key, "def456def456:1");
assert.equal(response.data.results[2].outcome, "submitted");
assert.equal(response.data.results[2].remote_id, "125");
assert.deepEqual(createdOrder, ["session-one:1", "session-one:2", "session-two:1"]);
assert.deepEqual(
  localStates.map((item) => item.state),
  ["submitting", "qc_pending", "submitting", "qc_pending", "submitting", "qc_pending"],
);
assert.equal(requests.filter((item) => item.href.endsWith("/submissions/form-schema")).length, 1);
assert.equal(requests.filter((item) => item.href.endsWith("/submissions/upload")).length, 3);
assert.equal(
  requests.filter((item) => item.href.endsWith("/submissions") && item.method === "POST").length,
  3,
);
assert.equal(requests.filter((item) => /\/submissions\/(123|124|125)$/.test(item.href)).length, 0);
const remoteWrites = requests.filter((item) => item.href.startsWith("/api/v1/") && item.method === "POST");
assert.ok(remoteWrites.length >= 2);
assert.ok(remoteWrites.every((item) => item.credentials === "include"));
assert.ok(remoteWrites.every((item) => item.headers["x-csrf-token"] === "csrf-test"));
