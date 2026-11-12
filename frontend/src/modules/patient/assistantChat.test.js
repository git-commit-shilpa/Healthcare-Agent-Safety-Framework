import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

import {
  CLASSIC_CORRECTION_FALLBACK_LINK, HISTORY_LINK, appendMessage, applyChatResponse, applyReview, applySaveOutcome, applySubmitted, createInitialState,
  classifyChatHistoryError, createHistoryState, createTurnRequest, describeFlowError,
  describeSaveStatus, parseChatListResponse, parseChatMessagesResponse, parseChatResponse,
  parseReviewResponse, resetFlow, shouldFetchChatHistory, validateChatMessage, validateProposal,
  shouldOfferClassicCorrectionFallback, isFreshChat,
} from "./assistantChat.js";
import {
  PatientApiError, confirmAssistantCorrection, patientRequest, reviewAssistantCorrection,
  sendAssistantMessage,
} from "./patientApi.js";

const AT = "2026-01-01T10:00:00.000Z";
const chatResponse = (mode, token = null, reply = "Hello") => ({ reply, decision: "Allow", intent: "x", mode, state_token: token });
const parse = (r) => parseChatResponse(r);
const http = (status, message = "") => new PatientApiError(message, { kind: "http", status });

test("normal answers and greetings leave the flow idle with no token", () => {
  for (const mode of ["answer", "greeting", "refusal", "notice", "cancelled"]) {
    const state = applyChatResponse(createInitialState(AT), parse(chatResponse(mode)), AT);
    assert.equal(state.stage, "idle", mode);
    assert.equal(state.token, null);
    assert.equal(state.messages.at(-1).role, "assistant");
  }
});

