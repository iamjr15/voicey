import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const [vectorPath, installPrefix] = process.argv.slice(2);
if (!vectorPath || !installPrefix) {
  throw new Error("usage: node verify_node.mjs <vector.json> <npm-prefix>");
}

const require = createRequire(import.meta.url);
const { Webhook } = require(
  path.join(installPrefix, "node_modules", "standardwebhooks"),
);
const vector = JSON.parse(await readFile(vectorPath, "utf8"));
const signature = new Webhook(vector.secret).sign(
  vector.event_id,
  new Date(vector.timestamp * 1000),
  vector.body,
);

assert.equal(signature, vector.signature);
