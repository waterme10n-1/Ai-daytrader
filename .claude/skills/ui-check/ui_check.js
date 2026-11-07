#!/usr/bin/env node
// Starter appen med en tom database og uden nøgler, åbner /meme og /desk i Playwright
// i telefon- og computerstørrelse, tager skærmbilleder og melder fejl.
//
//   node .claude/skills/ui-check/ui_check.js [--out MAPPE] [--alle-looks] [--hele-siden]
//
// Exit 0: ingen fejl. Exit 1: JS-fejl, HTTP-fejl fra appen, vandret scroll eller felter,
// der stadig indlæser. Skærmbillederne ligger i --out (standard: <tmp>/ui-check).

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..", "..");
const args = process.argv.slice(2);
const flag = (name) => args.includes(name);
const option = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const OUT = path.resolve(option("--out", path.join(os.tmpdir(), "ui-check")));
const ALL_LOOKS = flag("--alle-looks");
const FULL_PAGE = flag("--hele-siden");

let chromium, devices;
try {
  ({ chromium, devices } = require("playwright"));
} catch (e) {
  console.error("Playwright mangler. Installér det med `npm i -g playwright` og `npx playwright install chromium`.");
  process.exit(2);
}

function pythonBin() {
  for (const p of [path.join(ROOT, ".venv", "bin", "python"), path.join(ROOT, "venv", "bin", "python")]) {
    if (fs.existsSync(p)) return p;
  }
  for (const p of ["python3", "python"]) {
    if (spawnSync(p, ["--version"]).status === 0) return p;
  }
  return "python3";
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

async function waitForHealth(base, server, log) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (server.exitCode !== null) throw new Error("Serveren stoppede ved start:\n" + fs.readFileSync(log, "utf8").slice(-3000));
    try {
      const res = await fetch(base + "/healthz");
      if (res.ok) return;
    } catch (e) { /* ikke oppe endnu */ }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error("Serveren svarede ikke på /healthz inden for 30 sekunder. Se " + log);
}

function pages() {
  const list = [];
  const skins = ALL_LOOKS ? ["neon", "volt", "is", "rav", "papir"] : ["neon"];
  for (const skin of skins) list.push({ name: `meme-${skin}`, url: `/meme?skin=${skin}` });
  const themes = ALL_LOOKS ? ["dark", "light"] : ["dark"];
  for (const market of ["us", "dk"]) {
    // Siden vælger børs med ?m=us/dk (API-kaldene bruger ?market=dk).
    for (const theme of themes) list.push({ name: `desk-${market}${theme === "light" ? "-lys" : ""}`, url: `/desk?m=${market}`, deskTheme: theme, expect: market === "dk" ? "Danmark" : "USA" });
  }
  return list;
}

const SIZES = [
  { name: "telefon", options: { ...devices["iPhone 13"] } },
  { name: "computer", options: { viewport: { width: 1280, height: 800 } } },
];

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "ui-check-db-"));
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const log = path.join(OUT, "server.log");

  // Tom database, ingen nøgler og intet login: siden må aldrig kunne bruge rigtige konti eller koste penge.
  const blank = [
    "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SOLANA_PRIVATE_KEY", "MEME_WITHDRAW_ADDRESS",
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "DESK_ALPACA_API_KEY", "DESK_ALPACA_SECRET_KEY", "X_BEARER_TOKEN",
    "APP_USERNAME", "APP_PASSWORD", "APP_PASSWORD_HASH", "APP_TOTP_SECRET", "SESSION_SECRET",
  ];
  const env = { ...process.env, HOST: "127.0.0.1", PORT: String(port), MEME_ALLOW_LIVE: "false",
    DB_PATH: path.join(work, "trader.db"), MEME_DB_PATH: path.join(work, "meme.db"),
    DESK_DB_PATH: path.join(work, "desk.db"), DESK_DK_DB_PATH: path.join(work, "desk_dk.db") };
  for (const key of blank) env[key] = "";

  const logFd = fs.openSync(log, "w");
  const server = spawn(pythonBin(), ["run.py"], { cwd: ROOT, env, stdio: ["ignore", logFd, logFd] });
  const results = [];
  let browser;
  try {
    await waitForHealth(base, server, log);
    browser = await chromium.launch();
    for (const size of SIZES) {
      for (const pg of pages()) {
        const context = await browser.newContext(size.options);
        if (pg.deskTheme) await context.addInitScript((t) => { try { localStorage.setItem("desk-theme", t); } catch (e) {} }, pg.deskTheme);
        const page = await context.newPage();
        const problems = [];
        page.on("pageerror", (e) => problems.push("JS-fejl: " + e.message));
        page.on("console", (m) => { if (m.type() === "error") problems.push("Konsol: " + m.text()); });
        page.on("response", (r) => { if (r.url().startsWith(base) && r.status() >= 400) problems.push(`HTTP ${r.status()}: ${r.url().slice(base.length)}`); });
        await page.goto(base + pg.url, { waitUntil: "load" });
        await page.waitForTimeout(3500);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        if (overflow > 0) problems.push(`Vandret scroll: siden er ${overflow}px for bred`);
        if (pg.expect) {
          const title = await page.title();
          if (!title.includes(pg.expect)) problems.push(`Forkert børs: titlen er "${title}", ventede ${pg.expect}`);
        }
        const loading = await page.evaluate(() => [...document.querySelectorAll(".sk")].filter((el) => el.offsetParent !== null).length);
        if (loading > 0) problems.push(`${loading} felter indlæser stadig efter 3,5 s (.sk)`);
        const file = path.join(OUT, `${pg.name}-${size.name}.png`);
        await page.screenshot({ path: file, fullPage: FULL_PAGE });
        results.push({ page: pg.name, size: size.name, file, problems });
        await context.close();
      }
    }
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
    fs.closeSync(logFd);
    fs.rmSync(work, { recursive: true, force: true });
  }

  let failed = 0;
  for (const r of results) {
    const ok = r.problems.length === 0;
    if (!ok) failed += 1;
    console.log(`${ok ? "OK  " : "FEJL"} ${r.page} (${r.size})  ${r.file}`);
    for (const p of r.problems) console.log("       " + p);
  }
  console.log(`\n${results.length - failed} af ${results.length} sider uden fejl. Serverlog: ${log}`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error(e.message || e);
  process.exit(2);
});
