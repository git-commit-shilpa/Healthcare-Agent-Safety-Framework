import { taskRequest } from "../../shared/taskApi.js";

const base = "/patient/appointments";

export const listPhysicians = (token) => taskRequest(`${base}/physicians`, token);

export const getAvailability = (token, physicianId, date) =>
  taskRequest(
    `${base}/physicians/${Number(physicianId)}/availability?date=${encodeURIComponent(date)}`,
    token,
  );

export const listAppointments = (token) => taskRequest(base, token);

export const getAppointment = (token, requestId) => taskRequest(`${base}/${Number(requestId)}`, token);

/** `payload` comes from buildBookingPayload(): the requester is never sent. */
export const requestAppointment = (token, payload) =>
  taskRequest(base, token, { method: "POST", body: payload });

export const cancelAppointment = (token, requestId) =>
  taskRequest(`${base}/${Number(requestId)}/cancel`, token, { method: "POST", body: {} });

export const replyToAppointment = (token, requestId, message) =>
  taskRequest(`${base}/${Number(requestId)}/respond`, token, {
    method: "POST",
    body: { message: message.trim() },
  });
