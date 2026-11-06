import assert from "node:assert/strict";
import React from "react";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { createServer } from "vite";

let server;
let PatientRecordsPage;

before(async () => {
  server = await createServer({
    configFile: fileURLToPath(new URL("../../../vite.config.js", import.meta.url)),
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  ({ default: PatientRecordsPage } = await server.ssrLoadModule(
    "/src/modules/patient/MedicalRecordsSection.jsx",
  ));
});

after(async () => {
  await server?.close();
});

function renderRecordsPage(historyState, { path = "/dashboard/patient/records", permissions } = {}) {
  const context = {
    user: {
      permissions: permissions ?? [
        "view_own_record",
        "submit_record_correction",
        "view_own_correction_requests",
        "request_ai_assistance",
      ],
    },
    session: { access_token: "test-token" },
    recordState: {
      status: "ready",
      record: { age: 45, medical_condition: "Test condition", treatment: "Test treatment" },
      error: "",
    },
    historyState,
    retryRecord() {},
    refreshHistory() {},
  };
  function PatientOutlet() {
    return React.createElement(Outlet, { context });
  }
  const tree = React.createElement(
    MemoryRouter,
    { initialEntries: [path] },
    React.createElement(
      Routes,
      null,
      React.createElement(
        Route,
        { path: "/dashboard/patient", element: React.createElement(PatientOutlet) },
        React.createElement(Route, {
          path: "records",
          element: React.createElement(PatientRecordsPage),
        }),
      ),
    ),
  );
  return renderToStaticMarkup(tree);
}

test("Medical Records shows request ID, submission date, physician status, and distinct guardrail details", () => {
  const html = renderRecordsPage({
    status: "ready",
    error: "",
    items: [
      {
        request_id: 77,
        created_at: "2026-10-30T10:00:00Z",
        guardrail_decision: "Allow",
        approval_status: "Pending",
        reviewed_at: null,
        status_issue: null,
      },
    ],
  });

  assert.match(html, /My Correction Requests/);
  assert.match(html, /#77/);
  assert.match(html, /dateTime="2026-10-30T10:00:00Z"/);
  assert.match(html, /Current status/);
  assert.match(html, /ws-badge-pending">Pending review/);
  assert.match(html, /Guardrail decision<\/dt><dd><span class="ws-badge ws-badge-allow">Allowed/);
  assert.match(html, /Physician review<\/dt><dd><span class="ws-badge ws-badge-pending">Pending review/);
  assert.match(html, /<summary class="ws-link-button">View details<\/summary>/);
  assert.doesNotMatch(html, /Proposed treatment/);
});

test("My Correction Requests renders loading, empty, and retryable error states", () => {
  assert.match(
    renderRecordsPage({ status: "loading", items: [], error: "" }),
    /Loading your correction requests securely/,
  );
  assert.match(
    renderRecordsPage({ status: "ready", items: [], error: "" }),
    /submitted any correction requests yet/,
  );
  const errorHtml = renderRecordsPage({
    status: "error",
    items: [],
    error: "Correction request history is temporarily unavailable.",
  });
  assert.match(errorHtml, /role="alert"/);
  assert.match(errorHtml, /Correction request history is temporarily unavailable/);
  assert.match(errorHtml, /Reload requests/);
});

test("classic correction form is hidden during normal assistant operation and shown only as fallback", () => {
  const readyHistory = { status: "ready", items: [], error: "" };
  const normalHtml = renderRecordsPage(readyHistory);
  assert.match(normalHtml, /Request a Correction/);
  assert.doesNotMatch(normalHtml, /Temporary fallback|Proposed treatment/);

  const unavailableHtml = renderRecordsPage(readyHistory, {
    path: "/dashboard/patient/records?correctionFallback=1",
  });
  assert.match(unavailableHtml, /AI-assisted correction process is temporarily unavailable/);
  assert.match(unavailableHtml, /Proposed treatment/);

  const noAssistantAccessHtml = renderRecordsPage(readyHistory, {
    permissions: ["view_own_record", "submit_record_correction"],
  });
  assert.match(noAssistantAccessHtml, /AI-assisted correction process is unavailable for your account/);
  assert.match(noAssistantAccessHtml, /Proposed treatment/);
});
