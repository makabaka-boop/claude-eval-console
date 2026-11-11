import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import { createContext, runInContext } from "node:vm";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

let listener = null;
let documentNumber = 1;
let isolatedPage;
let resetOnce = true;
let alwaysReset = false;
let resets = 0;
const uploadIds = new Set();
function newDocument() {
  isolatedPage = createContext({
    document: { cookie: "solo_qa_csrf=csrf-test" },
    fetch: (...args) => globalThis.fetch(...args),
    File, FormData, atob, btoa,
  });
}
newDocument();
globalThis.chrome = {
  runtime: { onMessage: { addListener(callback) { listener = callback; } } },
  tabs: { async query() { return [{ id: 42, active: true, url: "https://solo2.jzxhnh.com/app/gsb/submit" }]; } },
  scripting: {
    async executeScript({ func, args, target, world }) {
      assert.equal(world, "ISOLATED", "page scripts must not share the upload cache");
      assert.equal(target.tabId, 42);
      const chunk = args[0]?.uploadId;
      if (chunk) {
        uploadIds.add(chunk);
        if (args[0].index === 1 && (resetOnce || alwaysReset)) {
          resetOnce = false;
          resets += 1;
          documentNumber += 1;
          newDocument();
        }
      }
      const documentId = `document-${documentNumber}`;
      if (target.documentIds && !target.documentIds.includes(documentId)) {
        throw new Error("No document with the requested documentId");
      }
      if ((chunk && args[0].index > 0) || args[0]?.body?.kind === "staged-file") {
        assert.deepEqual(target.documentIds, [documentId], "every later chunk and upload must use the first document");
      }
      isolatedPage.injectionArgs = args;
      const result = await runInContext(`(${func.toString()})(...injectionArgs)`, isolatedPage);
      return [{ result, frameId: 0, documentId }];
    },
  },
};

const pairId = "abc123abc123";
const traces = {
  A: new Blob(['{"type":"result","side":"A"}\n', new Uint8Array(600 * 1024)], { type: "application/x-ndjson" }),
  B: new Blob(['{"type":"result","side":"B"}\n'], { type: "application/x-ndjson" }),
};
const recordings = {
  A: new Blob([new Uint8Array(1024 * 1024), "synthetic-a-video"], { type: "video/x-m4v" }),
  B: new Blob([new Uint8Array(1024 * 1024), "synthetic-b-video"], { type: "video/x-m4v" }),
};
const digest = async (blob) => createHash("sha256").update(Buffer.from(await blob.arrayBuffer())).digest("hex");
const traceDigests = { A: await digest(traces.A), B: await digest(traces.B) };
const recordingDigests = { A: await digest(recordings.A), B: await digest(recordings.B) };
const localStates = [], requests = [];
let uploadCount = 0;
let transientUploadFailure = true;
let corruptLocalTrace = false;
let submissionCount = 0;

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

