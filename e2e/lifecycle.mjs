// Acceptance test 17: the complete election lifecycle through the real pages, on an
// emulated phone, one browser context (one "phone") per person.
//
//   node e2e/lifecycle.mjs <seed.json> <chromium|webkit>
//
// Environment: E2E_BASE_URL (default http://localhost:8765), PYTHON (default
// .venv/bin/python), PLAYWRIGHT_MODULE (path to playwright's index.mjs, optional).
// Time is simulated with e2e/clock.py and the scheduler is run with
// manage.py run_scheduler, exactly as in production.
import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";

const { chromium, webkit, devices } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const seed = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const browserName = process.argv[3] || "chromium";
const BASE = process.env.E2E_BASE_URL || "http://localhost:8765";
const PYTHON = process.env.PYTHON || ".venv/bin/python";
const ORG = `${BASE}/o/${seed.org}`;
const device = devices[browserName === "webkit" ? "iPhone 14" : "Pixel 7"];
const started = Date.now();

function log(step) {
  console.log(`[${browserName} ${((Date.now() - started) / 1000).toFixed(1)}s] ${step}`);
}
function fail(message) { throw new Error(message); }
function expect(condition, message) { if (!condition) fail(message); }

// --- TOTP (RFC 6238), one code per 30-second step and never reused -------------------
const lastStep = {};
function totpAt(hexKey, step) {
  const buf = Buffer.alloc(8); buf.writeBigUInt64BE(BigInt(step));
  const h = crypto.createHmac("sha1", Buffer.from(hexKey, "hex")).update(buf).digest();
  const o = h[h.length - 1] & 15;
  return String(((h.readUInt32BE(o) & 0x7fffffff) % 1000000)).padStart(6, "0");
}
async function freshCode(person) {
  let step = Math.floor(Date.now() / 30000);
  while (lastStep[person.email] !== undefined && step <= lastStep[person.email]) {
    await new Promise((r) => setTimeout(r, 1000));
    step = Math.floor(Date.now() / 30000);
  }
  lastStep[person.email] = step;
  return totpAt(person.totp_key, step);
}

// --- one phone per person -------------------------------------------------------------
const browser = await (browserName === "webkit" ? webkit : chromium).launch(
  process.env.E2E_EXECUTABLE ? { executablePath: process.env.E2E_EXECUTABLE } : {});
const errors = [];
const phones = {};
async function phone(key) {
  if (phones[key]) return phones[key];
  const person = seed.people[key];
  const context = await browser.newContext({ ...device });
  const page = await context.newPage();
  page.on("pageerror", (e) => errors.push(`${key}: ${e}`));
  // Failed requests are reported by URL below; the browser's own favicon request is not ours.
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) {
      errors.push(`${key}: ${m.text()}`);
    }
  });
  page.on("response", (r) => {
    if (r.status() >= 400 && new URL(r.url()).pathname !== "/favicon.ico") {
      errors.push(`${key}: ${r.status()} ${r.url()}`);
    }
  });
  await page.goto(`${BASE}/accounts/login/`);
  await page.fill('input[name="email"]', person.email);
  await page.fill('input[name="password"]', person.password);
  await page.click('main button[type="submit"]');
  await page.fill('input[name="token"]', await freshCode(person));
  await page.click('main button[type="submit"]');
  await page.waitForURL((url) => !url.pathname.startsWith("/accounts/"));
  phones[key] = { page, person };
  return phones[key];
}

