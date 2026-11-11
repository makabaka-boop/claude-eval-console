"use strict";

const LOCAL_ORIGIN = "http://127.0.0.1:8765";
const LOCAL_API = `${LOCAL_ORIGIN}/api/solo-qa`;
const SOLO_ORIGIN = "https://solo2.jzxhnh.com";
const SOLO_API = `${SOLO_ORIGIN}/api/v1`;
const TURN_KEY_RE = /^[a-f0-9]{12}:[1-9]\d*$/;
const PAIR_ID_RE = /^[a-f0-9]{12}$/;
const REMOTE_STATUS_TO_LOCAL = {
  SUBMITTED: "qc_pending",
  QC_PASSED: "qc_passed",
  PENDING_FIX: "needs_fix",
  DISCARDED: "discarded",
};
const FIELD_KEY_LABELS = {
  question_type: "任务类型",
  task_type: "任务类型",
  difficulty: "任务难度",
  languages: "语言/框架",
  language_framework: "语言/框架",
  harness: "Harness",
  harness_version: "Harness 版本",
  os: "操作系统",
  operating_system: "操作系统",
  reproducibility: "环境可复现等级",
  environment_reproducibility: "环境可复现等级",
  env_snapshot: "初始环境快照",
  user_prompt: "User Prompt",
  session_id: "SessionID",
  turn_id: "TurnID/PromptID",
  trace_file: "轨迹文件",
  trajectory: "轨迹文件",
  score_delivery: "交付完整性",
  desc_delivery: "交付完整性 - 描述",
  score_instruction: "指令遵循",
  desc_instruction: "指令遵循 - 描述",
  score_planning: "任务规划",
  desc_planning: "任务规划 - 描述",
  score_reasoning: "推理能力",
  desc_reasoning: "推理能力 - 描述",
  score_execution: "执行能力",
  desc_execution: "执行能力 - 描述",
  other_issues: "其他问题",
  round_no: "当前对话轮次排序",
};
const GSB_FIELD_KEY_LABELS = {
  user_prompt: "User Prompt",
  question_type: "任务类型",
  difficulty: "任务难度",
  languages: "语言/框架",
  language_framework: "语言/框架",
  harness: "Harness",
  harness_version: "Harness 版本",
  os_platform: "操作系统",
  operating_system: "操作系统",
  repro_level: "环境可复现等级",
  reproducibility: "环境可复现等级",
  env_snapshot: "初始环境快照",
  a_session_id: "A-SessionID",
  a_artifact_snapshot: "A-产物快照",
  a_video: "A-运行录屏",
  a_recording: "A-运行录屏",
  a_delivery_score: "A-交付完整性",
  a_score_delivery: "A-交付完整性",
  a_delivery: "A-交付完整性",
  a_completeness: "A-交付完整性",
  a_delivery_description: "A-交付完整性描述",
  a_desc_delivery: "A-交付完整性描述",
  a_delivery_desc: "A-交付完整性描述",
  a_completeness_description: "A-交付完整性描述",
  b_session_id: "B-SessionID",
  b_artifact_snapshot: "B-产物快照",
  b_video: "B-运行录屏",
  b_recording: "B-运行录屏",
  b_delivery_score: "B-交付完整性",
  b_score_delivery: "B-交付完整性",
  b_delivery: "B-交付完整性",
  b_completeness: "B-交付完整性",
  b_delivery_description: "B-交付完整性描述",
  b_desc_delivery: "B-交付完整性描述",
  b_delivery_desc: "B-交付完整性描述",
  b_completeness_description: "B-交付完整性描述",
  gsb_verdict: "GSB 结论",
  gsb_reason: "GSB 理由",
  validity: "有效性",
  remark: "备注",
};
const GSB_FIELD_KEY_ALIASES = {
  a_score_delivery: "a_delivery_score",
  a_desc_delivery: "a_delivery_description",
  b_score_delivery: "b_delivery_score",
  b_desc_delivery: "b_delivery_description",
};

function errorMessage(body, fallback) {
  if (typeof body === "string" && body.trim()) return body;
  if (body && typeof body === "object") {
    if (typeof body.error === "string" && body.error) return body.error;
    if (Array.isArray(body.errors) && body.errors.length) {
      const fields = body.errors.map((item) => {
        const key = item?.field || item?.loc?.at?.(-1) || "";
        const message = item?.message || item?.msg || "";
        return [key, message].filter(Boolean).join("：");
      }).filter(Boolean).join("；");
      if (fields) return `${typeof body.detail === "string" ? `${body.detail}：` : ""}${fields}`;
    }
    if (body.errors && typeof body.errors === "object") {
      const fields = Object.entries(body.errors).map(([key, value]) => {
        const message = typeof value === "string" ? value : value?.message || value?.msg || JSON.stringify(value);
        return `${key}：${message}`;
      }).join("；");
      if (fields) return `${typeof body.detail === "string" ? `${body.detail}：` : ""}${fields}`;
    }
    if (typeof body.detail === "string" && body.detail) return body.detail;
    if (Array.isArray(body.detail) && body.detail[0]?.msg) return body.detail[0].msg;
    if (typeof body.message === "string" && body.message) return body.message;
  }
  return fallback;
}

