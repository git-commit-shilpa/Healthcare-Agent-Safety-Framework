import { taskRequest } from "../../shared/taskApi.js";

/** Thin wrappers over the Technician endpoints. The caller passes the Supabase access token. */
export const getDashboard = (token) => taskRequest("/technician/dashboard", token);
export const listPatients = (token) => taskRequest("/technician/patients", token);
export const listTasks = (token) => taskRequest("/technician/tasks?limit=100", token);
export const getTask = (token, taskId) => taskRequest(`/technician/tasks/${Number(taskId)}`, token);
/** Read-only: is the requested appointment slot still bookable? Reserves nothing. */
export const checkAppointmentSlot = (token, taskId) =>
  taskRequest(`/technician/tasks/${Number(taskId)}/appointment-check`, token);
export const listApprovals = (token) => taskRequest("/technician/approvals", token);
export const startReview = (token, taskId) =>
  taskRequest(`/technician/tasks/${Number(taskId)}/start-review`, token, { method: "POST", body: {} });
export const submitDecision = (token, taskId, payload) =>
  taskRequest(`/technician/tasks/${Number(taskId)}/decision`, token, { method: "POST", body: payload });
export const completeTask = (token, taskId, patientMessage) =>
  taskRequest(`/technician/tasks/${Number(taskId)}/complete`, token, {
    method: "POST",
    body: patientMessage?.trim() ? { patient_message: patientMessage.trim() } : {},
  });
/** Coordination only (ready for Physician review, ask the patient, close after the Physician decided). */
export const submitCorrectionAction = (token, taskId, payload) =>
  taskRequest(`/technician/tasks/${Number(taskId)}/correction-action`, token, { method: "POST", body: payload });
