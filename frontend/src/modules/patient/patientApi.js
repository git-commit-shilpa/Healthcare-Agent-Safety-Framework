const apiBaseUrl = import.meta.env?.VITE_API_BASE_URL || "/api";

export const PATIENT_GET_TIMEOUT_MS = 20_000;
// Longer than the backend's own Supabase timeout so the backend can report an
// unconfirmed outcome (504) before the browser gives up.
export const PATIENT_SUBMIT_TIMEOUT_MS = 30_000;

export class PatientApiError extends Error {
  /**
   * kind:
   *  - "http": the backend answered with an error status
   *  - "unreachable": the backend could not be reached (e.g. dev proxy 500 with no body)
   *  - "timeout": the browser stopped waiting for a response
   *  - "network": the request failed without any response
   *  - "invalid": the backend answered successfully with unexpected data
   */
  constructor(message, { kind, status = null } = {}) {
    super(message);
    this.name = "PatientApiError";
    this.kind = kind;
    this.status = status;
  }
}

async function readBody(response) {
  const text = await response.text();
  if (!text) return { empty: true, data: null };
  try {
    return { empty: false, data: JSON.parse(text) };
  } catch {
    return { empty: false, data: null };
  }
}

export async function patientRequest(
  path,
  accessToken,
  { method = "GET", body, timeoutMs = PATIENT_GET_TIMEOUT_MS, fetchImpl = fetch } = {},
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetchImpl(`${apiBaseUrl}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${accessToken}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch {
    if (controller.signal.aborted) {
      throw new PatientApiError("The server took too long to respond.", { kind: "timeout" });
    }
    throw new PatientApiError("The server could not be reached.", { kind: "network" });
  } finally {
    clearTimeout(timer);
  }

  const { empty, data } = await readBody(response);
  if (response.ok) {
    if (data === null) {
      throw new PatientApiError("The server returned an invalid response.", {
        kind: "invalid",
        status: response.status,
      });
    }
    return data;
  }

  // The Vite development proxy answers 500 with an empty body when the API
  // server is not running; FastAPI errors always include a JSON body.
  if (empty && response.status === 500) {
    throw new PatientApiError(
      "The HASF backend is not reachable. Make sure the API server is running, then try again.",
      { kind: "unreachable", status: 500 },
    );
  }

  const detail =
    data && typeof data.detail === "string" && data.detail.trim()
      ? data.detail
      : `Request failed with status ${response.status}.`;
  throw new PatientApiError(detail, { kind: "http", status: response.status });
}

export function getPatientRecord(accessToken, options) {
  return patientRequest("/patient/my-record", accessToken, options);
}

export function getCorrectionRequests(accessToken, options) {
  return patientRequest("/patient/correction-requests", accessToken, options);
}

export function submitTreatmentCorrection(accessToken, proposedTreatment, options) {
  return patientRequest("/patient/correction-requests", accessToken, {
    method: "POST",
    body: { proposed_treatment: proposedTreatment },
    timeoutMs: PATIENT_SUBMIT_TIMEOUT_MS,
    ...options,
  });
}
