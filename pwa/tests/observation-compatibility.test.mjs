// Exercise the actual latest page against old and additively enriched bundles.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const Babel = require('../vendor/babel.min.js');
const source = readFileSync(new URL('../pages/latest.jsx', import.meta.url), 'utf8');
const { code } = Babel.transform(source, { presets: ['react'], filename: 'latest.jsx' });

test('latest page renders the same legacy data with optional observation metadata', () => {
  const bundle = { updated_at: '2026-09-25T00:00:00+00:00', sources_status: {},
    data: { advancing: 100, declining: 50, unchanged: 10 } };
  const window = { ED_DATA: { bundle, tickerGroups: [], tickers: [] } };
  const react = { useState: () => [null, () => {}], Fragment: 'Fragment',
    createElement: (type, props, ...children) => ({ type: typeof type === 'function' ? type.name : type, props, children }) };
  const names = ['StatusPill', 'PageHead', 'Sparkline', 'VintagePill'];
  const page = new Function('React', 'window', ...names, `${code}; return PageLatest;`)(react, window, ...names);
  const old = page();
  bundle.observations = { advancing: { value: 100, as_of: '2026-09-24', quality: 'verified', date_basis: 'observation' } };
  assert.deepEqual(page(), old);
});
