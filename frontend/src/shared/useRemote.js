import { useCallback, useEffect, useState } from "react";

import { describeError, isFeatureDisabled } from "./taskApi.js";

/**
 * Loads data once per dependency change and exposes reload().
 * status: "loading" | "ready" | "error" | "unavailable" (feature switched off on the backend).
 */
export function useRemote(load, deps) {
  const [state, setState] = useState({ status: "loading", data: null, error: "" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let active = true;
    setState((current) => ({ status: "loading", data: current.data, error: "" }));
    load()
      .then((data) => {
        if (active) setState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (!active) return;
        setState({
          status: isFeatureDisabled(error) ? "unavailable" : "error",
          data: null,
          error: describeError(error),
        });
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((current) => current + 1), []);
  return { ...state, reload };
}
