import React from "react";
import ReactDOM from "react-dom/client";
import App from "./app/App.jsx";
import "./shared/styles.css";
import "./shared/workspace.css";
import "./app/auth.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
