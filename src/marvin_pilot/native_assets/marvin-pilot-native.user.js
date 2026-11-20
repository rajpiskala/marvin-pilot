// ==UserScript==
// @name         Marvin Pilot - Native Bridge (Experimental)
// @namespace    https://github.com/rajpiskala/marvin-pilot
// @version      0.1.0
// @description  Apply a reviewed Marvin Pilot plan in this Marvin tab; no task cache.
// @match        https://app.amazingmarvin.com/*
// @noframes
// @run-at       document-idle
// @sandbox      JavaScript
// @grant        unsafeWindow
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @downloadURL  none
// @updateURL    none
// ==/UserScript==

(() => {
  "use strict";
  const endpoint = "http://127.0.0.1:48631";
  const origin = "https://app.amazingmarvin.com";
  const supportedBundle = origin + "/v1.71.4/20261024114248_ti4rjt.js";
  const codeKey = "pilotNativePairing";
  const tab = crypto.randomUUID();
  const page = unsafeWindow;
  const asPage = value => typeof cloneInto === "function" ? cloneInto(value, page) : value;
  const exportToPage = fn => typeof exportFunction === "function" ? exportFunction(fn, page) : fn;
  const fail = () => { throw new Error("Marvin native bridge refused the command"); };
  let runtime;

  GM_registerMenuCommand("Pair with Marvin Pilot", () => {
    const code = prompt("Paste the code printed by 'marvin-pilot native pair'.")?.trim();
    if (code && /^[A-Za-z0-9_-]{43}$/.test(code)) GM_setValue(codeKey, code);
  });
  GM_registerMenuCommand("Forget Marvin Pilot pairing", () => GM_setValue(codeKey, ""));

  function discover() {
    if (location.origin !== origin || ![...document.scripts].some(s => s.src === supportedBundle)) {
      return null;
    }
    let req;
    // Ask the already loaded bundle for its module registry. No remote code is loaded.
    const chunk = asPage([["marvin-pilot-native-" + tab], {}]);
    chunk.push(exportToPage(r => { req = r; }));
    page.webpackChunkMarvin?.push(chunk);
    const actions = req?.(37138);
    const tasks = req?.(59120)?.Z;
    const store = req?.(10338)?.Z;
    const types = req?.(74136);
    if (
      !store?.getState || !tasks || !actions?.checkSubscription ||
      types?.NKA !== "UPDATE_TASKS" || types?.Grx !== "DELETE_TASKS"
    ) return null;
    return { actions, tasks, store, types };
  }

  function binding() {
    runtime ||= discover();
    const account = runtime?.store.getState().account;
    const db = runtime?.tasks.db;
    if (
      !account?.userId || !account.email || !account.lastSync || account.readOnlyDB ||
      account.localDbBroken || !db?.allDocs || !db.bulkDocs || db.adapter !== "idb"
    ) return null;
    return {
      ...runtime, db,
      identity: { tab, protocol: 1, userId: String(account.userId), email: account.email },
    };
  }

  const rpc = (path, body, code) => new Promise((resolve, reject) => {
    GM_xmlhttpRequest({
      method: "POST",
      url: endpoint + path,
      headers: { Authorization: "Bearer " + code, "Content-Type": "application/json" },
      data: JSON.stringify(body),
      anonymous: true,
      redirect: "error",
      timeout: 5500,
      onload: response => {
        try { resolve(response.status === 200 ? JSON.parse(response.responseText) : null); }
        catch { reject(); }
      },
      onerror: reject,
      ontimeout: reject,
      onabort: reject,
    });
  });

  function plainTask(doc) {
    return doc?.db === "Tasks" && !doc.done && !doc._deleted && !doc.D &&
      ![
        "recurring", "recurringTaskId", "echo", "habitId", "isPinned", "pinId", "savedItemId",
        "isReward", "rewardId", "calId", "calURL", "etag", "calData", "reminders",
        "reminderOffset", "reminderTime", "reminder", "remindAt", "snooze", "autoSnooze",
        "isTracking", "tracking", "attachments", "_attachments", "shareId", "dependencies",
        "dependedOnBy", "_conflicts",
      ].some(key => Array.isArray(doc[key]) ? doc[key].length : Boolean(doc[key]));
  }

  function requireSameTab(previous, command, code) {
    const current = binding();
    if (
      !current || current.db !== previous.db ||
      current.identity.userId !== previous.identity.userId ||
      current.identity.email !== previous.identity.email ||
      GM_getValue(codeKey, "") !== code || Date.now() >= command.expiresAt
    ) fail();
  }

  const uniqueIds = ids => Array.isArray(ids) && ids.length > 0 && ids.length <= 200 &&
    new Set(ids).size === ids.length && ids.every(id => typeof id === "string" && id.length > 0 && id.length <= 500);

  async function read(db, keys) {
    let timer;
    try {
      return await Promise.race([
        db.allDocs(asPage({ keys, include_docs: true, conflicts: true })),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("Native storage read timed out")), 5000);
        }),
      ]);
    } finally { clearTimeout(timer); }
  }

  function agreeWithApp(b, rows) {
    const state = b.store.getState().tasks;
    for (const row of rows) {
      const live = row.doc, shown = state[row.key];
      if (!live && shown) fail();
      if (live?.db === "Tasks" && !live.done && (
        !shown || ["title", "note", "timeEstimate", "parentId"].some(key => shown[key] !== live[key])
      )) fail();
    }
  }

  async function execute(b, command, code) {
    requireSameTab(b, command, code);
    if (command.method === "ping") {
      const probe = await read(b.db, ["_local/marvin-pilot-readiness"]);
      requireSameTab(b, command, code);
      if (!Array.isArray(probe.rows) || probe.rows.length !== 1) fail();
      return { ready: true };
    }
    if (command.method === "read") {
      if (!uniqueIds(command.data?.keys)) fail();
      const snapshot = await read(b.db, command.data.keys);
      requireSameTab(b, command, code);
      agreeWithApp(b, snapshot.rows);
      return { rows: snapshot.rows };
    }
    if (command.method !== "write") fail();
    const docs = command.data?.docs;
    if (!Array.isArray(docs) || !uniqueIds(docs.map(d => d?._id))) fail();
    if (!await b.actions.checkSubscription(b.store.getState(), b.store.dispatch)) fail();
    const ids = docs.map(d => d._id);
    const snapshot = await read(b.db, ids);
    requireSameTab(b, command, code);
    const current = new Map(snapshot.rows.map(row => [row.key, row]));
    if (current.size !== docs.length) fail();
    const input = [];
    const rejected = [];
    const ranks = new Map();
    let rank = 0;
    if (docs.some(doc => !doc._rev)) {
      for (const task of Object.values(b.store.getState().tasks)) {
        if (Number.isFinite(task.masterRank)) {
          ranks.set(task.parentId, Math.max(ranks.get(task.parentId) ?? 0, task.masterRank));
        }
        if (task.day === "unassigned" && Number.isFinite(task.rank)) rank = Math.max(rank, task.rank);
      }
    }
    for (const doc of docs) {
      const existing = current.get(doc._id);
      if (!existing) fail();
      const old = existing.doc || null;
      if (old && !plainTask(old)) fail();
      if ((old?._rev ?? null) !== (doc._rev ?? null)) {
        rejected.push({ id: doc._id, error: "conflict" });
      } else if (doc._deleted === true) {
        if (!old || doc.db !== "Tasks" || doc.D !== 1) fail();
        input.push({ _id: doc._id, _rev: doc._rev, _deleted: true, db: "Tasks", D: 1 });
      } else {
        if (!plainTask(doc) || typeof doc.title !== "string") fail();
        if (!old && !existing.value?.deleted) {
          const masterRank = (ranks.get(doc.parentId) ?? 0) + 1;
          ranks.set(doc.parentId, masterRank);
          input.push({
            dueDate: null, firstScheduled: doc.day, rank: ++rank, masterRank, ...doc,
          });
        } else input.push(doc);
      }
    }
    if (!input.length) return rejected;
    requireSameTab(b, command, code);
    // Native PouchDB is already owned and open by Marvin. Its CAS checks also
    // catch races after the fresh keyed read. Do not touch raw browser storage.
    const rows = await b.db.bulkDocs(asPage(input));
    if (!Array.isArray(rows) || rows.length !== input.length) fail();
    const byId = new Map(rows.map(row => [row.id, row]));
    if (byId.size !== input.length || input.some(doc => !byId.has(doc._id))) fail();
    const saved = input.filter(doc => byId.get(doc._id).ok === true);
    if (saved.length) {
      const verified = await read(b.db, saved.map(doc => doc._id));
      requireSameTab(b, command, code);
      const refresh = [], removed = [];
      for (const row of verified.rows) {
        if (row.value?.rev !== byId.get(row.key)?.rev) fail();
        if (row.value.deleted) removed.push(row.key);
        else if (row.doc?._rev === row.value.rev) refresh.push(row.doc);
        else fail();
      }
      if (removed.length) b.store.dispatch(asPage({type:b.types.Grx, taskIds:removed}));
      if (refresh.length) b.store.dispatch(asPage({
        type:b.types.NKA, tasks:refresh, setter:{parentId:true},
      }));
      agreeWithApp(b, verified.rows);
    }
    return [...rows, ...rejected];
  }

  async function poll() {
    let delay = 1500;
    try {
      const code = GM_getValue(codeKey, "");
      if (!code) return;
      const b = binding();
      if (!b) return;
      const response = await rpc("/next", b.identity, code);
      if (!response) return;
      delay = 0;
      const command = response.command;
      if (!command) return;
      let data, ok = false;
      try { data = await execute(b, command, code); ok = true; }
      catch { /* A claimed write is never retried; the CLI preserves its journal. */ }
      await rpc("/reply", { ...b.identity, id: command.id, ok, data }, code);
    } catch { /* No listener is normal when Pilot is not applying a plan. */ }
    finally {
      // While connected, each long poll already yields the browser thread.
      // Do not put every request behind throttled background-tab timers.
      if (delay) setTimeout(poll, delay);
      else void poll();
    }
  }
  void poll();
})();
