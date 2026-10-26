export const ADMIN_IDLE_TIMEOUT_MS = 10 * 60 * 1000;

export function createAdminIdleTimer(
  onExpire,
  {
    timeoutMs = ADMIN_IDLE_TIMEOUT_MS,
    now = Date.now,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
  } = {},
) {
  let expiresAt = now() + timeoutMs;
  let timerHandle;
  let expired = false;
  let disposed = false;

  function armTimer() {
    clearTimer(timerHandle);
    timerHandle = setTimer(check, Math.max(0, expiresAt - now()));
  }

  function check() {
    if (disposed || expired) return expired;
    if (now() >= expiresAt) {
      expired = true;
      clearTimer(timerHandle);
      onExpire();
      return true;
    }
    armTimer();
    return false;
  }

  function reset() {
    if (disposed || expired) return false;
    expiresAt = now() + timeoutMs;
    armTimer();
    return true;
  }

  armTimer();

  return {
    check,
    dispose() {
      disposed = true;
      clearTimer(timerHandle);
    },
    reset,
  };
}
