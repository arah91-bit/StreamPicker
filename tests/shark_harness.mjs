/* Headless harness for the Feeding Frenzy mini-game.
 *
 * The game ships as a JS blob inside app/uitheme.py, which means the only
 * thing a Python test can normally assert is "the source contains a
 * string" — useless for a game whose whole contract is behavioural
 * (does it get harder? can it end? is my best score kept?).
 *
 * So we run the real, shipped script in a vm context against a DOM stub
 * and a virtual clock. Nothing here re-implements game rules: positions,
 * collisions, scoring and difficulty all come from the script itself.
 * Usage: node shark_harness.mjs <path-to-extracted.js>
 */
import { readFileSync } from "node:fs";
import vm from "node:vm";

const W = 1280, H = 800;

/* Seeded PRNG: the game leans on Math.random for spawn mix and position,
   and a flaky game test is worse than none. Same seed → same round. */
function seeded(seed) {
  let s = seed >>> 0;
  const rng = () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
  return Object.assign(Object.create(Math), { random: rng });
}

function sandbox(js, seed = 12345) {
  let now = 0, seq = 0;
  let timers = [], rafs = [];
  const listeners = new Map();          // document-level

  const clock = {
    setTimeout(fn, ms) {
      const id = ++seq;
      timers.push({ id, fn, at: now + (ms || 0), every: 0 });
      return id;
    },
    setInterval(fn, ms) {
      const id = ++seq;
      timers.push({ id, fn, at: now + ms, every: Math.max(1, ms) });
      return id;
    },
    clear(id) { timers = timers.filter((t) => t.id !== id); },
    raf(fn) { const id = ++seq; rafs.push({ id, fn }); return id; },
    craf(id) { rafs = rafs.filter((r) => r.id !== id); },
  };

  const mkStyle = () => {
    const s = { setProperty(k, v) { s[k] = v; }, removeProperty(k) { delete s[k]; } };
    return s;
  };

  class El {
    constructor(tag) {
      this.tagName = String(tag).toUpperCase();
      this.children = [];
      this.parent = null;
      this.style = mkStyle();
      this.textContent = "";
      this.attrs = {};
      this._cls = new Set();
      this._ev = new Map();
      this.spawnedAt = null;
    }
    get className() { return [...this._cls].join(" "); }
    set className(v) {
      this._cls = new Set(String(v).split(/\s+/).filter(Boolean));
    }
    get classList() {
      const self = this;
      return {
        add: (...c) => c.forEach((x) => self._cls.add(x)),
        remove: (...c) => c.forEach((x) => self._cls.delete(x)),
        contains: (c) => self._cls.has(c),
        toggle: (c, on) => (on ?? !self._cls.has(c)) ? self._cls.add(c) : self._cls.delete(c),
      };
    }
    get isConnected() { return !!this.parent; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] ?? null; }
    appendChild(c) {
      c.parent = this;
      if (c._cls.has("prey") || c._cls.has("bub")) c.spawnedAt = now;
      this.children.push(c);
      return c;
    }
    remove() {
      if (!this.parent) return;
      this.parent.children = this.parent.children.filter((c) => c !== this);
      this.parent = null;
    }
    addEventListener(t, fn) {
      if (!this._ev.has(t)) this._ev.set(t, []);
      this._ev.get(t).push(fn);
    }
    fire(t, ev = {}) {
      (this._ev.get(t) || []).forEach((fn) =>
        fn({ preventDefault() {}, target: this, ...ev }));
    }
    /* selectors we actually use: ".cls" and ".hbar i" */
    matches(sel) {
      if (sel.startsWith(".")) {
        return sel.slice(1).split(".").every((c) => this._cls.has(c));
      }
      return this.tagName === sel.toUpperCase();
    }
    descendants() {
      return this.children.flatMap((c) => [c, ...c.descendants()]);
    }
    querySelectorAll(sel) {
      const parts = sel.trim().split(/\s+/);
      let pool = this.descendants();
      for (const p of parts) pool = pool.filter((e) => e.matches(p));
      if (parts.length === 2) {
        // ".hbar i" — descendants of a matching ancestor
        pool = this.descendants().filter((e) => e.matches(parts[1]) &&
          e.parent && e.parent.matches(parts[0]));
      }
      return pool;
    }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    getBoundingClientRect() {
      /* prey drift bottom → top over their animationDuration; the shark is
         wherever the game's own transform put him. Everything the game
         reads about position funnels through here. */
      if (this._cls.has("prey") || this._cls.has("bub")) {
        const dur = parseFloat(this.style.animationDuration || "6") * 1000;
        const t = Math.min(1, (now - (this.spawnedAt ?? now)) / dur);
        const x = (parseFloat(this.style.left || "50") / 100) * W;
        const y = (H + 30) - t * (H + 150);
        return { left: x - 15, top: y, width: 30, height: 30 };
      }
      if (this === pet) {
        const m = /translate3d\((-?[\d.]+)px,\s*(-?[\d.]+)px/.exec(
          this.style.transform || "");
        if (m) return { left: +m[1], top: +m[2], width: 54, height: 54 };
        return { left: W - 100, top: H - 70, width: 52, height: 52 };
      }
      return { left: 0, top: 0, width: 0, height: 0 };
    }
  }

  const mk = (id, cls) => {
    const e = new El("div");
    e.id = id;
    if (cls) e.className = cls;
    return e;
  };
  const fx = mk("sharkfx"), shield = mk("sharkshield"), hud = mk("sharkhud");
  const pet = new El("button");
  pet.id = "sharkpet";
  pet.appendChild(new El("span"));
  const body = mk("body");
  const docEl = mk("html");
  docEl.dataset = { theme: "shark" };

  /* HUD readouts the game writes into */
  for (const cls of ["hscore", "hcombo", "hbest", "hlives", "hlevel", "hquit",
                     "htitle"]) {
    hud.appendChild(mk(null, cls));
  }
  const bar = mk(null, "hbar");
  bar.appendChild(new El("i"));
  hud.appendChild(bar);

  /* count every prey the game ever spawns — measuring difficulty off the
     DOM would just measure how fast the shark eats them */
  let spawnCount = 0;
  const fxAppend = fx.appendChild.bind(fx);
  fx.appendChild = (c) => {
    if (c._cls.has("prey")) spawnCount++;
    return fxAppend(c);
  };

  const byId = { sharkfx: fx, sharkpet: pet, sharkhud: hud, sharkshield: shield };
  const store = new Map();

  const document = {
    documentElement: docEl,
    body,
    hidden: false,
    getElementById: (id) => byId[id] || null,
    createElement: (t) => new El(t),
    querySelector: () => null,
    addEventListener(t, fn) {
      if (!listeners.has(t)) listeners.set(t, []);
      listeners.get(t).push(fn);
    },
    fire(t, ev = {}) {
      (listeners.get(t) || []).forEach((fn) =>
        fn({ preventDefault() {}, target: body, ...ev }));
    },
  };

  const ctx = vm.createContext({
    document,
    window: {},
    innerWidth: W,
    innerHeight: H,
    Math: seeded(seed),
    Date,
    JSON,
    console,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    setTimeout: clock.setTimeout,
    setInterval: clock.setInterval,
    clearTimeout: clock.clear,
    clearInterval: clock.clear,
    requestAnimationFrame: clock.raf,
    cancelAnimationFrame: clock.craf,
  });
  vm.runInContext(js, ctx);

  const api = {
    get now() { return now; },
    doc: document,
    fx, pet, hud, shield,
    hi: () => +(store.get("sp-shark-hi") || 0),
    hudNum: (cls) => {
      const v = hud.querySelector("." + cls)?.textContent ?? "";
      const m = /(-?\d+)/.exec(String(v));
      return m ? +m[1] : 0;
    },
    teeth: () => [...String(hud.querySelector(".hlives")?.textContent || "")]
      .filter((c) => c === "\u{1F9B7}").length,
    spawns: () => spawnCount,
    running: () => hud.classList.contains("on"),
    prey: (kind) => fx.querySelectorAll(".prey").filter((p) =>
      !p.classList.contains("eaten") &&
      (kind === "any" || (kind === "junk") === p.classList.contains("junk"))),
    spawned: 0,
    start() { pet.fire("click"); },
    quit() { hud.querySelector(".hquit").fire("click"); },
    steer(x, y) { shield.fire("pointerdown", { clientX: x, clientY: y, pointerType: "mouse" }); },
    hide() { document.hidden = true; document.fire("visibilitychange"); },
    show() { document.hidden = false; document.fire("visibilitychange"); },
    advance(ms) {
      const end = now + ms;
      while (now < end) {
        now = Math.min(end, now + 16);
        for (;;) {
          const due = timers.filter((t) => t.at <= now)
            .sort((a, b) => a.at - b.at)[0];
          if (!due) break;
          if (due.every) due.at = now + due.every;
          else timers = timers.filter((t) => t !== due);
          due.fn();
        }
        if (!document.hidden) {
          const batch = rafs; rafs = [];
          batch.forEach((r) => r.fn(now));
        }
      }
    },
    /* A stand-in for a decent human: chase the nearest fish, but break off
       to dodge junk that gets close, and ignore fish sitting right next to
       junk. Pass "junk" to play badly on purpose. Returns prey spawned. */
    play(ms, target = "fish") {
      const end = now + ms;
      const from = spawnCount;
      const mid = (r) => [r.left + r.width / 2, r.top + r.height / 2];
      const clampX = (v) => Math.max(60, Math.min(W - 60, v));
      const clampY = (v) => Math.max(60, Math.min(H - 60, v));
      while (now < end) {
        const [sx, sy] = mid(pet.getBoundingClientRect());
        const junk = api.prey("junk").map((p) => mid(p.getBoundingClientRect()));
        if (target !== "junk") {
          let near = null, nd = Infinity;
          for (const j of junk) {
            const d = (j[0] - sx) ** 2 + (j[1] - sy) ** 2;
            if (d < nd) { nd = d; near = j; }
          }
          if (near && nd < 150 ** 2) {          // too close — swim off it
            api.steer(clampX(sx - (near[0] - sx) * 2),
                      clampY(sy - (near[1] - sy) * 2));
            api.advance(120);
            continue;
          }
        }
        let best = null, bd = Infinity;
        for (const p of api.prey(target === "junk" ? "junk" : "fish")) {
          const [cx, cy] = mid(p.getBoundingClientRect());
          if (cy < 40 || cy > H - 20) continue;      // unreachable this pass
          if (target !== "junk" &&
              junk.some((j) => (j[0] - cx) ** 2 + (j[1] - cy) ** 2 < 95 ** 2)) {
            continue;                                // guarded by junk
          }
          const d = (cx - sx) ** 2 + (cy - sy) ** 2;
          if (d < bd) { bd = d; best = [cx, cy]; }
        }
        if (best) api.steer(best[0], best[1]);
        api.advance(120);
      }
      return spawnCount - from;
    },
  };
  return api;
}

