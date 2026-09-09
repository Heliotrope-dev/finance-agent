#!/usr/bin/env node
/**
 * Send one text message through the installed OpenClaw Weixin plugin.
 *
 * `openclaw message send` launches a fresh plugin runtime.  That process does
 * not restore the persisted Weixin context token, despite the token being a
 * required field for dependable outbound delivery.  This bridge restores the
 * token explicitly, then uses the plugin's own API sender so request format,
 * headers and error handling stay owned by the plugin.
 *
 * Stdout is deliberately JSON-only.  Python callers can treat a missing token
 * or an API error as failure and leave notification work pending for retry.
 */
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [accountId, target, message] = process.argv.slice(2);

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(2);
}

function safePart(value, label) {
  if (!value || !/^[A-Za-z0-9_@.:-]+$/.test(value)) {
    fail(`invalid ${label}`);
  }
  return value;
}

function locatePluginRoot() {
  const explicit = process.env.OPENCLAW_WEIXIN_PLUGIN_ROOT;
  if (explicit && fs.existsSync(path.join(explicit, "dist", "src", "messaging", "send.js"))) {
    return explicit;
  }

  const projectsRoot = "/root/.openclaw/npm/projects";
  try {
    const candidates = [];
    for (const entry of fs.readdirSync(projectsRoot)) {
      if (!entry.startsWith("tencent-weixin-openclaw-weixin-")) continue;
      const candidate = path.join(
        projectsRoot,
        entry,
        "node_modules",
        "@tencent-weixin",
        "openclaw-weixin",
      );
      if (fs.existsSync(path.join(candidate, "dist", "src", "messaging", "send.js"))) {
        try {
          const version = String(JSON.parse(
            fs.readFileSync(path.join(candidate, "package.json"), "utf8"),
          ).version || "0.0.0");
          candidates.push({ candidate, version });
        } catch {
          // Ignore incomplete plugin directories left behind by an interrupted update.
        }
      }
    }
    if (candidates.length) {
      // Plugin updates leave the old generation on disk.  Do not let directory
      // iteration silently keep the sender on an obsolete implementation.
      candidates.sort((a, b) => a.version.localeCompare(b.version, undefined, {
        numeric: true,
      }));
      return candidates.at(-1).candidate;
    }
  } catch {
    // The explicit error below tells the operator which dependency is absent.
  }
  fail("OpenClaw Weixin plugin is not installed; cannot make a context-bound send");
}

safePart(accountId, "account id");
safePart(target, "target");
if (typeof message !== "string" || !message.length) fail("message is empty");

const pluginRoot = locatePluginRoot();
const accountPath = path.join(
  "/root/.openclaw/openclaw-weixin/accounts",
  `${accountId}.json`,
);
let account;
try {
  account = JSON.parse(fs.readFileSync(accountPath, "utf8"));
} catch {
  fail(`cannot read Weixin account ${accountId}`);
}
if (typeof account.baseUrl !== "string" || typeof account.token !== "string" || !account.token) {
  fail(`Weixin account ${accountId} has no usable credentials`);
}

const inbound = await import(
  pathToFileURL(path.join(pluginRoot, "dist", "src", "messaging", "inbound.js")).href,
);
const sender = await import(
  pathToFileURL(path.join(pluginRoot, "dist", "src", "messaging", "send.js")).href,
);

// The crucial difference from `openclaw message send`: restore on this new
// process before reading the token.  Never silently send without one.
inbound.restoreContextTokens(accountId);
const contextToken = inbound.getContextToken(accountId, target);
if (!contextToken) {
  fail(`no persisted Weixin context token for ${target}; wait for an inbound WeChat message`);
}

try {
  const result = await sender.sendMessageWeixin({
    to: target,
    text: message,
    opts: { baseUrl: account.baseUrl, token: account.token, contextToken, timeoutMs: 30_000 },
  });
  process.stdout.write(JSON.stringify({ messageId: result.messageId, contextBound: true }) + "\n");
} catch (error) {
  fail(`Weixin API send failed: ${error instanceof Error ? error.message : String(error)}`);
}
