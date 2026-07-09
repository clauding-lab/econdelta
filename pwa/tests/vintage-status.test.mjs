// Node test for the PWA's data-freshness classifier (E3.2 VintagePill).
//
// The PWA is a build-step-free static SPA (vendored React UMD + in-browser
// Babel — pwa/index.html loads components.jsx as type="text/babel"), so there
// is no bundler or jsdom. This test exercises the REAL components.jsx by
// transforming it with the SAME vendored Babel the browser uses, evaluating it
// in a sandbox that stubs React + window + a controllable Date, then reading the
// `vintageStatus` / `CADENCE_DAYS` the module exposes via Object.assign(window).
//
// Run: node --test pwa/tests/   (or: node pwa/tests/vintage-status.test.mjs)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));

const Babel = require(join(here, '..', 'vendor', 'babel.min.js'));
const source = readFileSync(join(here, '..', 'components.jsx'), 'utf8');
const { code } = Babel.transform(source, {
  presets: ['react'],
  filename: 'components.jsx',
});

// Controllable clock. vintageStatus() reads Date.now() (the "today" anchor) and
// new Date(asOf) (the metric's vintage); we drive "today" by mutating NOW_MS.
let NOW_MS = 0;
const RealDate = Date;
function DateStub(...args) {
  return args.length ? new RealDate(...args) : new RealDate(NOW_MS);
}
DateStub.now = () => NOW_MS;
DateStub.parse = RealDate.parse.bind(RealDate);
DateStub.UTC = RealDate.UTC.bind(RealDate);
DateStub.prototype = RealDate.prototype;

const noop = () => {};
const reactStub = { useState: noop, useEffect: noop, useRef: noop, useMemo: noop };
const win = {};
// Only top-level statement in components.jsx is Object.assign(window, {...}); the
// component functions are merely DEFINED here (their JSX/globals never execute).
// eslint-disable-next-line no-new-func
new Function('React', 'window', 'Date', code)(reactStub, win, DateStub);

const { vintageStatus, CADENCE_DAYS } = win;

/** Age in days between an as_of date and a fixed "today". */
function at(todayIso, asOf, cadence) {
  NOW_MS = RealDate.parse(todayIso);
  return vintageStatus(asOf, cadence);
}

test('the module exposes the classifier and the widened daily grace', () => {
  assert.equal(typeof vintageStatus, 'function');
  // The fix: daily grace is 4 days, aligned with v_metric_freshness grace_days=4.
  assert.equal(CADENCE_DAYS.daily, 4);
});

test('a real 4-day holiday+weekend gap renders CLEAN (New Year, Thu 2026-01-01)', () => {
  // Daily series last published Wed 2025-12-31; Thu 01-01 holiday, Fri/Sat
  // weekend → first possible next publish is Sun 01-04, a genuine 4-day gap.
  // age = 4, grace = 4 → 4 > 4 is false → no pill. Under the old grace of 3
  // this ambered every daily ticker; that regression is what this asserts gone.
  assert.equal(at('2026-01-04T12:00:00Z', '2025-12-31', 'daily'), null);
});

test('a real 4-day holiday+weekend gap renders CLEAN (Independence Day, Thu 2026-03-26)', () => {
  // Last publish Wed 03-25; Thu 03-26 holiday, Fri/Sat weekend → next Sun 03-29.
  assert.equal(at('2026-03-29T12:00:00Z', '2026-03-25', 'daily'), null);
});

test('a 3-day gap is clean', () => {
  assert.equal(at('2026-01-03T12:00:00Z', '2025-12-31', 'daily'), null);
});

test('a 5-day gap ambers (beyond a holiday+weekend, something is late)', () => {
  const s = at('2026-01-05T12:00:00Z', '2025-12-31', 'daily');
  assert.equal(s.level, 'amber');
  assert.equal(s.ageDays, 5);
});

test('past 2x grace (9 days) it reds', () => {
  const s = at('2026-01-09T12:00:00Z', '2025-12-31', 'daily');
  assert.equal(s.level, 'red');
  assert.equal(s.ageDays, 9);
});

test('missing as_of or unrecognised cadence never guesses a pill', () => {
  assert.equal(at('2026-01-09T12:00:00Z', null, 'daily'), null);
  assert.equal(at('2026-01-09T12:00:00Z', '2025-12-31', null), null);
  assert.equal(at('2026-01-09T12:00:00Z', '2025-12-31', 'hourly'), null);
});
