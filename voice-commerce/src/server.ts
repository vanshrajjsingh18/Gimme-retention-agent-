/**
 * HTTP entry point: REST Voice API + MCP (Streamable HTTP) + adapters.
 *
 *   npm run dev       (sandbox, port 8787)
 *   VOICE_ENV=production node dist/server.js
 */

import { loadConfig } from "./config.js";
import { createRuntime } from "./app.js";
import { createHttpApp } from "./http/app.js";
import { createLogger } from "./observability/logger.js";
import { stdoutAuditSink } from "./audit/audit-log.js";
import { mintSandboxToken } from "./security/access-tokens.js";
import { AGENT_SCOPES, ALL_SCOPES } from "./security/scopes.js";

const config = loadConfig();
const log = createLogger({ service: "gimme-voice-commerce", env: config.env });

if (config.env === "production") {
  // Production must be wired to GIMME's real services behind the ports in src/backend/ports.ts.
  // The sandbox backend is deliberately not usable in production.
  log.error("No production GimmeBackend is configured. Implement the ports in src/backend/ports.ts and wire them here.");
  process.exit(1);
}

const rt = createRuntime({ config, log, auditSinks: [stdoutAuditSink] });
const app = createHttpApp(rt);

app.listen(config.port, async () => {
  log.info("GIMME Voice Commerce listening", { url: config.publicBaseUrl, mcp: `${config.publicBaseUrl}/mcp` });
  if (config.env === "sandbox") {
    const agent = await mintSandboxToken(config, { customerId: "TEST_CUSTOMER", scopes: AGENT_SCOPES, clientId: "sandbox-agent", ttlSeconds: 8 * 3600 });
    const firstParty = await mintSandboxToken(config, { customerId: "TEST_CUSTOMER", scopes: ALL_SCOPES, clientId: "gimme-ios-app", ttlSeconds: 8 * 3600 });
    process.stdout.write(
      `\nSandbox tokens for TEST_CUSTOMER (valid 8h):\n  agent (no confirm scope): ${agent}\n  first-party app:          ${firstParty}\n\n`,
    );
  }
});
