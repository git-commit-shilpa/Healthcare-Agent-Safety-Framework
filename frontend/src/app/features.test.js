import assert from "node:assert/strict";
import test from "node:test";

import { readFeatureFlags } from "./features.js";
import { buildWorkspacePages, workspacePages } from "./routeAccess.js";

const paths = (pages) => pages.map((p) => p.path);

test("flags are on only for exactly the string true", () => {
  assert.equal(readFeatureFlags({ VITE_SERVICE_REQUESTS_ENABLED: "true" }).serviceRequests, true);
  for (const value of ["TRUE", "1", "yes", "false", "", undefined]) {
    assert.equal(readFeatureFlags({ VITE_SERVICE_REQUESTS_ENABLED: value }).serviceRequests, false, String(value));
  }
  assert.equal(readFeatureFlags({}).serviceRequests, false);
});

test("with the flag off the Patient and Admin navigation is exactly what it was before", () => {
  const off = buildWorkspacePages({ serviceRequests: false });
  assert.deepEqual(paths(off.patient), ["overview", "records", "assistant", "requests", "profile"]);
  assert.deepEqual(paths(off.admin), ["overview", "users", "permissions", "ai-requests", "audit-logs"]);
  assert.deepEqual(paths(off.technician), ["overview", "patients"]);
});

test("with the flag on the new pages appear and nothing else changes", () => {
  const on = buildWorkspacePages({ serviceRequests: true });
  assert.deepEqual(paths(on.patient), ["overview", "records", "assistant", "requests", "profile"]);
  assert.deepEqual(paths(on.technician), ["overview", "patients", "tasks"]);
  assert.deepEqual(on.admin, buildWorkspacePages({}).admin);
});

test("the shipped navigation defaults to the flag being off", () => {
  assert.equal(paths(workspacePages.patient).includes("service-requests"), false);
  assert.equal(paths(workspacePages.technician).includes("tasks"), false);
});

test("the appointments flag is on only for exactly the string true", () => {
  assert.equal(readFeatureFlags({ VITE_APPOINTMENTS_ENABLED: "true" }).appointments, true);
  for (const value of ["TRUE", "1", "yes", "false", "", undefined]) {
    assert.equal(readFeatureFlags({ VITE_APPOINTMENTS_ENABLED: value }).appointments, false, String(value));
  }
  assert.equal(readFeatureFlags({}).appointments, false);
});

test("with appointments on, Appointments is its own sidebar item, separate from Requests", () => {
  const on = buildWorkspacePages({ appointments: true });
  assert.deepEqual(paths(on.patient), ["overview", "records", "assistant", "appointments", "requests", "profile"]);
  assert.equal(on.patient.find((p) => p.path === "appointments").label, "Appointments");
  assert.equal(on.patient.find((p) => p.path === "requests").label, "Requests");
  // The old separate entries are gone from the sidebar (their URLs redirect to Requests).
  assert.equal(paths(on.patient).includes("history"), false);
  assert.equal(paths(on.patient).includes("service-requests"), false);
});

test("appointments and service requests flags are independent and compose", () => {
  const both = buildWorkspacePages({ serviceRequests: true, appointments: true });
  assert.deepEqual(
    paths(both.patient),
    ["overview", "records", "assistant", "appointments", "requests", "profile"],
  );
  assert.deepEqual(paths(both.technician), ["overview", "patients", "tasks", "scheduling"]);
  assert.equal(both.technician.find((p) => p.path === "scheduling").label, "Scheduling Requests");
  assert.deepEqual(both.admin, buildWorkspacePages({}).admin);
});

test("without the appointments flag there is no Appointments or Scheduling Requests page", () => {
  const off = buildWorkspacePages({ serviceRequests: true });
  assert.equal(paths(off.patient).includes("appointments"), false);
  assert.deepEqual(paths(off.technician), ["overview", "patients", "tasks"]);
  assert.equal(paths(buildWorkspacePages({ appointments: true }).technician).includes("scheduling"), false);
});

test("Requests replaces AI Request History for every Patient", () => {
  assert.equal(paths(buildWorkspacePages({}).patient).includes("requests"), true);
  assert.equal(paths(buildWorkspacePages({}).patient).includes("history"), false);
});