globalThis.fetch = async (url, options = {}) => {
  const href = String(url), method = options.method || "GET";
  requests.push({ href, method, credentials: options.credentials || "", headers: Object.fromEntries(new Headers(options.headers || {}).entries()) });
  if (href.endsWith(`/api/pairwise/solo/${pairId}/payload`)) {
    return jsonResponse({
      id: pairId,
      values: {
        user_prompt: "实现一条可回验的困难任务",
        question_type: "0-1代码生成", difficulty: "困难", languages: "Go, Docker Compose",
        harness: "Claude Code", harness_version: "2.1.263", os_platform: "MacOS/Linux",
        repro_level: "已容器化，可一键起环境",
        env_snapshot: `https://github.com/test/repo/commit/${"a".repeat(40)}`,
        a_session_id: "session-A", "A-产物快照": `https://github.com/test/repo/commit/${"b".repeat(40)}`,
        b_session_id: "session-B", "B-产物快照": `https://github.com/test/repo/commit/${"c".repeat(40)}`,
        a_delivery_score: 5,
        a_delivery_description: "A 已逐项核对需求、构建和真实运行，核心功能完整，固定验收通过。",
        b_delivery_score: 3,
        b_delivery_description: "B 主流程可运行，但边界错误结构与题面不一致，异常场景下会返回错误结果。",
        gsb_verdict: "A 更好",
        gsb_reason: "A 的实现覆盖核心流程和边界条件，验收输出稳定，过程也没有影响结果的返工；B 完成了主流程，但边界错误结构不稳定，具体接口返回与题面不一致。综合产物完整性和实际业务影响，A 更好。",
        validity: "有效", remark: "",
      },
      payload_sha256: "d".repeat(64),
      models: { A: "auto_model/urm", B: "ark/urm-03" },
      trajectories: {
        A: { name: "A.jsonl", size: traces.A.size, sha256: traceDigests.A, url: `http://127.0.0.1:8765/api/pairwise/solo/${pairId}/trajectory/A` },
        B: { name: "B.jsonl", size: traces.B.size, sha256: traceDigests.B, url: `http://127.0.0.1:8765/api/pairwise/solo/${pairId}/trajectory/B` },
      },
      recordings: {
        A: { name: "A.m4v", size: recordings.A.size, sha256: recordingDigests.A, url: `http://127.0.0.1:8765/api/pairwise/attempts/aaaaaaaaaaaa/recording-file` },
        B: { name: "B.m4v", size: recordings.B.size, sha256: recordingDigests.B, url: `http://127.0.0.1:8765/api/pairwise/attempts/bbbbbbbbbbbb/recording-file` },
      },
      solo_qa: { state: "not_submitted", remote_id: "" },
    });
  }
  const trajectory = href.match(/\/trajectory\/(A|B)$/);
  if (trajectory) return new Response(corruptLocalTrace ? new Blob(["corrupted"]) : traces[trajectory[1]], { status: 200 });
  if (href.includes("/attempts/aaaaaaaaaaaa/recording-file")) return new Response(recordings.A, { status: 200 });
  if (href.includes("/attempts/bbbbbbbbbbbb/recording-file")) return new Response(recordings.B, { status: 200 });
  if (href.endsWith("/api/pairwise/solo/state")) {
    localStates.push(JSON.parse(options.body));
    return jsonResponse({ state: localStates.at(-1).state });
  }
  if (href.includes("/api/v1/gsb/submissions?page=1&page_size=20&keyword=")) return jsonResponse({ items: [], meta: { total: 0 } });
  if (href.endsWith("/api/v1/gsb/form-schema")) {
    const field = (field_key, label, field_type = "text", is_required = true, extra = {}) => ({ field_key, label, field_type, is_required, ...extra });
    return jsonResponse({ fingerprint: "gsb-schema-test", attachment_max_mb: 27, fields: [
      field("user_prompt","User Prompt","textarea"), field("question_type","任务类型","select",true,{options:["0-1代码生成","feature迭代","Bug修复"]}),
      field("difficulty","任务难度","select",true,{options:["困难","地狱"]}), field("languages","语言/框架"),
      field("harness","Harness","select",true,{options:["Claude Code","Codex CLI"]}), field("harness_version","Harness 版本"),
      field("os_platform","操作系统","select",true,{options:["MacOS/Linux","Windows"]}),
      field("repro_level","环境可复现等级","select",true,{options:["无外部依赖","有外部依赖，未容器化","已容器化，可一键起环境"]}), field("env_snapshot","初始环境快照","url"),
      field("a_session_id","A-SessionID"), field("a_trace_file","A-轨迹文件","attachment"), field("a_artifact_snapshot","A-产物快照","url"), field("a_screencast","A-运行录屏","video"),
      field("a_score_delivery","A-交付完整性","number"), field("a_desc_delivery","A-交付完整性描述","textarea"),
      field("x_a_model_name","A-模型名称","select",true,{options:["auto_model/urm","ark/urm-03"]}),
      field("b_session_id","B-SessionID"), field("b_trace_file","B-轨迹文件","attachment"), field("b_artifact_snapshot","B-产物快照","url"), field("b_screencast","B-运行录屏","video"),
      field("b_score_delivery","B-交付完整性","number"), field("b_desc_delivery","B-交付完整性描述","textarea"),
      field("x_b_model_name","B-模型名称","select",true,{options:["auto_model/urm","ark/urm-03"]}),
      field("gsb_verdict","GSB 结论","select",true,{options:["A 更好","Same","B 更好"]}), field("gsb_reason","GSB 理由","textarea"),
    ] });
  }
  if (href.endsWith("/api/v1/submissions/upload")) {
    if (transientUploadFailure) {
      transientUploadFailure = false;
      throw new TypeError("Failed to fetch");
    }
    assert.equal(method,"POST"); assert.ok(options.body instanceof FormData);
    const file = options.body.get("file");
    const isVideo = uploadCount === 1 || uploadCount === 3;
    assert.ok(file instanceof Blob);
    assert.equal(file.name, isVideo ? `${uploadCount === 1 ? "A" : "B"}.m4v` : `${uploadCount === 0 ? "A" : "B"}.jsonl`);
    assert.equal(options.body.get("kind"), isVideo ? "video" : null);
    const side = uploadCount < 2 ? "A" : "B";
    assert.equal(await digest(file), isVideo ? recordingDigests[side] : traceDigests[side], "assembled bytes must match the original evidence after retries");
    uploadCount += 1;
    return jsonResponse(isVideo
      ? { name: `server-generated-${uploadCount}`, url: `https://objects.test/${file.name}` }
      : { name: `server-generated-${uploadCount}`, path: `uploads/${uploadCount}`, size: file.size });
  }
  if (href.endsWith("/api/v1/gsb/submissions") && method === "POST") {
    submissionCount += 1;
    const body = JSON.parse(options.body);
    assert.equal(body.schema_fingerprint,"gsb-schema-test");
    assert.equal(body.data.a_trace_file[0].path,"uploads/1");
    assert.equal(body.data.a_screencast,"https://objects.test/A.m4v");
    assert.equal(body.data.b_trace_file[0].path,"uploads/3");
    assert.equal(body.data.b_screencast,"https://objects.test/B.m4v");
    assert.equal(body.data.a_score_delivery,5);
    assert.match(body.data.a_desc_delivery,/逐项核对需求/);
    assert.equal(body.data.b_score_delivery,3);
    assert.match(body.data.b_desc_delivery,/边界错误结构/);
    assert.equal(body.data.x_a_model_name,"auto_model/urm");
    assert.equal(body.data.x_b_model_name,"ark/urm-03");
    assert.equal(body.data.a_trace_file[0].name,"A.jsonl");
    assert.equal(body.data.b_trace_file[0].name,"B.jsonl");
    assert.equal(body.data.repro_level,"已容器化，可一键起环境");
    return jsonResponse({ id: 901, status: "SUBMITTED", message: "提交成功" });
  }
  throw new Error(`unexpected request: ${href}`);
};