const js = readFileSync(process.argv[2], "utf8");
const out = {};

/* ── 1. a competent run outlives any plausible round timer ────────────── */
{
  const g = sandbox(js);
  g.start();
  out.startedRunning = g.running();
  const marks = [];
  for (let i = 0; i < 8 && g.running(); i++) {          // 8 × 15s
    g.play(15000);
    marks.push({ at: g.now / 1000, score: g.hudNum("hscore"),
                 level: g.hudNum("hlevel"), teeth: g.teeth() });
  }
  out.feed = {
    running: g.running(), marks,
    elapsedSeconds: g.now / 1000,
    score: g.hudNum("hscore"), level: g.hudNum("hlevel"),
  };
}

/* ── 2. difficulty is unbounded: prey per second keeps climbing ───────── */
{
  const g = sandbox(js);
  g.start();
  const windows = [];
  for (let i = 0; i < 8 && g.running(); i++) {
    const level = g.hudNum("hlevel");
    const spawned = g.play(10000);
    windows.push({ level, perSecond: +(spawned / 10).toFixed(2) });
  }
  out.ramp = { windows };
}

/* ── 3. it can actually end: eat junk, lose all three teeth ───────────── */
{
  const g = sandbox(js);
  g.start();
  const teeth0 = g.teeth();
  g.play(4000);                        // bank a few points first
  const scored = g.hudNum("hscore");
  for (let i = 0; i < 40 && g.running(); i++) g.play(1500, "junk");
  out.lose = {
    teeth0, scored, running: g.running(), best: g.hi(),
    preyLeft: g.prey("any").length,
  };
}