// Privileged actions may ask for a fresh code (step-up) first.
async function stepUpIfAsked(p) {
  if (new URL(p.page.url()).pathname.startsWith("/accounts/step-up")) {
    await p.page.fill('input[name="token"]', await freshCode(p.person));
    await p.page.click('main button[type="submit"]');
    await p.page.waitForLoadState();
  }
}
async function go(p, url) {
  await p.page.goto(url);
  await stepUpIfAsked(p);
}
async function submit(p, selector = 'main form button[type="submit"]') {
  await Promise.all([p.page.waitForLoadState(), p.page.click(selector)]);
  await stepUpIfAsked(p);
}
async function text(p) { return p.page.textContent("main"); }
async function noSidewaysScroll(p, where) {
  const width = await p.page.evaluate(() => document.documentElement.scrollWidth);
  expect(width <= device.viewport.width + 1, `${where}: page is ${width}px wide on a ${device.viewport.width}px screen`);
}
function python(script, env = {}) {
  const out = execFileSync(PYTHON, ["manage.py", ...script], {
    env: { ...process.env, ...env }, encoding: "utf8", stdio: ["pipe", "pipe", "inherit"],
  });  // keep the scheduler's own lines, not the emails the console backend prints
  return out.split("\n").filter((l) => /^(election |audit:|deleted )/.test(l)).join("; ");
}
function pythonStdin(file, env) {
  return execFileSync(PYTHON, ["manage.py", "shell"], {
    input: fs.readFileSync(file), env: { ...process.env, ...env }, encoding: "utf8",
    stdio: ["pipe", "pipe", "inherit"],
  });
}
function nairobi(hoursFromNow) {  // datetime-local value in the organisation's time zone (UTC+3)
  return new Date(Date.now() + hoursFromNow * 3600e3 + 3 * 3600e3).toISOString().slice(0, 16);
}

