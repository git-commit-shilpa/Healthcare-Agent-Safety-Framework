import { taskRequest } from "../../shared/taskApi.js";

const base = "/patient/service-requests";

export const listCategories = (token) => taskRequest(`${base}/categories`, token);
export const listRequests = (token) => taskRequest(base, token);
export const submitRequest = (token, payload) => taskRequest(base, token, { method: "POST", body: payload });
export const cancelRequest = (token, taskId) =>
  taskRequest(`${base}/${Number(taskId)}/cancel`, token, { method: "POST", body: {} });
export const addRequestInfo = (token, taskId, message) =>
  taskRequest(`${base}/${Number(taskId)}/info`, token, { method: "POST", body: { message: message.trim() } });
