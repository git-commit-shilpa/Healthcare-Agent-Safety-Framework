import React from "react";
import ReactDOM from "react-dom/client";
import App from "./app/App.jsx";
import "./shared/styles.css";
import "./shared/workspace.css";
import "./app/auth.css";
import "./modules/patient/assistantChat.css";
import "./modules/patient/appointments.css";
import "./modules/technician/technician.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
