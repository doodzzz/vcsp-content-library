// Unit test for the tenant URL rules in src/main/portal-url.ts (runs with `npm test`).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const source = readFileSync(new URL("../src/main/portal-url.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
}).outputText;
const { portalUrlFor } = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

const B = "https://vcsp.bank.local";
const cases = [
    [[B, "tenant", "acme"], { url: `${B}/tenants/acme/upload/` }],
    [[`${B}/`, "tenant", "ACME-Bank"], { url: `${B}/tenants/acme-bank/upload/` }],
    [[`${B}:8443`, "tenant", "acme"], { url: `${B}:8443/tenants/acme/upload/` }],
    [[B, "service-provider", "System"], { url: `${B}/upload/` }],
    [[B, "tenant", "acme_bank"], "error"],          // underscore: not a valid tenant name
    [[B, "tenant", "ab"], "error"],                 // too short
    [[B, "tenant", "x".repeat(33)], "error"],       // too long
    [[B, "tenant", "acme/../x"], "error"],          // no path tricks
    [[B, "tenant", ""], "error"],
    [["http://vcsp.bank.local", "tenant", "acme"], "error"],   // https only
    [[`${B}/some/path`, "tenant", "acme"], "error"],           // address only
    [["https://vcsp.example.local", "tenant", "acme"], "error"], // never packaged
];
for (const [args, want] of cases) {
    const got = portalUrlFor(...args);
    if (want === "error") assert.ok(got.error && !got.url, `${JSON.stringify(args)} should be refused`);
    else assert.deepEqual(got, want, JSON.stringify(args));
}
console.log(`portal-url: ${cases.length} cases passed`);
