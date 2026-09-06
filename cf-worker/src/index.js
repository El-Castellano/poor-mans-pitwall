export { TimingPoller } from "./durable-object.js";

export default {
  async fetch(request, env) {
    const id = env.TIMING_POLLER.idFromName("singleton");
    const stub = env.TIMING_POLLER.get(id);
    return stub.fetch(request);
  },

  async scheduled(event, env) {
    // Belt-and-braces keep-alive: pings the Durable Object every minute
    // (see wrangler.toml's [triggers]) so its alarm loop gets restarted if
    // it were ever evicted. Harmless no-op if the loop is already ticking
    // every few seconds on its own.
    const id = env.TIMING_POLLER.idFromName("singleton");
    const stub = env.TIMING_POLLER.get(id);
    await stub.fetch("https://internal/ping");
  },
};