async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      credentials: "omit",
      cache: "no-store",
      ...options,
    });
  } catch (error) {
    throw new Error(`本地评测台连接失败：${error instanceof Error ? error.message : String(error)}`);
  }
  const text = await response.text();
  let body = {};
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    throw new Error(errorMessage(body, `本地接口请求失败 (${response.status})`));
  }
  return body;
}

function localJson(path, options = {}) {
  return requestJson(`${LOCAL_API}${path}`, options);
}

async function soloQaTab() {
  const tabs = await chrome.tabs.query({ url: `${SOLO_ORIGIN}/*` });
  const tab = tabs.find((item) => item.active && Number.isInteger(item.id))
    || tabs.find((item) => Number.isInteger(item.id));
  if (!tab) {
    throw new Error("请先在 Chrome 打开并登录 SOLO-QA，再保持该页面打开");
  }
  return tab;
}

function bytesToBase64(bytes) {
  const chunks = [];
  const chunkSize = 32 * 1024;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + chunkSize)));
  }
  return btoa(chunks.join(""));
}

function soloInjectionTarget(tab, documentId = "") {
  return documentId ? { tabId: tab.id, documentIds: [documentId] } : { tabId: tab.id };
}

async function requestInSoloQaPage(path, options = {}, pageBody = null, selectedTab = null, documentId = "") {
  const tab = selectedTab || await soloQaTab();
  const method = String(options.method || "GET").toUpperCase();
  const headers = {};
  new Headers(options.headers || {}).forEach((value, key) => { headers[key] = value; });
  const body = pageBody || (
    typeof options.body === "string"
      ? { kind: "text", value: options.body }
      : { kind: "none" }
  );
  let injected;
  try {
    injected = await chrome.scripting.executeScript({
      target: soloInjectionTarget(tab, documentId),
      // Same-origin requests retain the page's login, while page scripts cannot
      // replace the upload buffers or the built-ins used to assemble the file.
      world: "ISOLATED",
      func: async (request) => {
        const cookieValue = (name) => {
          const prefix = `${name}=`;
          const entry = document.cookie.split("; ").find((item) => item.startsWith(prefix));
          return entry ? decodeURIComponent(entry.slice(prefix.length)) : "";
        };
        const requestHeaders = { ...request.headers };
        if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method)) {
          const csrfToken = cookieValue("solo_qa_csrf");
          if (!csrfToken) {
            return {
              ok: false,
              status: 403,
              body: { detail: "SOLO-QA 页面缺少 CSRF 凭据，请刷新已登录页面后重试" },
            };
          }
          requestHeaders["X-CSRF-Token"] = csrfToken;
        }
        let requestBody;
        if (request.body.kind === "text") {
          requestBody = request.body.value;
        } else if (request.body.kind === "file") {
          const binary = atob(request.body.base64);
          const bytes = new Uint8Array(binary.length);
          for (let index = 0; index < binary.length; index += 1) {
            bytes[index] = binary.charCodeAt(index);
          }
          const filename = request.body.filename || "trajectory.jsonl";
          const file = new File(
            [bytes], filename,
            { type: request.body.contentType || "application/x-ndjson" },
          );
          const form = new FormData();
          form.append("file", file);
          if (request.body.uploadKind) form.append("kind", request.body.uploadKind);
          requestBody = form;
          delete requestHeaders["content-type"];
          delete requestHeaders["Content-Type"];
        } else if (request.body.kind === "staged-file") {
          const staged = globalThis.__soloQaGsbUploadChunks?.[request.body.id];
          if (!Array.isArray(staged) || staged.length !== request.body.chunks) {
            return { ok: false, status: 400, body: { detail: "上传文件分块未完整传入，请重新提交" } };
          }
          delete globalThis.__soloQaGsbUploadChunks[request.body.id];
          const filename = request.body.filename || "recording.m4v";
          const file = new File(
            staged, filename,
            { type: request.body.contentType || "application/octet-stream" },
          );
          if (file.size !== request.body.size) {
            return { ok: false, status: 400, body: { detail: "上传文件分块大小不一致，请重新提交" } };
          }
          const form = new FormData();
          form.append("file", file);
          if (request.body.uploadKind) form.append("kind", request.body.uploadKind);
          requestBody = form;
          delete requestHeaders["content-type"];
          delete requestHeaders["Content-Type"];
        }
        let response;
        try {
          response = await fetch(`/api/v1${request.path}`, {
            method: request.method,
            headers: requestHeaders,
            body: requestBody,
            credentials: "include",
            cache: "no-store",
          });
        } catch (error) {
          return {
            ok: false,
            status: 0,
            body: { detail: `SOLO-QA 连接失败：${error instanceof Error ? error.message : String(error)}` },
          };
        }
        const text = await response.text();
        let responseBody = {};
        if (text) {
          try { responseBody = JSON.parse(text); } catch { responseBody = text; }
        }
        return { ok: response.ok, status: response.status, body: responseBody };
      },
      args: [{ path, method, headers, body }],
    });
  } catch (error) {
    throw new Error(`无法调用已登录的 SOLO-QA 页面：${error instanceof Error ? error.message : String(error)}`);
  }
  const result = injected?.[0]?.result;
  if (!result || typeof result.status !== "number") {
    throw new Error("SOLO-QA 页面没有返回有效结果，请刷新页面后重试");
  }
  if (!result.ok) {
    if (result.status === 401) {
      throw new Error("SOLO-QA 页面登录状态无效，请在该页面重新登录后刷新");
    }
    throw new Error(errorMessage(result.body, `SOLO-QA 请求失败 (${result.status || "网络错误"})`));
  }
  return result.body;
}