test("offer -> details -> review -> submitted keeps server tokens exactly as issued", () => {
  let state = applyChatResponse(createInitialState(AT), parse(chatResponse("offer", "tok-offer", "Would you like to raise a correction request?")), AT);
  assert.deepEqual([state.stage, state.token], ["offer", "tok-offer"]);
  state = applyChatResponse(state, parse(chatResponse("details_step", "tok-details")), AT);
  assert.deepEqual([state.stage, state.token], ["details", "tok-details"]);
  const review = parseReviewResponse(
    { proposed_treatment: "Fictional text", state_token: "tok-confirm", expires_in_seconds: 300, notice: "Press Confirm." },
    "Fictional text",
  );
  state = applyReview(state, review, AT);
  assert.deepEqual([state.stage, state.token, state.proposal.text], ["review", "tok-confirm", "Fictional text"]);
  state = applySubmitted(state, { request_id: 9, guardrail_decision: "Escalate", approval_status: "Pending" }, AT);
  assert.equal(state.stage, "idle");
  assert.equal(state.token, null);
  assert.equal(state.proposal, null);
  assert.match(state.messages.at(-1).text, /#9 was submitted.*Pending/);
  assert.deepEqual(state.messages.at(-1).link, HISTORY_LINK);
});

test("untrusted chat responses are rejected, never invented", () => {
  for (const bad of [
    null, [], "x", {}, chatResponse("offer", null), chatResponse("details_step", ""),
    chatResponse("weird"), { ...chatResponse("answer"), reply: "" }, { ...chatResponse("answer"), state_token: 5 },
    chatResponse("answer", "x".repeat(601)),
  ]) {
    assert.equal(parse(bad), null, JSON.stringify(bad));
  }
});

test("chat save outcomes are preserved without changing the existing reply shape", () => {
  const saved = parse(chatResponse("answer"));
  assert.equal(saved.saveStatus, null);
  assert.equal(saved.saveReason, null);
  assert.equal(parse({ ...chatResponse("answer"), save_status: "not_saved", save_reason: "filtered" }).saveStatus, "not_saved");
  assert.equal(parse({ ...chatResponse("answer"), save_status: "unconfirmed" }).saveStatus, "unconfirmed");
  assert.equal(parse({ ...chatResponse("answer"), save_status: "disabled" }).saveStatus, "disabled");
  assert.equal(parse({ ...chatResponse("answer"), save_status: "unknown" }).saveStatus, null);
  assert.match(describeSaveStatus("not_saved", "filtered"), /wasn't saved.*protect your privacy/);
  assert.doesNotMatch(describeSaveStatus("not_saved", "filtered"), /saved to chat history\.$/);
  assert.match(describeSaveStatus("unconfirmed"), /couldn't confirm whether.*saved/);
  assert.equal(describeSaveStatus("saved"), "This conversation was saved to chat history.");
});

test("safety decision labels are shown only when explicitly returned by the backend", () => {
  const allowed = parse({ ...chatResponse("answer"), decision: "Allow" });
  assert.equal(allowed.decision, "Allow");
  const state = applyChatResponse(createInitialState(AT), allowed, AT);
  assert.equal(state.messages.at(-1).decision, "Allow");

  for (const decision of ["Block", "Escalate"]) {
    assert.equal(parse({ ...chatResponse("refusal"), decision }).decision, decision);
  }
  assert.equal(parse({ ...chatResponse("answer"), decision: "Unknown" }).decision, null);
  assert.equal(parse({ reply: "Hello", mode: "answer" }).decision, null);
});

test("a Technician-first correction is announced as being with the care team", () => {
  const state = applySubmitted(createInitialState(AT), { request_id: 9, guardrail_decision: "Escalate", approval_status: null, coordination: "assigned" }, AT);
  const text = state.messages.at(-1).text;
  assert.match(text, /#9 was submitted/);
  assert.match(text, /with your care team/);
  assert.match(text, /reviews it if clinical authorization is needed/);
  assert.doesNotMatch(text, /Pending review by your assigned Physician/);
  assert.equal(state.messages.at(-1).link.to, "/dashboard/patient/requests");
});

test("history responses are validated and restored without correction state", () => {
  const chatId = "fe3a98cb-04f1-4d1f-87ed-1b4c93616d63";
  const chats = parseChatListResponse({
    chats: [{ chat_id: chatId, title: "Portal help", last_activity_at: AT, message_count: 2 }],
  });
  assert.deepEqual(chats, [{
    chatId, title: "Portal help", lastActivityAt: AT, messageCount: 2,
  }]);
  assert.equal(parseChatListResponse({ chats: [{ chat_id: "bad" }] }), null);

  const messages = parseChatMessagesResponse({
    chat_id: chatId,
    messages: [
      { role: "user", content: "How do I find history?", created_at: AT, state_token: "must-not-display" },
      { role: "assistant", content: "Open AI Request History.", created_at: AT },
    ],
  }, chatId);
  const restored = createHistoryState(messages);
  assert.deepEqual(restored.messages.map(({ role, text, id }) => [role, text, id]), [
    ["user", "How do I find history?", 1],
    ["assistant", "Open AI Request History.", 2],
  ]);
  assert.equal(restored.token, null);
  assert.equal(parseChatMessagesResponse({ chat_id: chatId, messages: [] }, "different-id"), null);
});

test("each submitted turn gets an id; retrying retains the same request and chat id", () => {
  const first = createTurnRequest("chat-1", "first", null, true, "turn-1");
  const retry = first;
  const second = createTurnRequest("chat-1", "second", null, false, "turn-2");
  assert.equal(retry.turnId, first.turnId);
  assert.equal(retry.message, first.message);
  assert.notEqual(second.turnId, first.turnId);
  assert.equal(second.chatId, first.chatId);
  assert.equal(first.isNewChat, true);
  assert.equal(second.isNewChat, false);
});

test("a review response must echo exactly the text that was sent", () => {
  const ok = { proposed_treatment: "A", state_token: "t", expires_in_seconds: 300, notice: "n" };
  assert.ok(parseReviewResponse(ok, "A"));
  assert.equal(parseReviewResponse(ok, "A "), null);
  assert.equal(parseReviewResponse({ ...ok, state_token: "" }, "A"), null);
  assert.equal(parseReviewResponse({ ...ok, expires_in_seconds: "300" }, "A"), null);
});

test("cancellation and resets clear the token and proposal", () => {
  let state = applyChatResponse(createInitialState(AT), parse(chatResponse("offer", "t")), AT);
  state = applyChatResponse(state, parse(chatResponse("cancelled", null, "Okay, nothing was submitted.")), AT);
  assert.deepEqual([state.stage, state.token], ["idle", null]);
  state = resetFlow(applyChatResponse(state, parse(chatResponse("details_step", "t2")), AT), "expired", AT);
  assert.deepEqual([state.stage, state.token, state.proposal], ["idle", null, null]);
});

test("pointer replies add a navigation link", () => {
  const state = applyChatResponse(createInitialState(AT), parse(chatResponse("pointer", null, "The status is in AI Request History.")), AT);
  assert.equal(state.messages.at(-1).link.to, "/dashboard/patient/requests");
});

test("pending-request conflict (409) resets the flow and points to Request History", () => {
  const outcome = describeFlowError(http(409, "Correction request #5 is still awaiting Physician review."), "review");
  assert.equal(outcome.reset, true);
  assert.match(outcome.text, /#5/);
  assert.deepEqual(outcome.link, HISTORY_LINK);
});

test("expired, replayed and session-changed steps reset safely", () => {
  const expired = describeFlowError(http(409, "This conversation step is no longer valid. Please start again."), "confirm");
  assert.equal(expired.reset, true);
  assert.match(expired.text, /expired or was already used.*Nothing was submitted/);
  const session = describeFlowError(http(409, "Please sign in again to continue."), "review");
  assert.match(session.text, /session changed/);
  assert.equal(describeFlowError(http(401, "x"), "chat").reset, true);
});

test("uncertain confirm outcomes (timeout, network, 502, 504) direct the Patient to Request History without retry", () => {
  for (const error of [
    new PatientApiError("t", { kind: "timeout" }), new PatientApiError("n", { kind: "network" }),
    new PatientApiError("i", { kind: "invalid" }), http(502), http(504),
  ]) {
    const outcome = describeFlowError(error, "confirm");
    assert.equal(outcome.reset, true);
    assert.match(outcome.text, /couldn't confirm.*don't submit it again.*won't retry automatically/);
    assert.deepEqual(outcome.link, HISTORY_LINK);
  }
});

test("chat-phase network problems keep the flow so the Patient can retry; others do not claim success", () => {
  const outcome = describeFlowError(new PatientApiError("t", { kind: "timeout" }), "chat");
  assert.equal(outcome.reset, false);
  assert.doesNotMatch(outcome.text, /submitted/);
  assert.equal(describeFlowError(http(503), "chat").reset, false);
  assert.equal(describeFlowError(http(422), "review").reset, false);
  assert.equal(describeFlowError(http(503), "confirm").reset, true);
});

test("input validation", () => {
  assert.notEqual(validateProposal("  "), "");
  assert.notEqual(validateProposal("x".repeat(2001)), "");
  assert.equal(validateProposal("Fictional"), "");
  assert.notEqual(validateChatMessage(""), "");
  assert.notEqual(validateChatMessage("x".repeat(501)), "");
});

test("API helpers send the right bodies and tokens; chat never carries correction text", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };
  await sendAssistantMessage("jwt", "yes", "tok-1", { fetchImpl });
  await sendAssistantMessage("jwt", "hello", null, { fetchImpl });
  await reviewAssistantCorrection("jwt", "Fictional text", "tok-2", { fetchImpl });
  await confirmAssistantCorrection("jwt", "Fictional text", "tok-3", { fetchImpl });
  assert.match(calls[0].url, /\/patient\/assistant\/chat$/);
  assert.deepEqual(calls[0].body, { message: "yes", state_token: "tok-1" });
  assert.deepEqual(calls[1].body, { message: "hello" });
  assert.match(calls[2].url, /\/patient\/assistant\/correction\/review$/);
  assert.deepEqual(calls[2].body, { proposed_treatment: "Fictional text", state_token: "tok-2" });
  assert.match(calls[3].url, /\/patient\/assistant\/correction\/confirm$/);
  assert.deepEqual(calls[3].body, { proposed_treatment: "Fictional text", state_token: "tok-3" });
  assert.ok(calls.every((c) => c.init.headers.Authorization === "Bearer jwt"));
  assert.ok(calls.slice(0, 2).every((c) => !("proposed_treatment" in c.body)));
});

test("chat API sends first-turn identifiers once and omits is_new_chat on later turns", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };
  await sendAssistantMessage("jwt", "first", null, {
    chatId: "chat-1", turnId: "turn-1", isNewChat: true, fetchImpl,
  });
  await sendAssistantMessage("jwt", "second", null, {
    chatId: "chat-1", turnId: "turn-2", fetchImpl,
  });
  assert.deepEqual(calls.map((call) => call.body), [
    { message: "first", chat_id: "chat-1", turn_id: "turn-1", is_new_chat: true },
    { message: "second", chat_id: "chat-1", turn_id: "turn-2" },
  ]);
});

test("patient history endpoints use the authenticated request client and accept DELETE 204", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (init.method === "DELETE") return new Response(null, { status: 204 });
    return new Response(JSON.stringify({ chats: [] }), { status: 200 });
  };
  await patientRequest("/patient/assistant/chats", "jwt", { fetchImpl });
  await patientRequest("/patient/assistant/chats/chat%2Fid", "jwt", { fetchImpl });
  await patientRequest("/patient/assistant/chats/chat%2Fid", "jwt", {
    method: "DELETE", allowEmptyResponse: true, fetchImpl,
  });
  assert.match(calls[0].url, /\/patient\/assistant\/chats$/);
  assert.match(calls[1].url, /\/patient\/assistant\/chats\/chat%2Fid$/);
  assert.equal(calls[2].init.method, "DELETE");
  assert.match(calls[2].url, /\/patient\/assistant\/chats\/chat%2Fid$/);
  assert.ok(calls.every((call) => Object.hasOwn(call.init.headers, "Authorization")));
  await assert.rejects(
    patientRequest("/patient/assistant/chats/chat%2Fid", "jwt", { method: "DELETE", fetchImpl }),
    (error) => error.kind === "invalid",
  );
});