try {
  // 1. The administrator creates the election and its rules, and opens nominations.
  const admin = await phone("admin");
  await go(admin, `${ORG}/elections/new/`);
  await admin.page.fill('input[name="title"]', "Board election (fictional)");
  await admin.page.fill('input[name="seats"]', "1");
  await admin.page.fill('input[name="opens_at"]', nairobi(2));
  await admin.page.fill('input[name="closes_at"]', nairobi(26));
  await submit(admin);
  const electionId = new URL(admin.page.url()).pathname.split("/")[4];
  expect(/^[0-9a-f-]{36}$/.test(electionId), `no election id in ${admin.page.url()}`);
  const ELECTION = `${ORG}/elections/${electionId}`;
  log(`election created (${electionId})`);
  await go(admin, `${ELECTION}/rules/`);
  await admin.page.fill('textarea[name="tie_description"]',
    "Lots are drawn by the Returning Officer at the meeting, before witnesses.");
  await submit(admin);
  expect((await text(admin)).includes("Rules saved"), "rules not saved");
  await noSidewaysScroll(admin, "election page");
  await go(admin, `${ELECTION}/transition/nomination/`);
  await submit(admin);
  log("rules saved, nominations open");

  // 2. Two members nominate themselves.
  for (const key of ["alice", "bob"]) {
    const m = await phone(key);
    await go(m, `${ELECTION}/nominations/new/`);
    await m.page.fill('textarea[name="statement"]', `I will serve the club well. (${key}, fictional)`);
    await submit(m);
    expect((await text(m)).includes("Board election"), `${key}: nomination page did not load`);
  }
  log("two self-nominations submitted");

  // 3. The Returning Officer reviews, approves, submits for approval and schedules.
  const ro = await phone("ro");
  await go(ro, `${ELECTION}/transition/review/`);
  await submit(ro);
  await go(ro, `${ELECTION}/nominations/`);
  const decide = await ro.page.$$eval('main a:text-is("Decide")', (as) => as.map((a) => a.href));
  expect(decide.length === 2, `expected 2 nominations to decide, found ${decide.length}`);
  for (const href of decide) {
    await go(ro, href);
    await ro.page.check('input[name="decision"][value="approve"]');
    await submit(ro);
    expect((await text(ro)).includes("Decision recorded"), "nomination decision not recorded");
  }
  await go(ro, `${ELECTION}/transition/ready_for_approval/`);
  await submit(ro);
  expect((await text(ro)).includes("Awaiting approval"),
    `not submitted for approval: ${(await text(ro)).slice(0, 300)}`);
  await go(ro, `${ELECTION}/transition/scheduled/`);
  for (const key of ["admin", "auditor"]) {
    await ro.page.check(`input[name="keyholders"][value="${seed.people[key].id}"]`);
  }
  await submit(ro);
  expect((await text(ro)).toLowerCase().includes("backup"), "no backup code page after scheduling");
  log("nominations approved; scheduled with the key ceremony");

  // 4. Time passes: the scheduler opens voting.
  pythonStdin("e2e/clock.py", { E2E_ELECTION: electionId, E2E_MOVE: "open" });
  log(python(["run_scheduler"]));

  // 5. Three members vote on their phones; Carol keeps her receipt code.
  const choices = { alice: "Alice Candidate", bob: "Alice Candidate", carol: "Bob Candidate" };
  let carolCode = "";
  for (const [key, choice] of Object.entries(choices)) {
    const v = await phone(key);
    await go(v, `${ELECTION}/ballot/`);
    await noSidewaysScroll(v, "ballot");
    await v.page.locator("label", { hasText: choice }).locator('input[name="candidate"]').check();
    await submit(v);
    expect((await text(v)).includes(choice), `${key}: review page does not show the choice`);
    await v.page.click('#cast-form button[type="submit"]');
    await v.page.waitForSelector("text=Your ballot was accepted", { timeout: 20000 });
    const code = (await v.page.textContent("#receipt-code")).trim();
    expect(/^[0-9A-Z]{4}(-[0-9A-Z]{4}){3}$/.test(code), `${key}: receipt code missing (${code})`);
    expect(!(await text(v)).includes(choice), `${key}: receipt page reveals the choice`);
    if (key === "carol") carolCode = code;
    await go(v, `${ELECTION}/ballot/`);
    expect((await text(v)).includes("already"), `${key}: second visit does not say already voted`);
  }
  log("three ballots accepted, receipts shown");

  // 6. Time passes: the scheduler closes voting and seals the ballot box.
  pythonStdin("e2e/clock.py", { E2E_ELECTION: electionId, E2E_MOVE: "close" });
  log(python(["run_scheduler"]));

  // 7. The Returning Officer starts the count; two keyholders provide their shares.
  await go(ro, `${ELECTION}/count/start/`);
  await submit(ro);
  for (const key of ["admin", "auditor"]) {
    const k = await phone(key);
    await go(k, `${ELECTION}/count/`);
    await k.page.fill("#id_passphrase", seed.passphrase);
    await submit(k);
  }
  const auditor = phones.auditor;
  expect((await text(auditor)).includes("Counted (version 1)"), "the count did not run");
  log("counted with two keyholders");

  // 8. Certify, then publish.
  await go(ro, `${ELECTION}/count/`);
  const countPage = await text(ro);
  expect(countPage.includes("Alice Candidate") && countPage.includes("Review and certify"),
    "count page does not offer certification");
  await go(ro, `${ELECTION}/certify/`);
  await submit(ro);
  expect((await text(ro)).includes("Result certified"), "not certified");
  await go(admin, `${ELECTION}/publish/`);
  await submit(admin);
  expect((await text(admin)).includes("published"), "not published");
  log("certified and published");

  // 9. A member sees the result and checks her receipt in the browser.
  const carol = phones.carol;
  await go(carol, `${ELECTION}/results/`);
  await noSidewaysScroll(carol, "results");
  const results = await text(carol);
  expect(/Alice Candidate[\s\S]*2[\s\S]*Elected/.test(results), "results do not show Alice elected with 2 votes");
  await go(carol, `${ELECTION}/receipts/`);
  await carol.page.fill("#receipt-code-input", carolCode);
  await carol.page.click("#receipt-check button");
  await carol.page.waitForFunction(() => document.getElementById("receipt-result").textContent.length > 0);
  expect((await carol.page.textContent("#receipt-result")).startsWith("Found"), "receipt not found");
  log("results visible; receipt found");

  // 10. The Auditor verifies the audit chain and the election's fingerprints.
  python(["run_scheduler"]);  // chains and anchors the latest events
  await go(auditor, `${BASE}/o/${seed.org}/audit/`);
  await submit(auditor, 'main form[method="post"] button');
  expect((await text(auditor)).includes("Audit chain verified"), "audit chain does not verify");
  await go(auditor, `${ELECTION}/checks/`);
  expect((await text(auditor)).includes("All checks passed"), "election checks failed");
  log("audit chain verified; all election checks passed");

  expect(errors.length === 0, `browser errors: ${errors.join(" | ")}`);
  console.log(`PASS ${browserName} (${device.viewport.width}x${device.viewport.height}, ` +
              `${((Date.now() - started) / 1000).toFixed(0)}s)`);
} catch (error) {
  console.error(`FAIL ${browserName}: ${error.message}`);
  for (const [key, p] of Object.entries(phones)) {
    try { await p.page.screenshot({ path: `e2e-failure-${browserName}-${key}.png`, fullPage: true }); } catch { /* ignore */ }
  }
  if (errors.length) console.error(`browser errors: ${errors.join(" | ")}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