/* ── 4. quitting still banks your best ────────────────────────────────── */
{
  const g = sandbox(js);
  g.start();
  g.play(12000);
  const score = g.hudNum("hscore");
  g.quit();
  out.quit = { score, best: g.hi(), running: g.running() };
}

/* ── 5. a second run below your best must not lower it ────────────────── */
{
  const g = sandbox(js);
  g.start(); g.play(15000);
  const first = g.hudNum("hscore");
  g.quit();
  const banked = g.hi();
  g.start(); g.play(1200); g.quit();   // a deliberately poor run
  out.keepsBest = { first, banked, after: g.hi() };
}

/* ── 6. a hidden tab pauses instead of filling the sea ────────────────── */
{
  const g = sandbox(js);
  g.start();
  g.play(6000);
  const beforeHide = g.prey("any").length;
  g.hide();
  const clearedOnHide = g.prey("any").length;
  const spawnsAtHide = g.spawns();
  g.advance(20000);
  out.pause = {
    beforeHide, clearedOnHide,
    spawnedWhileHidden: g.spawns() - spawnsAtHide,
    pausedFlag: g.hud.classList.contains("paused"),
  };
  g.show();
  g.advance(3000);
  out.pause.spawnedAfterShow = g.spawns() - spawnsAtHide;
  out.pause.resumedFlag = g.hud.classList.contains("paused");
  out.pause.stillRunning = g.running();
}

/* ── 7. keystrokes meant for a form are left alone ────────────────────── */
{
  const g = sandbox(js);
  g.start();
  g.play(2000);
  let prevented = 0;
  const send = (tag) => g.doc.fire("keydown", {
    key: "a",
    target: { tagName: tag, isContentEditable: false },
    preventDefault() { prevented++; },
  });
  send("INPUT"); send("TEXTAREA"); send("SELECT");
  out.keys = { preventedInFields: prevented };
  send("DIV");
  out.keys.preventedOnPage = prevented;
}

console.log(JSON.stringify(out, null, 1));
