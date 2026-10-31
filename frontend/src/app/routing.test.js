import assert from "node:assert/strict";
import { after, before, describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import { createServer } from "vite";

const frontendRoot = fileURLToPath(new URL("../..", import.meta.url));
const supabaseStub = fileURLToPath(new URL("./testing/supabaseStub.js", import.meta.url));

let server;
let renderRoute;

before(async () => {
  server = await createServer({
    root: frontendRoot,
    configFile: false,
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
});

after(async () => {
  await server?.close();
});

const adminAuth = {
  status: "ready",
  session: { access_token: "test-token", user: { id: "admin-auth-id" } },
  user: {
    user_id: 1,
    email: "admin@example.test",
    role: "admin",
    permissions: ["manage_users", "monitor_ai_requests", "view_audit_logs"],
    name: null,
  },
  adminOverview: { users: [], permissions: [], ai_requests: [], audit_logs: [], errors: {} },
  error: "",
};

const patientAuth = {
  status: "ready",
  session: { access_token: "test-token", user: { id: "patient-auth-id" } },
  user: {
    user_id: 2,
    email: "patient@example.test",
    role: "patient",
    permissions: ["view_own_record", "submit_record_correction", "view_own_correction_requests"],
    name: "Test Patient",
  },
  adminOverview: null,
  error: "",
};

const signedOut = { status: "signed-out", session: null, user: null, adminOverview: null, error: "" };

function pageTitle(html) {
  return html.match(/<h1 id="ws-page-title"[^>]*>(.*?)<\/h1>/)?.[1] ?? null;
}

function activeNav(html) {
  return html.match(/<a aria-current="page" class="ws-nav-link is-active" href="([^"]+)"/)?.[1] ?? null;
}

const adminPages = {
  "/dashboard/admin/overview": "Pre-execution safety overview",
  "/dashboard/admin/users": "User access",
  "/dashboard/admin/permissions": "Role permissions",
  "/dashboard/admin/ai-requests": "AI guardrails",
  "/dashboard/admin/audit-logs": "Audit trail",
};

const patientPages = {
  "/dashboard/patient/overview": "Welcome, Test.",
  "/dashboard/patient/records": "My Medical Records",
  "/dashboard/patient/assistant": "Ask AI Assistant",
  "/dashboard/patient/history": "AI Request History",
  "/dashboard/patient/profile": "My Profile",
};

describe("direct URLs (page reload) render the matching page with an active sidebar item", () => {
  for (const [path, title] of Object.entries(adminPages)) {
    test(`admin ${path}`, () => {
      const html = renderRoute(path, adminAuth);
      assert.equal(pageTitle(html), title);
      assert.equal(activeNav(html), path);
      assert.equal((html.match(/is-active/g) || []).length, 1, "exactly one active item");
    });
  }

  for (const [path, title] of Object.entries(patientPages)) {
    test(`patient ${path}`, () => {
      const html = renderRoute(path, patientAuth);
      assert.equal(pageTitle(html), title);
      assert.equal(activeNav(html), path);
      assert.equal((html.match(/is-active/g) || []).length, 1, "exactly one active item");
    });
  }
});

describe("each page shows only its own content", () => {
  test("patient history page does not render the record or correction form", () => {
    const html = renderRoute("/dashboard/patient/history", patientAuth);
    assert.match(html, /Your requests/);
    assert.doesNotMatch(html, /Record details/);
    assert.doesNotMatch(html, /Request a treatment correction/);
    assert.doesNotMatch(html, /What your account can do/);
  });

  test("admin users page does not render audit or guardrail panels", () => {
    const html = renderRoute("/dashboard/admin/users", adminAuth);
    assert.match(html, /Invite user/);
    assert.doesNotMatch(html, /Ask AI about Audit Logs/);
    assert.doesNotMatch(html, /Ask AI about request decisions/);
    assert.doesNotMatch(html, /Permission matrix/);
  });

  test("sidebar links are page routes, never in-page anchors", () => {
    for (const [auth, pages] of [[adminAuth, adminPages], [patientAuth, patientPages]]) {
      const html = renderRoute(Object.keys(pages)[0], auth);
      for (const path of Object.keys(pages)) {
        assert.match(html, new RegExp(`class="ws-nav-link[^"]*" href="${path}"`));
      }
      assert.doesNotMatch(html, /class="ws-nav-link[^"]*" href="#/);
    }
  });

  test("the patient assistant remains a placeholder", () => {
    const html = renderRoute("/dashboard/patient/assistant", patientAuth);
    assert.match(html, /The assistant is not active yet/);
    assert.doesNotMatch(html, /<textarea|<input/);
  });
});

describe("role protection", () => {
  test("a Patient opening Admin pages sees no Admin content", () => {
    for (const path of Object.keys(adminPages)) {
      const html = renderRoute(path, patientAuth);
      assert.doesNotMatch(html, /Invite user|Permission matrix|AI request monitoring|Audit log|Command modules/, path);
      assert.doesNotMatch(html, /ws-theme-admin/, path);
    }
  });

  test("an Admin opening Patient pages sees no Patient content", () => {
    for (const path of Object.keys(patientPages)) {
      const html = renderRoute(path, adminAuth);
      assert.doesNotMatch(html, /Record details|Your requests|Request a treatment correction|Welcome, /, path);
      assert.doesNotMatch(html, /ws-theme-patient/, path);
    }
  });

  test("signed-out visitors see no protected content", () => {
    for (const path of [...Object.keys(adminPages), ...Object.keys(patientPages)]) {
      assert.equal(renderRoute(path, signedOut), "", path);
    }
  });

  test("unknown sub-pages render nothing while redirecting to the overview", () => {
    assert.equal(renderRoute("/dashboard/patient/not-a-page", patientAuth).includes("ws-page-title"), false);
    assert.equal(renderRoute("/dashboard/admin/not-a-page", adminAuth).includes("ws-page-title"), false);
  });

  test("a loading session shows the secure loading screen", () => {
    const html = renderRoute("/dashboard/patient/records", { status: "loading", user: null, session: null });
    assert.match(html, /Restoring your secure session/);
    assert.doesNotMatch(html, /Record details/);
  });
});

describe("history entries (Back / Forward)", () => {
  test("each history position renders its own page", () => {
    const entries = ["/dashboard/patient/overview", "/dashboard/patient/records", "/dashboard/patient/history"];
    assert.equal(pageTitle(renderRoute(entries, patientAuth, 0)), "Welcome, Test.");
    assert.equal(pageTitle(renderRoute(entries, patientAuth, 1)), "My Medical Records");
    assert.equal(pageTitle(renderRoute(entries, patientAuth, 2)), "AI Request History");
  });
});

describe("login page", () => {
  test("renders the split-screen sign-in with no prefilled credentials", () => {
    const html = renderRoute("/login", signedOut);
    assert.match(html, /class="auth-split"/);
    assert.match(html, /<label for="email">Email address<\/label>/);
    assert.match(html, /<label for="password">Password<\/label>/);
    assert.match(html, /Healthcare Agent Safety Framework/);
    assert.doesNotMatch(html, /<input[^>]*value="[^"]+"/, "inputs start empty");
    assert.ok(html.indexOf("<main") < html.indexOf("<aside"), "form comes before the visual panel");
  });
});
