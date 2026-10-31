import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  adminSessionExpiredEvent,
  apiBaseUrl,
  getApiJson,
  readApiResponse,
} from "../shared/apiClient.js";
import { createAdminIdleTimer } from "../shared/adminIdleTimer.js";
import { supabase } from "../shared/supabase.js";

const authContext = createContext(null);

export function AuthProvider({ children }) {
  const [auth, setAuth] = useState({
    status: "loading",
    session: null,
    user: null,
    adminOverview: null,
    error: "",
  });
  const authRef = useRef(auth);
  const logoutRef = useRef(null);
  authRef.current = auth;

  useEffect(() => {
    if (!supabase) {
      setAuth({
        status: "configuration-error",
        session: null,
        user: null,
        adminOverview: null,
        error: "Supabase Auth is not configured. Set the frontend environment variables and restart Vite.",
      });
      return undefined;
    }

    let active = true;
    let requestNumber = 0;

    async function loadAccount(session) {
      const currentRequest = ++requestNumber;
      if (!active) return;

      if (!session) {
        setAuth({
          status: "signed-out",
          session: null,
          user: null,
          adminOverview: null,
          error: "",
        });
        return;
      }

      const currentAuth = authRef.current;
      if (
        currentAuth.status === "ready" &&
        currentAuth.session?.user.id === session.user.id
      ) {
        setAuth({ ...currentAuth, session });
        return;
      }

      setAuth({
        status: "loading",
        session,
        user: null,
        adminOverview: null,
        error: "",
      });

      try {
        const result = await getApiJson("/auth/bootstrap", session.access_token);
        if (currentRequest === requestNumber && active) {
          setAuth({
            status: "ready",
            session,
            user: result.user,
            adminOverview: result.admin_overview,
            error: "",
          });
        }
      } catch (error) {
        if (currentRequest === requestNumber && active) {
          setAuth({
            status: "blocked",
            session,
            user: null,
            adminOverview: null,
            error:
              error instanceof Error
                ? error.message
                : "Could not validate this account.",
          });
        }
      }
    }

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      void loadAccount(session);
    });

    function handleAdminSessionExpired() {
      setAuth({
        status: "signed-out",
        session: null,
        user: null,
        adminOverview: null,
        error: "",
      });
      void supabase.auth.signOut({ scope: "local" }).then(
        ({ error }) => {
          if (error) console.error("Could not clear the expired local session.", error);
        },
        (error) => console.error("Could not clear the expired local session.", error),
      );
    }

    window.addEventListener(adminSessionExpiredEvent, handleAdminSessionExpired);

    return () => {
      active = false;
      window.removeEventListener(adminSessionExpiredEvent, handleAdminSessionExpired);
      subscription.unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (
      auth.status !== "ready" ||
      auth.user?.role !== "admin" ||
      !auth.session
    ) {
      return undefined;
    }

    let active = true;
    let lastActivitySignalAt = Number.NEGATIVE_INFINITY;
    let queuedActivitySignal = null;
    const timer = createAdminIdleTimer(() => {
      const endSession = logoutRef.current;
      if (endSession) {
        const accessToken = authRef.current.session?.access_token;
        if (accessToken) {
          void fetch(`${apiBaseUrl}/admin/session/logout`, {
            method: "POST",
            headers: { Authorization: `Bearer ${accessToken}` },
          })
            .then((response) =>
              readApiResponse(response, { notifyAdminExpiry: false }),
            )
            .catch((error) => {
              console.error("Could not end the idle Admin session on the backend.", error);
            });
        }
        void endSession({ bestEffort: true }).catch((error) => {
          console.error("Could not clear the idle Admin session.", error);
          setAuth({
            status: "signed-out",
            session: null,
            user: null,
            adminOverview: null,
            error: "",
          });
        });
      }
    });

    async function recordActivity() {
      const accessToken = authRef.current.session?.access_token;
      if (!accessToken) return;

      try {
        const response = await fetch(`${apiBaseUrl}/admin/session/activity`, {
          method: "POST",
          headers: { Authorization: "Bearer " + accessToken },
        });
        await readApiResponse(response);
      } catch (error) {
        if (active) console.error("Could not record Admin activity.", error);
      }
    }

    function handleInteraction(event) {
      if (!event.isTrusted || document.visibilityState !== "visible") return;
      if (!timer.reset()) return;

      const now = Date.now();
      if (now - lastActivitySignalAt >= 1000) {
        if (queuedActivitySignal !== null) {
          clearTimeout(queuedActivitySignal);
          queuedActivitySignal = null;
        }
        lastActivitySignalAt = now;
        void recordActivity();
      } else {
        if (queuedActivitySignal !== null) clearTimeout(queuedActivitySignal);
        queuedActivitySignal = setTimeout(() => {
          queuedActivitySignal = null;
          if (active && document.visibilityState === "visible") {
            lastActivitySignalAt = Date.now();
            void recordActivity();
          }
        }, 1000 - (now - lastActivitySignalAt));
      }
    }

    function handleVisibilityChange() {
      if (document.visibilityState === "visible") timer.check();
    }

    const activityEvents = ["pointerdown", "keydown", "touchstart", "wheel"];
    for (const eventName of activityEvents) {
      document.addEventListener(eventName, handleInteraction, { passive: true });
    }
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      active = false;
      timer.dispose();
      if (queuedActivitySignal !== null) clearTimeout(queuedActivitySignal);
      for (const eventName of activityEvents) {
        document.removeEventListener(eventName, handleInteraction);
      }
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [auth.status, auth.user?.role, auth.session?.user.id]);

  async function login(email, password) {
    if (!supabase) {
      throw new Error("Supabase Auth is not configured.");
    }
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) throw error;
  }

  async function logout({ bestEffort = false } = {}) {
    if (!supabase) return;
    const currentAuth = authRef.current;
    const isAdmin = currentAuth.user?.role === "admin";
    let backendLogoutError = null;
    if (isAdmin && currentAuth.session?.access_token && !bestEffort) {
      try {
        const response = await fetch(`${apiBaseUrl}/admin/session/logout`, {
          method: "POST",
          headers: { Authorization: "Bearer " + currentAuth.session.access_token },
        });
        await readApiResponse(response, { notifyAdminExpiry: false });
      } catch (error) {
        backendLogoutError = error;
      }
    }

    const { error } = isAdmin
      ? await supabase.auth.signOut({ scope: "local" })
      : await supabase.auth.signOut();
    if (error) throw error;
    if (backendLogoutError && !bestEffort) throw backendLogoutError;
  }
  logoutRef.current = logout;

  return (
    <authContext.Provider value={{ ...auth, login, logout }}>
      {children}
    </authContext.Provider>
  );
}

export function useAuth() {
  return useContext(authContext);
}

/**
 * Supplies a fixed auth state without Supabase. Used only by the routing
 * tests; the application always uses AuthProvider.
 */
export function AuthStateProvider({ value, children }) {
  return <authContext.Provider value={value}>{children}</authContext.Provider>;
}