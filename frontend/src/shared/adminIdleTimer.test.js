import assert from "node:assert/strict";
import test from "node:test";
import { ADMIN_IDLE_TIMEOUT_MS, createAdminIdleTimer } from "./adminIdleTimer.js";

function createFakeTimers() {
  let currentTime = 0;
  let nextId = 0;
  const timers = new Map();

  return {
    clearTimer(id) {
      timers.delete(id);
    },
    now() {
      return currentTime;
    },
    setTimer(callback, delay) {
      const id = ++nextId;
      timers.set(id, { callback, deadline: currentTime + delay });
      return id;
    },
    advance(milliseconds) {
      currentTime += milliseconds;
      while (true) {
        const due = [...timers.entries()]
          .filter(([, timer]) => timer.deadline <= currentTime)
          .sort((left, right) => left[1].deadline - right[1].deadline)[0];
        if (!due) return;
        timers.delete(due[0]);
        due[1].callback();
      }
    },
    jump(milliseconds) {
      currentTime += milliseconds;
    },
  };
}

test("Admin timer expires after ten minutes without activity", () => {
  const clock = createFakeTimers();
  let expirations = 0;
  createAdminIdleTimer(() => expirations++, {
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  clock.advance(ADMIN_IDLE_TIMEOUT_MS - 1);
  assert.equal(expirations, 0);

  clock.advance(1);
  assert.equal(expirations, 1);
});

test("Admin activity resets expiration and cannot revive an expired timer", () => {
  const clock = createFakeTimers();
  let expirations = 0;
  const timer = createAdminIdleTimer(() => expirations++, {
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  clock.advance(ADMIN_IDLE_TIMEOUT_MS - 1);
  assert.equal(timer.reset(), true);
  clock.advance(ADMIN_IDLE_TIMEOUT_MS - 1);
  assert.equal(expirations, 0);

  clock.advance(1);
  assert.equal(expirations, 1);
  assert.equal(timer.reset(), false);
  clock.advance(ADMIN_IDLE_TIMEOUT_MS);
  assert.equal(expirations, 1);
});

test("visibility checks expire a suspended Admin timer", () => {
  const clock = createFakeTimers();
  let expirations = 0;
  const timer = createAdminIdleTimer(() => expirations++, {
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  clock.jump(ADMIN_IDLE_TIMEOUT_MS);
  assert.equal(timer.check(), true);
  assert.equal(expirations, 1);

  const secondClock = createFakeTimers();
  let disposedExpirations = 0;
  const disposedTimer = createAdminIdleTimer(() => disposedExpirations++, {
    now: secondClock.now,
    setTimer: secondClock.setTimer,
    clearTimer: secondClock.clearTimer,
  });
  disposedTimer.dispose();
  secondClock.advance(ADMIN_IDLE_TIMEOUT_MS);
  assert.equal(disposedExpirations, 0);
});
