import assert from "node:assert/strict";
import test from "node:test";

import { TaskApiError, isFeatureDisabled, isUnconfirmed, taskRequest } from "./taskApi.js";

function fakeFetch(status, body, { text } = {}) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    return { ok: status >= 200 && status < 300, status, text: async () => text ?? (body === undefined ? "" : JSON.stringify(body)) };
  };
  impl.calls = calls;
  return impl;
}

test("sends the bearer token and JSON body, returns parsed data", async () => {
  const fetchImpl = fakeFetch(200, { ok: true });
  const data = await taskRequest("/technician/tasks/1/decision", "tok", { method: "POST", body: { decision: "ALLOW" }, fetchImpl });
  assert.deepEqual(data, { ok: true });
  const { url, init } = fetchImpl.calls[0];
  assert.match(url, /\/technician\/tasks\/1\/decision$/);
  assert.equal(init.headers.Authorization, "Bearer tok");
  assert.equal(init.headers["Content-Type"], "application/json");
  assert.equal(init.body, JSON.stringify({ decision: "ALLOW" }));
});

test("GET sends no body or content type", async () => {
  const fetchImpl = fakeFetch(200, []);
  await taskRequest("/technician/tasks", "tok", { fetchImpl });
  assert.equal(fetchImpl.calls[0].init.body, undefined);
  assert.equal(fetchImpl.calls[0].init.headers["Content-Type"], undefined);
});

test("errors carry the safe detail, status and machine code", async () => {
  const fetchImpl = fakeFetch(409, { detail: "Physician approval is required.", code: "approval_required" });
  await assert.rejects(taskRequest("/x", "t", { method: "POST", body: {}, fetchImpl }), (error) => {
    assert.ok(error instanceof TaskApiError);
    assert.deepEqual([error.message, error.status, error.code, error.kind], ["Physician approval is required.", 409, "approval_required", "http"]);
    return true;
  });
});

test("feature-disabled and unconfirmed outcomes are recognisable", async () => {
  const disabled = await taskRequest("/x", "t", { fetchImpl: fakeFetch(503, { detail: "This feature is not available yet.", code: "feature_disabled" }) }).catch((e) => e);
  assert.equal(isFeatureDisabled(disabled), true);
  assert.equal(isUnconfirmed(disabled), false);
  const unconfirmed = await taskRequest("/x", "t", { fetchImpl: fakeFetch(504, { detail: "Check first.", code: "unconfirmed" }) }).catch((e) => e);
  assert.equal(isUnconfirmed(unconfirmed), true);
  assert.equal(isFeatureDisabled(new Error("x")), false);
});

test("validation errors (detail is a list) show a generic message, not raw JSON", async () => {
  const error = await taskRequest("/x", "t", { fetchImpl: fakeFetch(422, { detail: [{ loc: ["body"], msg: "bad" }] }) }).catch((e) => e);
  assert.equal(error.message, "Please check the details you entered.");
});

test("empty 500 means the backend is not running; bad success bodies are rejected", async () => {
  const down = await taskRequest("/x", "t", { fetchImpl: fakeFetch(500, undefined) }).catch((e) => e);
  assert.equal(down.kind, "unreachable");
  const invalid = await taskRequest("/x", "t", { fetchImpl: fakeFetch(200, undefined, { text: "not json" }) }).catch((e) => e);
  assert.equal(invalid.kind, "invalid");
});

test("network failures and timeouts are classified", async () => {
  const network = await taskRequest("/x", "t", { fetchImpl: async () => { throw new Error("offline"); } }).catch((e) => e);
  assert.equal(network.kind, "network");
  const slow = await taskRequest("/x", "t", {
    timeoutMs: 5,
    fetchImpl: (url, init) => new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(new Error("aborted")))),
  }).catch((e) => e);
  assert.equal(slow.kind, "timeout");
});
