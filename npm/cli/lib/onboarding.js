'use strict';

const { terminal } = require('./terminal');

const enabled = value => !!value && !['0', 'false', 'no', 'off'].includes(value.toLowerCase());
const informational = args => args.some(arg => ['--help', '-h', '--version', '-V'].includes(arg));

function shouldOnboard(args = [], options = {}) {
  const env = options.env || process.env;
  return !informational(args) && !enabled(env.CI) && !enabled(env.RAMP_ROUTER_SKIP_ONBOARDING);
}

function onboardingSession(args = [], options = {}) {
  if (!shouldOnboard(args, options)) return null;
  return (options.terminal || terminal)({ allowCaptured: options.lifecycle === true });
}

module.exports = { shouldOnboard, onboardingSession, informational };
