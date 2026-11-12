import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const css = readFileSync(new URL("./assistantChat.css", import.meta.url), "utf8");

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
const contrast = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

test("secure forms inside the dark assistant panel pin dark text on the white field", () => {
  const rule = css.match(/\.assistant-command-panel \.ws-form :is\(input, select, textarea\) \{([^}]*)\}/);
  assert.ok(rule, "contrast rule is present");
  assert.match(rule[1], /background:\s*#fff/);
  assert.match(rule[1], /color:\s*#1c2541/);
  assert.match(rule[1], /caret-color:\s*#1c2541/);
  assert.match(css, /\.assistant-command-panel \.ws-form :is\(input, select, textarea\):focus \{[^}]*color:\s*#1c2541/);
});

test("typed text, placeholder and caret meet WCAG AA on white", () => {
  assert.ok(contrast("#1c2541", "#ffffff") >= 7);
  assert.ok(contrast("#5b6275", "#ffffff") >= 4.5);
});
