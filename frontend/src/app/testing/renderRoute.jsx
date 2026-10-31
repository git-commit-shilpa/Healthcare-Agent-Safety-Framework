import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";

import { AppRoutes } from "../App.jsx";
import { AuthStateProvider } from "../auth.jsx";

/**
 * Renders the real route tree for a URL with a fixed auth state. Effects do
 * not run during server rendering, so no API calls are made.
 */
export function renderRoute(entries, auth, initialIndex) {
  const initialEntries = Array.isArray(entries) ? entries : [entries];
  // MemoryRouter uses useLayoutEffect, which React reports as a no-op during
  // server rendering. That one expected message is hidden; others still show.
  const originalError = console.error;
  console.error = (message, ...rest) => {
    if (typeof message === "string" && message.startsWith("Warning: useLayoutEffect does nothing on the server")) {
      return;
    }
    originalError(message, ...rest);
  };
  try {
    return renderToStaticMarkup(
      <MemoryRouter initialEntries={initialEntries} initialIndex={initialIndex}>
        <AuthStateProvider value={auth}>
          <AppRoutes />
        </AuthStateProvider>
      </MemoryRouter>,
    );
  } finally {
    console.error = originalError;
  }
}
