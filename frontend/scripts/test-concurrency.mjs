/** Bound browser test workers inside flow containers; leave CI's default intact. */
export function testConcurrency(env = process.env) {
  const override = env.PRELOOP_TEST_CONCURRENCY;
  if (override !== undefined) {
    if (!/^[1-9]\d*$/.test(override) || !Number.isSafeInteger(Number(override))) {
      throw new Error('PRELOOP_TEST_CONCURRENCY must be a positive integer');
    }
    return Number(override);
  }
  // Flow runners expose host CPUs even when their cgroup has only one CPU
  // and 4 GiB of RAM. The runner's CPU-derived default can open too many
  // Chromium test pages for that memory budget.
  return env.FLOW_ID && env.EXECUTION_ID ? 1 : undefined;
}