await import("../chrome-solo-qa-helper/background.js");
const response = await new Promise((resolve) => {
  assert.equal(listener({ type: "SOLO_GSB_SUBMIT", payload: { pair_ids: [pairId] } },
    { url: "http://127.0.0.1:8765/#exports" }, resolve), true);
});
assert.equal(response.ok,true);
assert.equal(response.data.results[0].outcome,"submitted",JSON.stringify(response.data));
assert.equal(response.data.results[0].remote_id,"901");
assert.equal(uploadCount,4);
assert.deepEqual(localStates.map((item)=>item.state),["submitting","qc_pending"]);
const remoteWrites=requests.filter((item)=>item.href.startsWith("/api/v1/")&&item.method==="POST");
assert.ok(remoteWrites.every((item)=>item.credentials==="include"));
assert.ok(remoteWrites.every((item)=>item.headers["x-csrf-token"]==="csrf-test"));
assert.equal(resets, 1, "the first multi-chunk file must recover from a real document reset");
assert.ok(uploadIds.size >= 5, "a document reset and a network failure must use fresh staging IDs");
assert.equal(submissionCount, 1);
assert.deepEqual(Object.keys(runInContext("globalThis.__soloQaGsbUploadChunks", isolatedPage)), []);

// Repeated document changes must stop after the bounded retries, without
// uploading a truncated file or creating a remote submission.
alwaysReset = true;
const resetsBeforeFailure = resets;
const failed = await new Promise((resolve) => {
  listener({ type: "SOLO_GSB_SUBMIT", payload: { pair_ids: [pairId] } },
    { url: "http://127.0.0.1:8765/#exports" }, resolve);
});
assert.equal(failed.data.results[0].outcome, "failed");
assert.match(failed.data.results[0].error, /A 轨迹上传失败（已自动重试）.*第 2\/2 块/);
assert.equal(resets - resetsBeforeFailure, 3);
assert.equal(uploadCount, 4);
assert.equal(submissionCount, 1);
assert.equal(localStates.at(-1).state, "failed");

// Hash mismatches are evidence changes, not a transient transport failure.
alwaysReset = false;
corruptLocalTrace = true;
const idsBeforeCorruption = uploadIds.size;
const corrupted = await new Promise((resolve) => {
  listener({ type: "SOLO_GSB_SUBMIT", payload: { pair_ids: [pairId] } },
    { url: "http://127.0.0.1:8765/#exports" }, resolve);
});
assert.match(corrupted.data.results[0].error, /A 轨迹文件摘要与本地记录不一致/);
assert.equal(uploadIds.size, idsBeforeCorruption);
assert.equal(submissionCount, 1);
console.log("SOLO GSB upload: document recovery, exact bytes, bounded retries and unchanged-evidence checks passed");