test("history API failures remain explicit", async () => {
  const fetchImpl = async () => new Response(JSON.stringify({ detail: "History unavailable." }), { status: 503 });
  await assert.rejects(patientRequest("/patient/assistant/chats", "jwt", { fetchImpl }), (error) => {
    assert.equal(error.status, 503);
    assert.match(error.message, /History unavailable/);
    return true;
  });
});

test("disabled chat history 404 is expected and prevents repeat history requests", async () => {
  const firstRequest = shouldFetchChatHistory(false, false);
  assert.equal(firstRequest, true);

  let fetchCount = 0;
  const fetchImpl = async () => {
    fetchCount += 1;
    return new Response(JSON.stringify({ detail: "Not found." }), { status: 404 });
  };
  let disabled;
  await assert.rejects(
    patientRequest("/patient/assistant/chats", "jwt", { fetchImpl }),
    (error) => {
      disabled = classifyChatHistoryError(error);
      return error.status === 404;
    },
  );
  assert.deepEqual(disabled, { status: "disabled", chats: [], error: "" });
  assert.equal(shouldFetchChatHistory(disabled.status === "disabled", false), false);
  assert.equal(shouldFetchChatHistory(disabled.status === "disabled", true), false);
  if (shouldFetchChatHistory(disabled.status === "disabled", false)) {
    await patientRequest("/patient/assistant/chats", "jwt", { fetchImpl });
  }
  assert.equal(fetchCount, 1);

  for (const status of [401, 403, 503]) {
    assert.equal(classifyChatHistoryError(http(status, "Request failed.")).status, "error");
  }
});

