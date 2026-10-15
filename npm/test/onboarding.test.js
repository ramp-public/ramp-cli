'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { shouldOnboard, onboardingSession } = require('../cli/lib/onboarding');

test('one gate permits interactive onboarding and recognizes disabled CI values', () => {
  const session = {};
  for (const CI of ['', '0', 'false', 'FALSE', 'no', 'off']) {
    assert.equal(onboardingSession([], { env: { CI }, terminal: () => session }), session);
  }
});

test('CI, skip mode, and help/version never open a terminal', () => {
  const terminal = () => { throw new Error('Must not open the terminal'); };
  for (const env of [{ CI: 'true' }, { CI: '1' }, { RAMP_ROUTER_SKIP_ONBOARDING: '1' }, { RAMP_ROUTER_SKIP_ONBOARDING: 'true' }]) {
    assert.equal(onboardingSession([], { env, terminal }), null);
  }
  for (const flag of ['--help', '-h', '--version', '-V']) {
    assert.equal(onboardingSession([flag], { env: {}, terminal }), null);
  }
});

test('headless installation defers the workspace because it has no wrapper terminal', () => {
  assert.equal(shouldOnboard([], { env: {} }), true);
  assert.equal(onboardingSession([], { env: {}, terminal: () => null }), null);
});
