import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { after, before, describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const frontendRoot = fileURLToPath(new URL("../..", import.meta.url));
const supabaseStub = fileURLToPath(new URL("./testing/supabaseStub.js", import.meta.url));

let server;
let renderRoute;
let DecisionForm;
let AppointmentDecisionForm;
let AppointmentSummary;
let CorrectionCoordination;
let CorrectionRequestsPanel;

before(async () => {
  server = await createServer({
    root: frontendRoot,
    configFile: false,
    // Deterministic: never read the developer's real .env or VITE_* variables. Feature flags are OFF here;
    // tests that need a flag ON build the navigation with buildWorkspacePages({...}).
    envDir: false,
    envPrefix: "HASF_TEST_NO_SUCH_PREFIX_",
    logLevel: "error",
    appType: "custom",
    server: { middlewareMode: true, hmr: false, watch: null },
    optimizeDeps: { noDiscovery: true, include: [] },
    esbuild: { jsx: "automatic" },
    resolve: {
      alias: [{ find: /^(\.\.\/)+shared\/supabase\.js$/, replacement: supabaseStub }],
    },
  });
  ({ renderRoute } = await server.ssrLoadModule("/src/app/testing/renderRoute.jsx"));
  DecisionForm = (await server.ssrLoadModule("/src/modules/technician/DecisionForm.jsx")).default;
  AppointmentDecisionForm = (await server.ssrLoadModule("/src/modules/technician/AppointmentDecisionForm.jsx")).default;
  AppointmentSummary = (await server.ssrLoadModule("/src/modules/technician/AppointmentSummary.jsx")).default;
  CorrectionCoordination = (await server.ssrLoadModule("/src/modules/technician/CorrectionCoordination.jsx")).default;
  CorrectionRequestsPanel = (await server.ssrLoadModule("/src/modules/patient/RequestHistorySection.jsx")).CorrectionRequestsPanel;
});

after(async () => {
  await server?.close();
});

const make = (role, name, permissions) => ({
  status: "ready",
  session: { access_token: "test-token", user: { id: `${role}-auth-id` } },
  user: { user_id: 6, email: `${role}@example.test`, role, permissions, name },
  adminOverview: role === "admin" ? { users: [], permissions: [], ai_requests: [], audit_logs: [], errors: {} } : null,
  error: "",
});
const technicianAuth = make("technician", "Test Technician", ["view_assigned_patient", "view_assigned_task", "update_task_status"]);
const patientAuth = make("patient", "Test Patient", ["view_own_record", "submit_service_request", "view_own_service_requests"]);
const adminAuth = make("admin", null, ["manage_users"]);
const physicianAuth = make("physician", "Dr Test", ["review_escalation"]);
const signedOut = { status: "signed-out", session: null, user: null, adminOverview: null, error: "" };

const pageTitle = (html) => html.match(/<h1 id="ws-page-title"[^>]*>(.*?)<\/h1>/)?.[1] ?? null;
const navLinks = (html) => [...html.matchAll(/class="ws-nav-link[^"]*" href="([^"]+)"/g)].map((m) => m[1]);

const technicianPages = {
  "/dashboard/technician/overview": "Welcome, Test.",
  "/dashboard/technician/patients": "Assigned Patients",
  "/dashboard/technician/tasks": "Tasks",
};

describe("Technician portal", () => {
  for (const [path, title] of Object.entries(technicianPages)) {
    test(`renders ${path} in the Technician theme`, () => {
      const html = renderRoute(path, technicianAuth);
      assert.equal(pageTitle(html), title);
      assert.match(html, /ws-theme-technician/);
      assert.match(html, /aria-label="Technician pages"/);
    });
  }

  test("the sidebar is the Technician navigation, with Tasks hidden while the feature flag is off", () => {
    const html = renderRoute("/dashboard/technician/overview", technicianAuth);
    assert.deepEqual(navLinks(html), ["/dashboard/technician/overview", "/dashboard/technician/patients"]);
  });

  test("the overview shows loading states and never invents numbers before data arrives", () => {
    const html = renderRoute("/dashboard/technician/overview", technicianAuth);
    assert.match(html, /Assigned patients/);
    assert.match(html, /Awaiting physician/);
    assert.match(html, /Loading securely/);
    assert.doesNotMatch(html, /ws-error/);
  });

  test("the tasks page shows a loading state and the empty detail prompt", () => {
    const html = renderRoute("/dashboard/technician/tasks", technicianAuth);
    assert.match(html, /Loading securely/);
    assert.match(html, /Select a request to review it/);
    assert.match(html, /role="tablist"/);
    assert.match(html, /Needs review/);
  });

  test("the patients page states that the summary is read-only", () => {
    const html = renderRoute("/dashboard/technician/patients", technicianAuth);
    assert.match(html, /read-only/i);
    assert.doesNotMatch(html, /<form/);
  });

  test("the bare technician URL and unknown pages redirect (rendering nothing during server rendering)", () => {
    // <Navigate> only acts in an effect, which does not run in static rendering; the existing
    // Admin and Patient routing tests rely on the same behaviour.
    assert.equal(renderRoute("/dashboard/technician", technicianAuth).includes("ws-page-title"), false);
    assert.equal(renderRoute("/dashboard/technician/nope", technicianAuth).includes("ws-page-title"), false);
  });
});

describe("role protection around the new pages", () => {
  const protectedPaths = Object.keys(technicianPages);

  test("other roles never see Technician content or theme", () => {
    for (const auth of [patientAuth, adminAuth]) {
      for (const path of protectedPaths) {
        const html = renderRoute(path, auth);
        assert.doesNotMatch(html, /ws-theme-technician|Assigned Patients|Care operations|CARE OPERATIONS|Your queue/i, `${auth.user.role} ${path}`);
      }
    }
  });

  test("a Physician is also kept out of the Technician area", () => {
    for (const path of protectedPaths) {
      assert.doesNotMatch(renderRoute(path, physicianAuth), /ws-theme-technician/, path);
    }
  });

  test("a Technician opening Patient or Admin pages sees none of their content", () => {
    for (const path of ["/dashboard/patient/records", "/dashboard/patient/service-requests", "/dashboard/admin/users", "/dashboard/admin/overview"]) {
      const html = renderRoute(path, technicianAuth);
      assert.doesNotMatch(html, /ws-theme-patient|ws-theme-admin|Record details|Invite user|Ask for help|Permission matrix/, path);
    }
  });

  test("signed-out visitors see nothing", () => {
    for (const path of [...protectedPaths, "/dashboard/patient/service-requests"]) {
      assert.equal(renderRoute(path, signedOut), "", path);
    }
  });

  test("a loading session shows the secure loading screen, not technician content", () => {
    const html = renderRoute("/dashboard/technician/tasks", { status: "loading", user: null, session: null });
    assert.match(html, /Restoring your secure session/);
    assert.doesNotMatch(html, /Your queue/);
  });
});

describe("Patient Service Requests page", () => {
  test("renders inside the existing Patient portal when opened directly", () => {
    const html = renderRoute("/dashboard/patient/requests", patientAuth);
    assert.equal(pageTitle(html), "Requests");
    assert.match(html, /ws-theme-patient/);
    assert.match(html, /Corrections you raise with the AI Assistant appear here/);
    assert.doesNotMatch(html, /Guardrail decision<\/th>/);
  });

  test("the existing Patient navigation is unchanged while the flag is off", () => {
    const html = renderRoute("/dashboard/patient/overview", patientAuth);
    assert.deepEqual(navLinks(html), [
      "/dashboard/patient/overview",
      "/dashboard/patient/records",
      "/dashboard/patient/assistant",
      "/dashboard/patient/requests",
      "/dashboard/patient/profile",
    ]);
  });

  test("the chatbot, records and history pages still render exactly as before", () => {
    assert.match(renderRoute("/dashboard/patient/assistant", patientAuth), /Message the assistant/);
    assert.match(renderRoute("/dashboard/patient/requests", patientAuth), /Record correction requests/);
    assert.match(renderRoute("/dashboard/patient/records", patientAuth), /Record details|My Medical Records/);
  });
});

describe("responsive layout", () => {
  test("the new stylesheet collapses its two-column layouts on narrow screens", () => {
    const css = readFileSync(new URL("../modules/technician/technician.css", import.meta.url), "utf8");
    assert.match(css, /@media \(max-width: 960px\)[\s\S]*\.tc-layout/);
    assert.match(css, /@media \(max-width: 960px\)[\s\S]*\.sr-grid/);
  });
});

describe("Technician decision form", () => {
  const render = (task) => renderToStaticMarkup(createElement(DecisionForm, { task, token: "t", onDone: async () => {} }));
  const task = (overrides) => ({ task_id: 1, status: "In Review", requires_physician_approval: false, approval_status: "none", ...overrides });
  const radios = (html) => [...html.matchAll(/type="radio"[^>]*value="(\w+)"/g)].map((m) => m[1]).sort();

  test("an in-review task offers ALLOW, BLOCK and HOLD", () => {
    assert.deepEqual(radios(render(task({}))), ["ALLOW", "BLOCK", "HOLD"]);
  });

  test("statuses that allow no decision render no form at all", () => {
    for (const status of ["Submitted", "In Progress", "Completed", "Declined", "Cancelled"]) {
      assert.equal(render(task({ status })), "", status);
    }
  });

  test("while a physician approval is pending the Technician cannot decline it", () => {
    assert.deepEqual(radios(render(task({ status: "Awaiting Physician", requires_physician_approval: true, approval_status: "pending" }))), ["ALLOW"]);
  });

  test("an on-hold task can only be declined", () => {
    assert.deepEqual(radios(render(task({ status: "On Hold" }))), ["BLOCK"]);
  });

  test("both note fields are labelled, explain who can read them, and no decision is preselected", () => {
    const html = render(task({}));
    assert.match(html, /<label[^>]*>Internal case note \(optional\)<\/label>/);
    assert.match(html, /<label[^>]*>Message to the patient \(optional\)<\/label>/);
    assert.match(html, /Visible only to you and the reviewing physician\. Patients never see this\./);
    assert.match(html, /Visible to the patient/);
    assert.match(html, /Internal · patients never see this/);
    assert.match(html, /tc-choices tc-choices-3/);
    assert.doesNotMatch(html, /checked/);
    assert.match(html, /<button[^>]*disabled=""[^>]*type="submit"|<button[^>]*type="submit"[^>]*disabled=""/);
  });

  test("the form never contains an approve or reject control", () => {
    const html = render(task({ status: "Awaiting Physician", requires_physician_approval: true, approval_status: "pending" }));
    assert.doesNotMatch(html, /Approve|Reject/);
  });
});

describe("Patient Appointments page", () => {
  test("renders in the Patient theme with its heading, the booking panel and a link to Requests", () => {
    const html = renderRoute("/dashboard/patient/appointments", patientAuth);
    assert.equal(pageTitle(html), "Appointments");
    assert.match(html, /ws-theme-patient/);
    assert.match(html, /Request an appointment/);
    assert.match(html, /href="\/dashboard\/patient\/requests"/);
    assert.match(html, /not a booking until a Technician confirms/i);
  });

  test("never shows hardcoded doctors or time slots before the API answers", () => {
    const html = renderRoute("/dashboard/patient/appointments", patientAuth);
    assert.doesNotMatch(html, /Dr\.? /);
    assert.doesNotMatch(html, /\d{1,2}:\d{2}\s?(AM|PM)/i);
    assert.doesNotMatch(html, /type="radio"/);
  });

  test("the old history and service-request addresses redirect to Requests", () => {
    const src = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
    for (const old of ["history", "service-requests"]) {
      assert.ok(src.includes(`<Navigate replace to="/dashboard/patient/requests" />} path="${old}" />`), old);
    }
  });

  test("other roles cannot open the Patient Appointments page", () => {
    for (const auth of [technicianAuth, adminAuth, physicianAuth, signedOut]) {
      const html = renderRoute("/dashboard/patient/appointments", auth);
      assert.notEqual(pageTitle(html), "Appointments");
    }
  });
});

describe("Technician appointment decision UI", () => {
  const appointment = {
    appointment_id: 1, physician_id: 4, physician_name: "Dr. Fictional", starts_at: "2030-06-03T14:00:00Z",
    ends_at: "2030-06-03T14:30:00Z", timezone: "America/New_York", local_label: "Mon, 03 Jun 2030, 10:00 AM EDT",
    reason: "Annual check-up", status: "Pending Confirmation", status_label: "Pending Technician Confirmation",
    patient_response: "It is my yearly visit.",
  };
  const task = (over = {}) => ({
    task_id: 20, status: "In Review", approval_status: "none", requires_physician_approval: false,
    category: "appointment_scheduling", title: "Appointment", created_at: "2026-01-01T00:00:00Z",
    patient: { patient_id: 101, name: "Fictional Patient" }, appointment, events: [], ...over,
  });
  const form = (t) => renderToStaticMarkup(createElement(AppointmentDecisionForm, { task: t, token: "t", onDone: async () => {} }));

  test("the summary shows patient, doctor, preferred time, status, reason and the patient's reply", () => {
    const html = renderToStaticMarkup(createElement(AppointmentSummary, { task: task() }));
    for (const text of ["Fictional Patient", "Dr. Fictional", "Mon, 03 Jun 2030, 10:00 AM EDT", "America/New_York",
      "Pending Technician Confirmation", "Annual check-up", "It is my yearly visit."]) {
      assert.ok(html.includes(text), text);
    }
  });

  test("the summary renders nothing for ordinary tasks", () => {
    assert.equal(renderToStaticMarkup(createElement(AppointmentSummary, { task: task({ appointment: null }) })), "");
  });

  test("an in-review request offers the four context-specific actions, not ALLOW / BLOCK / HOLD", () => {
    const html = form(task());
    for (const label of ["Confirm Appointment", "Request More Information", "Reject Request", "Refer to Physician"]) {
      assert.ok(html.includes(label), label);
    }
    assert.doesNotMatch(html, /<strong>(ALLOW|BLOCK|HOLD)<\/strong>/);
    assert.doesNotMatch(html, /Keep this request pending until something else happens/);
    assert.match(html, /Decide on this request/);
  });

  test("other statuses offer only what the backend accepts", () => {
    const onHold = form(task({ status: "On Hold" }));
    assert.ok(onHold.includes("Reject Request"));
    assert.ok(!onHold.includes("Confirm Appointment"));
    assert.equal(form(task({ status: "Completed" })), "");
    assert.equal(form(task({ status: "Submitted" })), "");
  });

  test("the availability check is shown while loading and never claims availability up front", () => {
    const html = form(task());
    assert.match(html, /Checking the doctor/);
    assert.match(html, /Re-check availability/);
    assert.doesNotMatch(html, /doctor is available at this time/i);
  });
});

describe("Technician correction coordination form", () => {
  const REASONS = [{ code: "duplicate_request", label: "Duplicate of a request already on file" }];
  const render = (status, approval = "not_requested", actions = ["request_physician_authorization", "request_information", "decline"]) =>
    renderToStaticMarkup(createElement(CorrectionCoordination, {
      task: { task_id: 30, category: "record_correction", status, correction: { request_id: 500, approval_status: approval, reviewed_at: null, allowed_actions: actions, decline_reasons: actions.includes("decline") ? REASONS : [] } },
      token: "t", onDone: async () => {},
    }));
  const radios = (html) => [...html.matchAll(/type="radio"[^>]*value="(\w+)"/g)].map((m) => m[1]);

  test("offers HOLD and DENY only, never ALLOW/approve/reject, and shows the Physician review as not sent yet", () => {
    const html = render("In Review");
    assert.deepEqual(radios(html), ["request_physician_authorization", "request_information", "decline"]);
    assert.match(html, /HOLD · Request Physician authorization/);
    assert.match(html, /HOLD · Request more information/);
    assert.match(html, /DENY · Decline \(administrative reason\)/);
    assert.match(html, /Not sent to the Physician yet/);
    assert.doesNotMatch(html, /ALLOW|Approve this|Reject this|value="close"/);
  });

  test("after authorization only a question is possible; deny and close are not offered", () => {
    const html = render("Awaiting Physician", "pending", ["request_information"]);
    assert.deepEqual(radios(html), ["request_information"]);
    assert.doesNotMatch(html, /DENY|Decline/);
  });

  test("close is offered only once the Physician has decided, and the decision is shown read-only", () => {
    const html = render("Awaiting Physician", "approved", ["close"]);
    assert.deepEqual(radios(html), ["close"]);
    assert.match(html, /Close coordination task/);
    assert.match(html, /Approved by physician/);
    assert.match(html, /Only the Physician can approve or reject a correction/);
  });

  test("with nothing to do it explains why instead of rendering a form", () => {
    assert.match(render("On Hold", "not_requested", []), /Waiting for the patient/);
    assert.doesNotMatch(render("On Hold", "not_requested", []), /<form/);
    assert.match(render("Submitted", "not_requested", []), /Start the review first/);
  });

  test("no correction text or Physician identity is part of what the form renders", () => {
    assert.doesNotMatch(render("In Review"), /request_details|proposed|reviewer/i);
  });
});

describe("Patient Requests: correction coordination", () => {
  const item = (coordination) => ({ request_id: 5, created_at: "2026-10-30T10:00:00Z", reviewed_at: null, guardrail_decision: "Escalate", approval_status: "Pending", status_issue: null, coordination });
  const render = (items, token = "t") => renderToStaticMarkup(createElement(CorrectionRequestsPanel, { historyState: { status: "ready", items }, refreshHistory: () => {}, token }));

  test("shows the care-team status and the real Physician review status, without guardrail internals", () => {
    const html = render([item({ task_id: 30, status: "Awaiting Physician", status_label: "With your Physician for review.", patient_message: null, can_add_info: false })]);
    assert.match(html, /#5/);
    assert.match(html, /With your Physician for review\./);
    assert.match(html, /Pending/);
    assert.doesNotMatch(html, /Guardrail decision|<textarea/);
  });

  test("a question from the care team shows a short reply box with a privacy reminder", () => {
    const html = render([item({ task_id: 30, status: "On Hold", status_label: "Your care team needs more information from you", patient_message: "Which date did this change?", can_add_info: true })]);
    assert.match(html, /Which date did this change\?/);
    assert.match(html, /<textarea/);
    assert.match(html, /do not include medical details/i);
    assert.match(html, /maxLength="500"/);
  });

  test("corrections without a coordination task still render", () => {
    const html = render([item(null)]);
    assert.match(html, /#5/);
    assert.doesNotMatch(html, /<textarea/);
  });
});
