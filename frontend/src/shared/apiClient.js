export const apiBaseUrl = import.meta.env?.VITE_API_BASE_URL || "/api";
export const roles = ["admin", "patient", "physician", "technician"];
export const adminSessionExpiredMessage = "Admin session expired. Sign in again.";
export const adminSessionExpiredEvent = "admin-session-expired";
const pendingGetRequests = new Map();

export async function readApiResponse(response, { notifyAdminExpiry = true } = {}) {
  const result = await response.json();
  if (!response.ok) {
    if (
      notifyAdminExpiry &&
      response.status === 401 &&
      result.detail === adminSessionExpiredMessage
    ) {
      window.dispatchEvent(new Event(adminSessionExpiredEvent));
    }
    throw new Error(result.detail || `Request failed with status ${response.status}.`);
  }
  return result;
}

export function getApiJson(path, accessToken) {
  const requestKey = `${path}:${accessToken}`;
  const existingRequest = pendingGetRequests.get(requestKey);
  if (existingRequest) return existingRequest;

  const request = fetch(`${apiBaseUrl}${path}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
    .then(readApiResponse);

  let sharedRequest;
  sharedRequest = request.finally(() => {
    if (pendingGetRequests.get(requestKey) === sharedRequest) {
      pendingGetRequests.delete(requestKey);
    }
  });
  pendingGetRequests.set(requestKey, sharedRequest);
  return sharedRequest;
}