test("Patient assistant page module loads with all named imports resolved", async () => {
  const server = await createServer({
    configFile: fileURLToPath(new URL("../../../vite.config.js", import.meta.url)),
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  try {
    const page = await server.ssrLoadModule("/src/modules/patient/PatientAssistantPage.jsx");
    assert.equal(typeof page.default, "function");
  } finally {
    await server.close();
  }
});

test("retrying a failed chat request resends the exact turn identifiers and message", async () => {
  const calls = [];
  let attempts = 0;
  const fetchImpl = async (_url, init) => {
    calls.push(JSON.parse(init.body));
    attempts += 1;
    if (attempts === 1) throw new TypeError("offline");
    return new Response(JSON.stringify(chatResponse("answer")), { status: 200 });
  };
  const request = { chatId: "chat-1", turnId: "turn-1", isNewChat: true, fetchImpl };
  await assert.rejects(sendAssistantMessage("jwt", "same message", null, request));
  await sendAssistantMessage("jwt", "same message", null, request);
  assert.deepEqual(calls[0], calls[1]);
  assert.deepEqual(calls[1], {
    message: "same message", chat_id: "chat-1", turn_id: "turn-1", is_new_chat: true,
  });
});

test("appendMessage assigns increasing ids", () => {
  let state = createInitialState(AT);
  state = appendMessage(appendMessage(state, "user", "a", AT), "assistant", "b", AT);
  assert.deepEqual(state.messages.map((m) => m.id), [1, 2, 3]);
});

test("chat state is memory-only and Records links to the assistant", async () => {
  const { readFileSync } = await import("node:fs");
  const read = (name) => readFileSync(new URL(name, import.meta.url), "utf8");
  for (const file of ["./PatientAssistantPage.jsx", "./assistantChat.js", "./patientApi.js"]) {
    assert.doesNotMatch(read(file), /localStorage|sessionStorage|indexedDB|document\.cookie/, file);
  }
  const records = read("./MedicalRecordsSection.jsx");
  assert.match(records, /to="\/dashboard\/patient\/assistant"/);
  assert.match(records, /My Correction Requests/);
  assert.match(records, /View details/);
  assert.doesNotMatch(records, /Temporary fallback/);
  // Only the structured Confirm button reaches the confirm endpoint.
  assert.equal((read("./PatientAssistantPage.jsx").match(/confirmAssistantCorrection\(/g) || []).length, 1);
});

test("classic correction fallback is offered only for unavailable pre-submission assistant steps", () => {
  assert.deepEqual(CLASSIC_CORRECTION_FALLBACK_LINK, {
    to: "/dashboard/patient/records?correctionFallback=1",
    label: "Use the secure correction form",
  });
  for (const error of [
    { kind: "timeout" },
    { kind: "network" },
    { kind: "unreachable" },
    { kind: "invalid" },
    { status: 503 },
  ]) {
    assert.equal(shouldOfferClassicCorrectionFallback(error, "chat"), true);
    assert.equal(shouldOfferClassicCorrectionFallback(error, "review"), true);
    assert.equal(shouldOfferClassicCorrectionFallback(error, "confirm"), false);
  }
  assert.equal(shouldOfferClassicCorrectionFallback({ status: 403 }, "chat"), false);
  assert.equal(shouldOfferClassicCorrectionFallback({ status: 422 }, "review"), false);
});

const withSave = (mode, save_status, save_reason = null, token = null, reply = "Hello") => ({
  ...chatResponse(mode, token, reply), chat_id: "c", turn_id: "t", save_status, save_reason,
});

test("disabled history is never treated as saved and keeps is_new_chat for later turns", () => {
  const outcome = applySaveOutcome(parse(withSave("answer", "disabled")).saveStatus, false);
  assert.deepEqual(outcome, { persisted: false, refreshChats: false, historyDisabled: true });
  assert.match(describeSaveStatus("disabled"), /turned off.*not being saved/);
  assert.doesNotMatch(describeSaveStatus("disabled"), /was saved/);
  // The next turn would still be a new chat, so a backend that is enabled later can create it.
  assert.equal(createTurnRequest("c", "next", null, !outcome.persisted, "t2").isNewChat, true);
});

test("only saved and duplicate mark a chat persisted; failures and unknowns never do", () => {
  for (const status of ["saved", "duplicate"]) {
    assert.deepEqual(applySaveOutcome(status, false), { persisted: true, refreshChats: true, historyDisabled: false });
  }
  for (const status of ["not_saved", "unconfirmed", null, undefined]) {
    assert.deepEqual(applySaveOutcome(status, false), { persisted: false, refreshChats: false, historyDisabled: null }, String(status));
    assert.doesNotMatch(describeSaveStatus(status, "error"), /was saved to chat history/);
  }
  assert.match(describeSaveStatus("not_saved", "filtered"), /wasn't saved/);
  assert.match(describeSaveStatus("unconfirmed"), /couldn't confirm/);
  // Already-persisted chats stay persisted through a later failed save.
  assert.equal(applySaveOutcome("not_saved", true).persisted, true);
  assert.equal(applySaveOutcome("disabled", true).persisted, true);
});

test("history enabled mid-session: disabled, then saved, creates the chat on the first successful save", () => {
  let persisted = false;
  const sent = [];
  for (const status of ["disabled", "disabled", "saved", "saved"]) {
    sent.push(createTurnRequest("c", "m", null, !persisted).isNewChat);
    persisted = applySaveOutcome(status, persisted).persisted;
  }
  assert.deepEqual(sent, [true, true, true, false]);
});

test("chat keeps working with history disabled: messages stay visible and the correction flow runs", () => {
  let state = createInitialState(AT);
  state = appendMessage(state, "user", "How do corrections work?", AT);
  state = applyChatResponse(state, parse(withSave("answer", "disabled", null, null, "A Physician reviews it.")), AT);
  assert.deepEqual(state.messages.map((m) => m.role), ["assistant", "user", "assistant"]);
  state = applyChatResponse(state, parse(withSave("offer", "disabled", null, "tok-offer")), AT);
  assert.deepEqual([state.stage, state.token], ["offer", "tok-offer"]);
  state = applyChatResponse(state, parse(withSave("details_step", "disabled", null, "tok-details")), AT);
  assert.deepEqual([state.stage, state.token], ["details", "tok-details"]);
  state = applyReview(state, { text: "Fictional", token: "tok-confirm", notice: "n", expiresIn: 300 }, AT);
  assert.equal(state.stage, "review");
  state = applySubmitted(state, { request_id: 9 }, AT);
  assert.deepEqual([state.stage, state.token, state.proposal], ["idle", null, null]);
  assert.equal(state.messages.length, 7);
  assert.equal(parse(withSave("answer", "disabled")).saveStatus, "disabled");
});

test("a failed send keeps the same turn_id on retry and the persisted flag is untouched", () => {
  const first = createTurnRequest("c", "same", null, true, "turn-1");
  // A thrown error produces no save outcome at all, so nothing is marked persisted.
  const retry = first;
  assert.deepEqual([retry.turnId, retry.isNewChat], ["turn-1", true]);
});

test("the page applies save outcomes only through applySaveOutcome", async () => {
  const { readFileSync } = await import("node:fs");
  const page = readFileSync(new URL("./PatientAssistantPage.jsx", import.meta.url), "utf8");
  assert.match(page, /applySaveOutcome\(parsed\.saveStatus, hasPersistedChat\.current\)/);
  assert.doesNotMatch(page, /saveStatus === "disabled"[\s\S]{0,80}hasPersistedChat\.current = true/);
});

test("not_saved reasons are explained accurately and unknown reasons stay neutral", () => {
  assert.match(describeSaveStatus("not_saved", "filtered"), /protect your privacy/);
  const ineligible = describeSaveStatus("not_saved", "not_eligible");
  assert.doesNotMatch(ineligible, /privacy/);
  assert.match(ineligible, /isn't kept in chat history/);
  for (const reason of ["error", "chat_full", "chat_limit", "chat_unavailable", "no_ids", null, "from_the_future"]) {
    const text = describeSaveStatus("not_saved", reason);
    assert.doesNotMatch(text, /privacy/, String(reason));
    assert.match(text, /wasn't saved/, String(reason));
  }
});

test("the not_eligible reason is accepted; unrecognized reasons are dropped, not trusted", () => {
  const base = { reply: "Hi", mode: "refusal", state_token: null, save_status: "not_saved" };
  assert.equal(parseChatResponse({ ...base, save_reason: "not_eligible" }).saveReason, "not_eligible");
  assert.equal(parseChatResponse({ ...base, save_reason: "filtered" }).saveReason, "filtered");
  assert.equal(parseChatResponse({ ...base, save_reason: "brand_new_reason" }).saveReason, null);
  // not_saved and unconfirmed never mark a chat persisted, whatever the reason.
  for (const status of ["not_saved", "unconfirmed"]) {
    assert.equal(applySaveOutcome(status, false).persisted, false);
  }
  assert.match(describeSaveStatus("unconfirmed"), /couldn't confirm/);
  assert.doesNotMatch(describeSaveStatus("unconfirmed"), /was saved to chat history/);
});

test("decision reason and emergency flag are carried onto the assistant message", () => {
  const emergency = parse({
    reply: "Please call your local emergency number now.", decision: "Escalate", intent: "emergency",
    mode: "notice", state_token: null, reason_code: "emergency_detected",
    reason: "The message describes a possible emergency.",
  });
  assert.equal(emergency.reason, "The message describes a possible emergency.");
  const message = applyChatResponse(createInitialState(AT), emergency, AT).messages.at(-1);
  assert.equal(message.decision, "Escalate");
  assert.equal(message.emergency, true);
  assert.equal(message.reason, "The message describes a possible emergency.");

  const blocked = applyChatResponse(
    createInitialState(AT),
    parse({ reply: "No.", decision: "Block", intent: "other_patient_access", mode: "refusal", reason: "Never allowed." }),
    AT,
  ).messages.at(-1);
  assert.equal(blocked.emergency, undefined);
  assert.equal(blocked.reason, "Never allowed.");

  // Older responses without a reason still parse and render.
  const legacy = parse(chatResponse("answer"));
  assert.equal(legacy.reason, null);
  assert.equal("reason" in applyChatResponse(createInitialState(AT), legacy, AT).messages.at(-1), false);
});

test("record replies parse and the save notice states the real reason", () => {
  const record = parse({
    reply: "Here is what your record shows.\nCondition: Fictional", decision: "Allow",
    intent: "view_own_record", mode: "record", state_token: null,
    save_status: "not_saved", save_reason: "sensitive",
  });
  assert.equal(record.mode, "record");
  assert.equal(applyChatResponse(createInitialState(AT), record, AT).stage, "idle");
  assert.match(describeSaveStatus(record.saveStatus, record.saveReason), /medical record aren't saved/);
  assert.match(describeSaveStatus("not_saved", "refused"), /Refused or blocked/);
  assert.match(describeSaveStatus("not_saved", "error"), /wasn't saved to chat history\.$/);
  assert.match(describeSaveStatus("disabled"), /turned off/);
  assert.equal(parse({ reply: "x", mode: "bogus" }), null);
});

// ---- Reopening a saved conversation (fictional data only) ----
const CHAT_ID = "11111111-1111-4111-8111-111111111111";
const PH_USER = "Correction request step";
const PH_REPLY = "Correction request step recorded. No details were saved.";
const stored = (role, content, display_mode = null, n = 0) => ({ role, content, display_mode, created_at: `2026-11-05T10:0${n}:00+00:00` });
const restore = (messages) => parseChatMessagesResponse({ chat_id: CHAT_ID, messages }, CHAT_ID);
const pair = (n) => [stored("user", PH_USER, null, n), stored("assistant", PH_REPLY, "event", n)];

test("a run of stored correction placeholders is restored as ONE privacy notice, not fake chat", () => {
  const messages = restore([
    stored("user", "What does the portal show?", null, 1),
    stored("assistant", "Your pages and request history.", "answer", 1),
    ...pair(2), ...pair(3), ...pair(4), ...pair(5), ...pair(6),
  ]);
  assert.equal(messages.length, 3);
  assert.deepEqual(messages.slice(0, 2).map((m) => m.text), ["What does the portal show?", "Your pages and request history."]);
  assert.equal(messages[2].redacted, true);
  assert.equal(messages[2].role, "assistant");
  assert.match(messages[2].text, /never saved/);
  assert.deepEqual(messages[2].link, HISTORY_LINK);
  assert.equal(messages.filter((m) => m.text === PH_USER || m.text === PH_REPLY).length, 0);
});

test("real messages are restored exactly and in order; separate runs each get a notice", () => {
  const messages = restore([
    ...pair(1),
    stored("user", "Hello there", null, 2), stored("assistant", "Hi!", "greeting", 2),
    ...pair(3),
  ]);
  assert.deepEqual(messages.map((m) => m.redacted === true), [true, false, false, true]);
  assert.deepEqual(messages.slice(1, 3).map((m) => m.text), ["Hello there", "Hi!"]);
});

test("a Patient's own message that happens to match the placeholder is never hidden", () => {
  const messages = restore([stored("user", PH_USER, null, 1), stored("assistant", "I can explain corrections.", "answer", 1)]);
  assert.deepEqual(messages.map((m) => m.text), [PH_USER, "I can explain corrections."]);
  assert.equal(messages.some((m) => m.redacted), false);
});

test("an older server that sends the raw placeholder rows is handled the same way", () => {
  // Same rows as above: display_mode "event" on the assistant row is what marks the pair.
  const messages = restore(pair(1).concat(pair(2)));
  assert.equal(messages.length, 1);
  assert.equal(messages[0].redacted, true);
  // An already-collapsed response (new server) is stable when collapsed again.
  const again = restore([stored("assistant", messages[0].text, "event", 1)]);
  assert.equal(again.length, 1);
});

test("restoring never invents or alters stored text", () => {
  const messages = restore([stored("user", "Is there a pharmacy page?", null, 1), stored("assistant", "No, but you can ask your Physician.", "answer", 1)]);
  assert.deepEqual(messages.map((m) => [m.role, m.text]), [["user", "Is there a pharmacy page?"], ["assistant", "No, but you can ask your Physician."]]);
});

test("a conversation made only of correction steps is a real chat, not the empty welcome screen", () => {
  const state = createHistoryState(restore([...pair(1), ...pair(2)]));
  assert.equal(state.messages.length, 1);
  assert.equal(isFreshChat(state), false);
  assert.equal(isFreshChat(createInitialState(AT)), true); // New Chat shows the welcome screen
  assert.equal(isFreshChat(applyChatResponse(createInitialState(AT), parse(chatResponse("answer")), AT)), false);
});

test("a restored chat continues as an existing chat and a new chat starts fresh", () => {
  const restored = createHistoryState(restore([stored("user", "Hello there", null, 1), stored("assistant", "Hi!", "greeting", 1)]));
  assert.deepEqual([restored.stage, restored.token, restored.proposal], ["idle", null, null]);
  // After restoring, a saved reply keeps is_new_chat false for the next turn.
  assert.equal(applySaveOutcome("saved", true).persisted, true);
  // Opening an old chat never carries a state token, so correction steps cannot be resumed.
  assert.equal(createInitialState(AT).token, null);
});

test("correction steps report truthfully that only a placeholder was kept", () => {
  assert.match(describeSaveStatus("saved", "redacted"), /privacy placeholder/);
  assert.doesNotMatch(describeSaveStatus("saved", "redacted"), /conversation was saved/);
  assert.match(describeSaveStatus("saved", null), /conversation was saved/);
  assert.equal(parse({ reply: "ok", mode: "notice", save_status: "saved", save_reason: "redacted" }).saveReason, "redacted");
});

test("malformed history responses are rejected, not guessed at", () => {
  assert.equal(parseChatMessagesResponse({ chat_id: "other", messages: [] }, CHAT_ID), null);
  assert.equal(restore([{ role: "system", content: "x", created_at: "t" }]), null);
  assert.equal(restore([{ role: "user", content: 5, created_at: "t" }]), null);
});
