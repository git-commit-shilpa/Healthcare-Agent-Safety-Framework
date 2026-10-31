import assert from "node:assert/strict";
import test from "node:test";

import { dashboardPath, decideRouteAccess, pageForPath, workspacePages } from "./routeAccess.js";

const ready = (role) => ({ status: "ready", user: { role }, session: {} });

test("signed-out users are sent to the login page", () => {
  assert.deepEqual(decideRouteAccess({ status: "signed-out", user: null }, "admin"), {
    type: "redirect",
    to: "/login",
  });
  assert.deepEqual(decideRouteAccess({ status: "signed-out", user: null }, "patient"), {
    type: "redirect",
    to: "/login",
  });
});

test("loading and account problems never render a protected page", () => {
  assert.equal(decideRouteAccess({ status: "loading" }, "admin").type, "loading");
  assert.equal(decideRouteAccess(null, "admin").type, "loading");
  assert.equal(decideRouteAccess({ status: "blocked" }, "patient").type, "issue");
  assert.equal(decideRouteAccess({ status: "configuration-error" }, "patient").type, "issue");
});

test("a Patient cannot open Admin routes and an Admin cannot open Patient routes", () => {
  assert.deepEqual(decideRouteAccess(ready("patient"), "admin"), {
    type: "redirect",
    to: "/dashboard/patient",
  });
  assert.deepEqual(decideRouteAccess(ready("admin"), "patient"), {
    type: "redirect",
    to: "/dashboard/admin",
  });
  assert.deepEqual(decideRouteAccess(ready("physician"), "patient"), {
    type: "redirect",
    to: "/dashboard/physician",
  });
});

test("the matching role is allowed", () => {
  assert.deepEqual(decideRouteAccess(ready("admin"), "admin"), { type: "allow" });
  assert.deepEqual(decideRouteAccess(ready("patient"), "patient"), { type: "allow" });
});

test("every sidebar item has its own page path", () => {
  for (const [role, pages] of Object.entries(workspacePages)) {
    const paths = pages.map((page) => page.path);
    assert.equal(new Set(paths).size, paths.length, `${role} page paths are unique`);
    for (const page of pages) {
      assert.doesNotMatch(page.path, /#/, "no scroll-to-section links");
      assert.equal(pageForPath(role, dashboardPath(role, page.path)), page);
    }
  }
});

test("page lookup ignores other roles and unknown paths", () => {
  assert.equal(pageForPath("patient", "/dashboard/admin/users"), null);
  assert.equal(pageForPath("patient", "/dashboard/patient/unknown"), null);
  assert.equal(pageForPath("admin", "/dashboard/admin"), null);
  assert.equal(pageForPath("patient", "/dashboard/patient/history/extra").path, "history");
});