function remoteJson(path, options = {}) {
  return requestInSoloQaPage(path, options);
}

function normalizedUploadFilename(filename, uploadKind = "") {
  const fallback = uploadKind === "video" ? "recording.m4v" : "trajectory.jsonl";
  let safe = String(filename || fallback).split(/[\\/]/).pop().replace(/[\r\n";]/g, "_").trim();
  if (!safe) safe = fallback;
  if (uploadKind === "video") {
    if (!/\.(?:mp4|mov|webm|m4v)$/i.test(safe)) safe += ".m4v";
  } else if (!/\.jsonl$/i.test(safe)) {
    safe += ".jsonl";
  }
  return safe;
}

async function remoteFile(path, blob, filename, uploadKind = "") {
  const contentType = blob.type || "application/octet-stream";
  const safeFilename = normalizedUploadFilename(filename, uploadKind);
  let uploaded;
  if (blob.size <= 512 * 1024) {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    uploaded = await requestInSoloQaPage(path, { method: "POST" }, {
      kind: "file", base64: bytesToBase64(bytes), filename: safeFilename, contentType, uploadKind,
    });
    return { ...uploaded, name: safeFilename, size: uploaded?.size ?? blob.size };
  }
  const tab = await soloQaTab();
  const id = crypto.randomUUID();
  const chunkSize = 512 * 1024;
  const chunks = Math.ceil(blob.size / chunkSize);
  let documentId = "";
  try {
    for (let index = 0; index < chunks; index += 1) {
      const piece = new Uint8Array(await blob.slice(index * chunkSize, (index + 1) * chunkSize).arrayBuffer());
      let injected;
      try {
        injected = await chrome.scripting.executeScript({
          target: soloInjectionTarget(tab, documentId), world: "ISOLATED",
          func: ({ uploadId, index, total, base64 }) => {
            const storage = globalThis.__soloQaGsbUploadChunks ||= Object.create(null);
            const staged = storage[uploadId] ||= [];
            if (index !== staged.length || total < 1) {
              return { ok: false, received: staged.length, reason: "页面已刷新或分块缓存丢失" };
            }
            const binary = atob(base64);
            const bytes = new Uint8Array(binary.length);
            for (let byte = 0; byte < binary.length; byte += 1) bytes[byte] = binary.charCodeAt(byte);
            staged.push(bytes);
            return { ok: true, received: staged.length };
          },
          args: [{ uploadId: id, index, total: chunks, base64: bytesToBase64(piece) }],
        });
      } catch (error) {
        throw new Error(`文件分块传入 SOLO-QA 页面失败（第 ${index + 1}/${chunks} 块）：${error instanceof Error ? error.message : String(error)}`);
      }
      const entry = injected?.[0];
      const result = entry?.result;
      if (result?.ok !== true || result.received !== index + 1 || !entry.documentId
          || (documentId && entry.documentId !== documentId)) {
        const detail = result?.reason || "页面没有返回有效分块确认";
        throw new Error(`文件分块传入 SOLO-QA 页面失败（第 ${index + 1}/${chunks} 块）：${detail}`);
      }
      // A reload must restart the whole file, never continue in a new document.
      documentId = entry.documentId;
    }
    uploaded = await requestInSoloQaPage(path, { method: "POST" }, {
      kind: "staged-file", id, chunks, size: blob.size,
      filename: safeFilename, contentType, uploadKind,
    }, tab, documentId);
    return { ...uploaded, name: safeFilename, size: uploaded?.size ?? blob.size };
  } finally {
    try {
      await chrome.scripting.executeScript({
        target: soloInjectionTarget(tab, documentId), world: "ISOLATED",
        func: (uploadId) => { if (globalThis.__soloQaGsbUploadChunks) delete globalThis.__soloQaGsbUploadChunks[uploadId]; },
        args: [id],
      });
    } catch { /* page may have navigated while an upload failed */ }
  }
}

function jsonOptions(body) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function normalizeLabel(value) {
  return String(value || "")
    .replace(/[＊*：:]/g, "")
    .replace(/\s+/g, "")
    .toLowerCase();
}

function optionsForField(field) {
  const candidates = [field.options, field.choices, field.validation?.options];
  return candidates.find(Array.isArray) || [];
}

function normalizeChoice(field, value) {
  const options = optionsForField(field);
  if (!options.length) return value;
  const wanted = normalizeLabel(value);
  const match = options.find((option) => {
    const optionValue = typeof option === "object" ? option.value : option;
    const optionLabel = typeof option === "object" ? option.label : option;
    return normalizeLabel(optionValue) === wanted || normalizeLabel(optionLabel) === wanted;
  });
  if (!match) return value;
  return typeof match === "object" ? match.value : match;
}

function attachmentField(field) {
  return field.field_type === "attachment" || field.field_type === "file";
}

function gsbUploadKind(field) {
  const text = normalizeLabel(`${field.field_key || ""}${field.label || ""}`);
  if (/(轨迹|trace|trajectory)/i.test(text)) return "trajectory";
  if (field.field_type === "video" || /(录屏|video|recording|screencast)/i.test(text)) return "recording";
  return "";
}

function gsbUploadField(field) {
  return attachmentField(field) || field.field_type === "video"
    || /^(?:a|b)_(?:trace_file|screencast)$/.test(String(field.field_key || ""));
}

function gsbModelField(field) {
  return /(模型名称|model.?name)/i.test(`${field.field_key || ""}${field.label || ""}`);
}

function buildRemoteData(schema, bundle, uploaded) {
  const valuesByLabel = new Map(
    Object.entries(bundle.values || {}).map(([label, value]) => [normalizeLabel(label), value]),
  );
  const result = {};
  const missing = [];
  for (const field of schema.fields || []) {
    if (field.is_enabled === false) continue;
    const key = String(field.field_key || "");
    if (!key) continue;
    if (attachmentField(field)) {
      result[key] = [uploaded];
      continue;
    }
    const fallbackLabel = FIELD_KEY_LABELS[key] || "";
    const labels = [field.label, fallbackLabel].filter(Boolean).map(normalizeLabel);
    const matchedLabel = labels.find((label) => valuesByLabel.has(label));
    if (matchedLabel) {
      result[key] = normalizeChoice(field, valuesByLabel.get(matchedLabel));
    } else if (field.is_required) {
      missing.push(field.label || key);
    }
  }
  if (missing.length) {
    throw new Error(`SOLO-QA 新增了无法映射的必填项：${missing.join("、")}`);
  }
  return result;
}

function gsbSide(field) {
  const text = normalizeLabel(`${field.field_key || ""}${field.label || ""}`);
  if (/^(?:x_)?a(?:_|-|模型名称)|^第一次跑/.test(text)) return "A";
  if (/^(?:x_)?b(?:_|-|模型名称)|^第二次跑/.test(text)) return "B";
  return "";
}

function buildGsbRemoteData(schema, bundle, uploaded) {
  const values = bundle.values || {};
  const valuesByLabel = new Map(
    Object.entries(values).map(([label, value]) => [normalizeLabel(label), value]),
  );
  const result = {};
  const missing = [];
  for (const field of schema.fields || []) {
    if (field.is_enabled === false) continue;
    const key = String(field.field_key || "");
    if (!key) continue;
    if (gsbModelField(field)) {
      const side = gsbSide(field);
      // Older local servers omit models; their Pair runs are fixed to this model.
      const model = String(bundle.models?.[side] || (side ? "auto_model/urm" : "")).trim();
      const options = optionsForField(field);
      const accepted = options.find((option) => {
        const value = typeof option === "object" ? option.value : option;
        const label = typeof option === "object" ? option.label : option;
        return normalizeLabel(value) === normalizeLabel(model)
          || normalizeLabel(label) === normalizeLabel(model);
      });
      if (!model || (options.length && !accepted)) {
        if (field.is_required) missing.push(`${field.label || key}${model ? `（本地模型 ${model} 不在远端选项中）` : ""}`);
      } else {
        result[key] = accepted && typeof accepted === "object" ? accepted.value : model;
      }
      continue;
    }
    if (gsbUploadField(field)) {
      const side = gsbSide(field);
      const kind = gsbUploadKind(field);
      const file = side && kind ? uploaded?.[side]?.[kind] : null;
      if (kind === "recording" && file?.url) result[key] = file.url;
      else if (kind === "trajectory" && file) result[key] = [file];
      else if (field.is_required) missing.push(field.label || key);
      continue;
    }
    const fallbackLabel = GSB_FIELD_KEY_LABELS[key] || "";
    const labels = [key, GSB_FIELD_KEY_ALIASES[key], field.label, fallbackLabel]
      .filter(Boolean).map(normalizeLabel);
    const matchedLabel = labels.find((label) => valuesByLabel.has(label));
    if (matchedLabel) {
      result[key] = normalizeChoice(field, valuesByLabel.get(matchedLabel));
    } else if (field.is_required) {
      missing.push(field.label || key);
    }
  }
  if (missing.length) {
    throw new Error(`SOLO-QA GSB 新增了无法映射的必填项：${missing.join("、")}`);
  }
  return result;
}

async function sha256Hex(blob) {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function loadLocalBundle(turnKey) {
  if (!TURN_KEY_RE.test(turnKey)) throw new Error("本地轮次标识不正确");
  const [runId, turnNumber] = turnKey.split(":");
  return localJson(`/turns/${runId}/${turnNumber}/payload`);
}

async function recordLocal(bundle, values) {
  return localJson("/state", jsonOptions({
    turn_key: bundle.key,
    payload_sha256: bundle.payload_sha256,
    ...values,
  }));
}

function remoteQcText(item) {
  const parts = [];
  const seen = new Set();
  const relevantKey = /(qc|quality|reason|evidence|detail|summary|message|reject|check|查重|质检|打回|证据)/i;
  const add = (value) => {
    const text = String(value || "").trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    parts.push(text);
  };
  add(item?.qc_summary);
  add(item?.message);
  const visit = (value, relevant = false, depth = 0) => {
    if (depth > 5 || value == null) return;
    if (typeof value === "string") {
      if (relevant) add(value);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach((entry) => visit(entry, relevant, depth + 1));
      return;
    }
    if (typeof value !== "object") return;
    Object.entries(value).forEach(([key, entry]) => {
      visit(entry, relevant || relevantKey.test(key), depth + 1);
    });
  };
  visit(item);
  return parts.join("\n").slice(0, 4000);
}

function compactRemote(item) {
  return {
    id: item.id,
    status: String(item.status || "SUBMITTED").slice(0, 64),
    session_id: String(item.session_id || "").slice(0, 128),
    turn_id: String(item.turn_id || "").slice(0, 128),
    round_no: item.round_no || 0,
    qc_summary: remoteQcText(item),
    submitted_at: String(item.submitted_at || "").slice(0, 128),
    updated_at: String(item.updated_at || item.qc_finished_at || "").slice(0, 128),
  };
}

async function remoteDetails(items) {
  const details = [];
  const queue = [...items];
  const workers = Array.from({ length: Math.min(5, queue.length) }, async () => {
    while (queue.length) {
      const item = queue.shift();
      try {
        details.push(compactRemote(await remoteJson(`/submissions/${encodeURIComponent(item.id)}`)));
      } catch (error) {
        if (item.session_id && item.turn_id) details.push(compactRemote(item));
        else throw error;
      }
    }
  });
  await Promise.all(workers);
  return details;
}

async function listAllRemote() {
  const items = [];
  const pageSize = 20;
  let remoteTotal = 0;
  for (let page = 1; page <= 25; page += 1) {
    const response = await remoteJson(`/submissions?page=${page}&page_size=${pageSize}`);
    const pageItems = Array.isArray(response.items) ? response.items : [];
    items.push(...pageItems);
    remoteTotal = Number(response.meta?.total ?? items.length);
    if (!pageItems.length || items.length >= remoteTotal || items.length >= 500) break;
  }
  return {
    items: await remoteDetails(items.slice(0, 500)),
    total: remoteTotal,
    complete: items.length >= remoteTotal,
  };
}

async function syncAllRemote() {
  const remote = await listAllRemote();
  const local = await localJson("/sync", jsonOptions({
    items: remote.items,
    complete: remote.complete,
  }));
  return {
    ...local,
    remote_total: remote.total,
    partial: !remote.complete,
  };
}

async function findRemoteMatch(bundle) {
  const turnId = String(bundle.values?.["TurnID/PromptID"] || "");
  const sessionId = String(bundle.values?.SessionID || "");
  const roundNo = Number(bundle.values?.["当前对话轮次排序"] || 0);
  if (!turnId || !sessionId) return null;
  const response = await remoteJson(
    `/submissions?page=1&page_size=20&keyword=${encodeURIComponent(turnId)}`,
  );
  const items = Array.isArray(response.items) ? response.items : [];
  const listed = items.map(compactRemote);
  const listedMatch = listed.find(
    (item) => item.session_id === sessionId && item.turn_id === turnId && Number(item.round_no) === roundNo,
  );
  if (listedMatch) return listedMatch;
  const unresolved = items.filter((item) => !item.session_id || !item.turn_id);
  if (!unresolved.length) return null;
  const details = await remoteDetails(unresolved);
  return details.find(
    (item) => item.session_id === sessionId && item.turn_id === turnId && Number(item.round_no) === roundNo,
  ) || null;
}

async function uploadTrajectory(bundle, schema) {
  const limitMb = Number(schema.attachment_max_mb || 20);
  if (Number(bundle.trajectory.size) > limitMb * 1024 * 1024) {
    throw new Error(`轨迹文件超过 SOLO-QA 当前 ${limitMb} MB 上限`);
  }
  const traceResponse = await fetch(bundle.trajectory.url, { cache: "no-store" });
  if (!traceResponse.ok) {
    let detail = "";
    try { detail = errorMessage(await traceResponse.json(), ""); } catch { detail = ""; }
    throw new Error(detail || `读取本地轨迹失败 (${traceResponse.status})`);
  }
  const blob = await traceResponse.blob();
  const digest = await sha256Hex(blob);
  if (digest !== bundle.trajectory.sha256) throw new Error("读取到的轨迹文件摘要与本地记录不一致");
  return remoteFile("/submissions/upload", blob, bundle.trajectory.name || "trajectory.jsonl");
}

async function submitOne(turnKey, loadFormSchema) {
  const bundle = await loadLocalBundle(turnKey);
  if (bundle.solo_qa?.remote_id && !["failed", "remote_missing", "not_submitted"].includes(bundle.solo_qa.state)) {
    return { turn_key: turnKey, outcome: "skipped", reason: "本地已记录为提交过" };
  }
  const existing = await findRemoteMatch(bundle);
  if (existing) {
    await recordLocal(bundle, {
      state: REMOTE_STATUS_TO_LOCAL[existing.status] || "qc_pending",
      remote_id: String(existing.id),
      remote_status: existing.status,
      qc_summary: existing.qc_summary,
      submitted_at: existing.submitted_at,
      remote_updated_at: existing.updated_at,
      payload_sha256: "",
      error: "",
    });
    return { turn_key: turnKey, outcome: "recovered", remote_id: String(existing.id) };
  }
  await recordLocal(bundle, { state: "submitting", error: "" });
  try {
    const schema = await loadFormSchema();
    buildRemoteData(schema, bundle, { name: "pending", path: "pending", size: 0 });
    const uploaded = await uploadTrajectory(bundle, schema);
    const data = buildRemoteData(schema, bundle, uploaded);
    const created = await remoteJson("/submissions", jsonOptions({
      data,
      schema_fingerprint: schema.fingerprint || "",
    }));
    const remoteId = String(created.id || "");
    if (!remoteId) throw new Error("SOLO-QA 已响应，但没有返回提交 ID");
    const detail = compactRemote({ ...created, status: created.status || "SUBMITTED" });
    await recordLocal(bundle, {
      state: REMOTE_STATUS_TO_LOCAL[detail.status] || "qc_pending",
      remote_id: remoteId,
      remote_status: detail.status || "SUBMITTED",
      qc_summary: detail.qc_summary || created.message || "",
      submitted_at: detail.submitted_at,
      remote_updated_at: detail.updated_at,
      error: "",
    });
    return { turn_key: turnKey, outcome: "submitted", remote_id: remoteId, status: detail.status };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    let recovered = null;
    try { recovered = await findRemoteMatch(bundle); } catch { recovered = null; }
    if (recovered) {
      await recordLocal(bundle, {
        state: REMOTE_STATUS_TO_LOCAL[recovered.status] || "qc_pending",
        remote_id: String(recovered.id),
        remote_status: recovered.status,
        qc_summary: recovered.qc_summary,
        submitted_at: recovered.submitted_at,
        remote_updated_at: recovered.updated_at,
        error: "",
      });
      return { turn_key: turnKey, outcome: "recovered", remote_id: String(recovered.id) };
    }
    await recordLocal(bundle, { state: "failed", error: message });
    throw new Error(message);
  }
}

function orderTurnKeysByConversation(keys) {
  const groups = new Map();
  for (const key of keys) {
    const [runId, turnNumber] = key.split(":");
    if (!groups.has(runId)) groups.set(runId, []);
    groups.get(runId).push({ key, turnNumber: Number(turnNumber) });
  }
  return [...groups.values()].flatMap((group) =>
    group.sort((first, second) => first.turnNumber - second.turnNumber)
      .map((item) => item.key)
  );
}

async function submitBatch(payload) {
  const selectedKeys = Array.isArray(payload?.turn_keys)
    ? [...new Set(payload.turn_keys.map(String))]
    : [];
  if (!selectedKeys.length) throw new Error("请至少选择一个轮次");
  if (selectedKeys.length > 100 || selectedKeys.some((key) => !TURN_KEY_RE.test(key))) {
    throw new Error("提交轮次列表格式不正确");
  }
  const keys = orderTurnKeysByConversation(selectedKeys);
  const results = [];
  let schemaPromise = null;
  const loadFormSchema = () => {
    if (!schemaPromise) schemaPromise = remoteJson("/submissions/form-schema");
    return schemaPromise;
  };
  for (let index = 0; index < keys.length; index += 1) {
    const key = keys[index];
    try {
      results.push(await submitOne(key, loadFormSchema));
    } catch (error) {
      results.push({
        turn_key: key,
        outcome: "failed",
        error: error instanceof Error ? error.message : String(error),
      });
      return { results, stopped: true, remaining: keys.length - index - 1 };
    }
  }
  return { results, stopped: false, remaining: 0 };
}

function pairLocalJson(path, options = {}) {
  return requestJson(`${LOCAL_ORIGIN}/api/pairwise/solo${path}`, options);
}

async function loadLocalPairBundle(pairId) {
  if (!PAIR_ID_RE.test(pairId)) throw new Error("本地 Pair 标识不正确");
  return pairLocalJson(`/${pairId}/payload`);
}

async function recordPairLocal(bundle, values) {
  return pairLocalJson("/state", jsonOptions({
    pair_id: bundle.id,
    payload_sha256: bundle.payload_sha256,
    ...values,
  }));
}

function compactGsbRemote(item) {
  return {
    id: item.id,
    status: String(item.status || "SUBMITTED").slice(0, 64),
    a_session_id: String(item.a_session_id || "").slice(0, 128),
    b_session_id: String(item.b_session_id || "").slice(0, 128),
    gsb_verdict: String(item.gsb_verdict || "").slice(0, 32),
    qc_summary: remoteQcText(item),
    submitted_at: String(item.submitted_at || "").slice(0, 128),
    updated_at: String(item.updated_at || item.qc_finished_at || "").slice(0, 128),
  };
}

async function listAllRemoteGsb() {
  const items = [];
  const pageSize = 20;
  let remoteTotal = 0;
  for (let page = 1; page <= 25; page += 1) {
    const response = await remoteJson(`/gsb/submissions?page=${page}&page_size=${pageSize}`);
    const pageItems = Array.isArray(response.items) ? response.items : [];
    items.push(...pageItems.map(compactGsbRemote));
    remoteTotal = Number(response.meta?.total ?? items.length);
    if (!pageItems.length || items.length >= remoteTotal || items.length >= 500) break;
  }
  return { items: items.slice(0, 500), total: remoteTotal, complete: items.length >= remoteTotal };
}

async function syncAllRemoteGsb() {
  const remote = await listAllRemoteGsb();
  const local = await pairLocalJson("/sync", jsonOptions({
    items: remote.items,
    complete: remote.complete,
  }));
  return { ...local, remote_total: remote.total, partial: !remote.complete };
}

async function findRemoteGsbMatch(bundle) {
  const aSession = String(bundle.values?.a_session_id || bundle.values?.["A-SessionID"] || "");
  const bSession = String(bundle.values?.b_session_id || bundle.values?.["B-SessionID"] || "");
  if (!aSession || !bSession) return null;
  const response = await remoteJson(
    `/gsb/submissions?page=1&page_size=20&keyword=${encodeURIComponent(aSession)}`,
  );
  return (Array.isArray(response.items) ? response.items : [])
    .map(compactGsbRemote)
    .find((item) => item.a_session_id === aSession && item.b_session_id === bSession) || null;
}

async function uploadGsbEvidence(bundle, side, kind, schema) {
  const isRecording = kind === "recording";
  const evidence = isRecording ? bundle.recordings?.[side] : bundle.trajectories?.[side];
  const label = isRecording ? "录屏" : "轨迹";
  if (!evidence) throw new Error(`${side} 的本地${label}资料不存在`);
  const limitMb = Number(isRecording
    ? (schema.video_max_mb || schema.recording_max_mb || 500)
    : (schema.attachment_max_mb || 27));
  if (Number(evidence.size) > limitMb * 1024 * 1024) {
    throw new Error(`${side} ${label}文件超过 SOLO-QA 当前 ${limitMb} MB 上限`);
  }
  const response = await fetch(evidence.url, { cache: "no-store" });
  if (!response.ok) throw new Error(`读取本地 ${side} ${label}失败 (${response.status})`);
  const blob = await response.blob();
  if (await sha256Hex(blob) !== evidence.sha256) {
    throw new Error(`${side} ${label}文件摘要与本地记录不一致`);
  }
  const upload = () => remoteFile(
    "/submissions/upload", blob,
    evidence.name || (isRecording ? `${side}.m4v` : `${side}.jsonl`),
    isRecording ? "video" : "",
  );
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      return await upload();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const retryable = /failed to fetch|连接失败|网络错误|文件分块|分块大小|页面没有返回有效结果|no document|frame.*removed/i.test(message);
      if (!retryable || attempt === 3) {
        throw new Error(`${side} ${label}上传失败${attempt > 1 ? "（已自动重试）" : ""}：${message}`);
      }
      await new Promise((resolve) => setTimeout(resolve, 800));
    }
  }
}

async function submitOneGsb(pairId, loadFormSchema) {
  const bundle = await loadLocalPairBundle(pairId);
  if (bundle.solo_qa?.remote_id && !["failed", "remote_missing", "not_submitted"].includes(bundle.solo_qa.state)) {
    return { pair_id: pairId, outcome: "skipped", reason: "本地已记录为提交过" };
  }
  const existing = await findRemoteGsbMatch(bundle);
  if (existing) {
    await recordPairLocal(bundle, {
      state: REMOTE_STATUS_TO_LOCAL[existing.status] || "qc_pending",
      remote_id: String(existing.id), remote_status: existing.status,
      qc_summary: existing.qc_summary, submitted_at: existing.submitted_at,
      remote_updated_at: existing.updated_at, error: "",
    });
    return { pair_id: pairId, outcome: "recovered", remote_id: String(existing.id) };
  }
  await recordPairLocal(bundle, { state: "submitting", error: "" });
  try {
    const schema = await loadFormSchema();
    const pendingTrajectory = { name: "pending.jsonl", path: "pending", size: 0 };
    const pendingRecording = { name: "pending.m4v", url: "https://pending.invalid/video.m4v", size: 0 };
    const pendingUploads = {
      A: { trajectory: pendingTrajectory, recording: pendingRecording },
      B: { trajectory: pendingTrajectory, recording: pendingRecording },
    };
    buildGsbRemoteData(schema, bundle, pendingUploads);
    const uploaded = {
      A: {
        trajectory: await uploadGsbEvidence(bundle, "A", "trajectory", schema),
        recording: await uploadGsbEvidence(bundle, "A", "recording", schema),
      },
      B: {
        trajectory: await uploadGsbEvidence(bundle, "B", "trajectory", schema),
        recording: await uploadGsbEvidence(bundle, "B", "recording", schema),
      },
    };
    const data = buildGsbRemoteData(schema, bundle, uploaded);
    let created;
    try {
      created = await remoteJson("/gsb/submissions", jsonOptions({
        data,
        schema_fingerprint: schema.fingerprint || "",
      }));
    } catch (error) {
      throw new Error(`创建 GSB 记录失败：${error instanceof Error ? error.message : String(error)}`);
    }
    const remoteId = String(created.id || "");
    if (!remoteId) throw new Error("SOLO-QA 已响应，但没有返回 GSB 提交 ID");
    const detail = compactGsbRemote({ ...created, status: created.status || "SUBMITTED" });
    await recordPairLocal(bundle, {
      state: REMOTE_STATUS_TO_LOCAL[detail.status] || "qc_pending",
      remote_id: remoteId, remote_status: detail.status,
      qc_summary: detail.qc_summary || created.message || "",
      submitted_at: detail.submitted_at, remote_updated_at: detail.updated_at, error: "",
    });
    return { pair_id: pairId, outcome: "submitted", remote_id: remoteId, status: detail.status };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    let recovered = null;
    try { recovered = await findRemoteGsbMatch(bundle); } catch { recovered = null; }
    if (recovered) {
      await recordPairLocal(bundle, {
        state: REMOTE_STATUS_TO_LOCAL[recovered.status] || "qc_pending",
        remote_id: String(recovered.id), remote_status: recovered.status,
        qc_summary: recovered.qc_summary, submitted_at: recovered.submitted_at,
        remote_updated_at: recovered.updated_at, error: "",
      });
      return { pair_id: pairId, outcome: "recovered", remote_id: String(recovered.id) };
    }
    await recordPairLocal(bundle, { state: "failed", error: message });
    throw new Error(message);
  }
}

async function submitGsbBatch(payload) {
  const pairIds = Array.isArray(payload?.pair_ids)
    ? [...new Set(payload.pair_ids.map(String))]
    : [];
  if (!pairIds.length) throw new Error("请至少选择一个 Pair");
  if (pairIds.length > 100 || pairIds.some((id) => !PAIR_ID_RE.test(id))) {
    throw new Error("提交 Pair 列表格式不正确");
  }
  const results = [];
  let schemaPromise = null;
  const loadFormSchema = () => {
    if (!schemaPromise) schemaPromise = remoteJson("/gsb/form-schema");
    return schemaPromise;
  };
  for (let index = 0; index < pairIds.length; index += 1) {
    const pairId = pairIds[index];
    try {
      results.push(await submitOneGsb(pairId, loadFormSchema));
    } catch (error) {
      results.push({ pair_id: pairId, outcome: "failed",
        error: error instanceof Error ? error.message : String(error) });
      return { results, stopped: true, remaining: pairIds.length - index - 1 };
    }
  }
  return { results, stopped: false, remaining: 0 };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  let senderOrigin = "";
  try { senderOrigin = new URL(sender.url || "").origin; } catch { senderOrigin = ""; }
  if (senderOrigin !== LOCAL_ORIGIN) {
    sendResponse({ ok: false, error: "只接受本地评测台发起的请求" });
    return false;
  }
  const action = message?.type === "SOLO_QA_SYNC"
    ? syncAllRemote
    : message?.type === "SOLO_QA_SUBMIT"
      ? () => submitBatch(message.payload || {})
      : message?.type === "SOLO_GSB_SYNC"
        ? syncAllRemoteGsb
        : message?.type === "SOLO_GSB_SUBMIT"
          ? () => submitGsbBatch(message.payload || {})
          : null;
  if (!action) {
    sendResponse({ ok: false, error: "未知的提交助手操作" });
    return false;
  }
  action()
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) => sendResponse({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    }));
  return true;
});
