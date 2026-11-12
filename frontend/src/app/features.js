/**
 * Frontend feature flags. A flag is ON only when its value is exactly "true".
 *
 * VITE_SERVICE_REQUESTS_ENABLED shows the Patient "Service requests" page and the Technician
 * "Tasks" page in the sidebar. Keep it off until migrations 010-012 are applied and verified
 * on the live Supabase project, and set HASF_SERVICE_REQUESTS_ENABLED=true on the backend at
 * the same time. This only hides navigation: the backend enforces the real gate.
 *
 * VITE_APPOINTMENTS_ENABLED switches the Patient sidebar from "AI Request History" to
 * "Appointments". Keep it off until migration 013 is applied and verified.
 */
export function readFeatureFlags(env = import.meta.env ?? {}) {
  return {
    serviceRequests: env.VITE_SERVICE_REQUESTS_ENABLED === "true",
    // Replaces the Patient "AI Request History" sidebar item with "Appointments" (the history
    // page stays reachable from the Appointments page). Needs migration 013 and the backend
    // flag HASF_APPOINTMENTS_ENABLED=true (which itself requires HASF_SERVICE_REQUESTS_ENABLED).
    appointments: env.VITE_APPOINTMENTS_ENABLED === "true",
  };
}
