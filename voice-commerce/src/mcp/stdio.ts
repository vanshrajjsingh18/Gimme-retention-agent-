/**
 * MCP over stdio, for local development with desktop MCP clients.
 *
 * The principal is fixed for the process: GIMME_ACCESS_TOKEN is verified at
 * start-up exactly as a bearer token would be. In the sandbox, omit it and a
 * TEST_CUSTOMER agent token is minted. Logs go to stderr — stdout is the
 * protocol channel.
 */

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { createRuntime } from "../app.js";
import { loadConfig } from "../config.js";
import { stderrLogger } from "../observability/logger.js";
import { mintSandboxToken } from "../security/access-tokens.js";
import { AGENT_SCOPES } from "../security/scopes.js";
import { createMcpServer } from "./server.js";

const config = loadConfig();
const log = stderrLogger();
const rt = createRuntime({ config, log });

const token =
  process.env.GIMME_ACCESS_TOKEN ??
  (config.env === "sandbox"
    ? await mintSandboxToken(config, { customerId: process.env.SANDBOX_CUSTOMER ?? "TEST_CUSTOMER", scopes: AGENT_SCOPES, clientId: "stdio-agent" })
    : undefined);
const principal = await rt.verifier.verify(token);

const server = createMcpServer(rt, { stdioPrincipal: principal });
await server.connect(new StdioServerTransport());
log.info("GIMME MCP (stdio) ready", { customer: principal.customerId, scopes: principal.scopes });
