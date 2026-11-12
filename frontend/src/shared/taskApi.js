/**
 * Small authenticated JSON client for the service-request / Technician task endpoints.
 * Errors carry the backend's safe `detail` text and machine `code` (e.g. "feature_disabled",
 * "no_eligible_technician", "approval_required"). Nothing here logs request bodies.
 */
const apiBaseUrl = import.meta.env?.VITE_API_BASE_URL || "/api";

export const TASK_GET_TIMEOUT_MS = 20_000;
export const TASK_WRITE_TIMEOUT_MS = 30_000;

export class TaskApiError extends Error {
  /** kind: "http" | "unreachable" | "timeout" | "network" | "invalid" */
  constructor(message, { kind, status = null, code = null } = {}) {
    super(message);
    this.name = "TaskApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
  }
}

/** The backend answers 503 + code "feature_disabled" until the feature is switched on. */
export function isFeatureDisabled(error) {
  return error instanceof TaskApiError && error.status === 503 && error.code === "feature_disabled";
}

/** 504 means the outcome is unknown: do not retry a write blindly. */
export function isUnconfirmed(error) {
  return error instanceof TaskApiError && error.status === 504;
}

export function describeError(error, fallback = "Something went wrong. Please try again.") {
  return error instanceof Error && error.message ? error.message : fallback;
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

export async function taskRequest(
  path,
  accessToken,
  { method = "GET", body, timeoutMs, fetchImpl = fetch } = {},
) {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    timeoutMs ?? (method === "GET" ? TASK_GET_TIMEOUT_MS : TASK_WRITE_TIMEOUT_MS),
  );
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
      throw new TaskApiError("The server took too long to respond.", { kind: "timeout" });
    }
    throw new TaskApiError("The server could not be reached.", { kind: "network" });
  } finally {
    clearTimeout(timer);
  }

  const { empty, data } = await readBody(response);
  if (response.ok) {
    if (data === null) {
      throw new TaskApiError("The server returned an invalid response.", {
        kind: "invalid",
        status: response.status,
      });
    }
    return data;
  }
  if (empty && response.status === 500) {
    throw new TaskApiError(
      "The HASF backend is not reachable. Make sure the API server is running, then try again.",
      { kind: "unreachable", status: 500 },
    );
  }
  // FastAPI validation errors use a list for `detail`; show a generic message for those.
  const detail =
    data && typeof data.detail === "string" && data.detail.trim()
      ? data.detail
      : response.status === 422
        ? "Please check the details you entered."
        : `Request failed with status ${response.status}.`;
  throw new TaskApiError(detail, {
    kind: "http",
    status: response.status,
    code: typeof data?.code === "string" ? data.code : null,
  });
}
