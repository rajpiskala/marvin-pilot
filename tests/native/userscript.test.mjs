import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { randomUUID } from "node:crypto";

const source = readFileSync(new URL(
  "../../src/marvin_pilot/native_assets/marvin-pilot-native.user.js", import.meta.url
), "utf8");
const origin = "https://app.amazingmarvin.com";
const src = origin + "/v1.71.4/20261024114248_ti4rjt.js";
const task = { _id: "old", _rev: "1-old", db: "Tasks", title: "Old", parentId: "unassigned" };

async function run(commands, { before = [task], expectedBundle = src } = {}) {
  const rows = new Map(before.map(document => [document._id, { doc: structuredClone(document) }]));
  const taskState = Object.fromEntries(before.map(document => [document._id, structuredClone(document)]));
  const state = {
    account: { userId: "123", email: "test@example.com", lastSync: 1 },
    tasks: taskState,
  };
  const actions = [];
  let requests = 0, bulkCalls = 0;
  const db = {
    adapter: "idb",
    async allDocs({ keys }) {
      return { rows: keys.map(key => {
        const value = rows.get(key);
        if (!value) return { key, error: "not_found" };
        if (value.deleted) return { key, value: { deleted: true, rev: value.rev } };
        return { key, value: { rev: value.doc._rev }, doc: value.doc };
      }) };
    },
    async bulkDocs(docs) {
      bulkCalls += 1;
      return docs.map(document => {
        const id = document._id, old = rows.get(id);
        if ((old?.doc?._rev ?? null) !== (document._rev ?? null)) {
          return { id, error: "conflict" };
        }
        const rev = "2-written";
        if (document._deleted) rows.set(id, { deleted: true, rev });
        else rows.set(id, { doc: { ...document, _rev: rev } });
        return { id, ok: true, rev };
      });
    },
  };
  const store = {
    getState: () => state,
    dispatch(action) {
      actions.push(action.type);
      if (action.type === "DELETE_TASKS") for (const id of action.taskIds) delete taskState[id];
      if (action.type === "UPDATE_TASKS") for (const item of action.tasks) taskState[item._id] = item;
    },
  };
  const modules = {
    37138: { checkSubscription: async () => true },
    59120: { Z: { db } },
    10338: { Z: store },
    74136: { NKA: "UPDATE_TASKS", Grx: "DELETE_TASKS" },
  };
  const replies = [];
  const done = new Promise(resolve => {
    const context = {
      crypto: { randomUUID },
      location: { origin }, document: { scripts: [{ src: expectedBundle }] },
      unsafeWindow: { webpackChunkMarvin: { push(chunk) {
        chunk[2](id => modules[id]);
      } } },
      GM_getValue: () => "synthetic",
      GM_registerMenuCommand: () => {},
      setTimeout(fn, delay) {
        if (delay === 1500) resolve();
        else return setTimeout(fn, delay);
      },
      clearTimeout,
      GM_xmlhttpRequest({ url, onload, data }) {
        requests += 1;
        if (url.endsWith("/reply")) {
          replies.push(JSON.parse(data));
          onload({ status: 200, responseText: "{}" });
        } else if (!commands.length) onload({ status: 409 });
        else {
          const command = { id: "test", expiresAt: Date.now() + 60_000, ...commands.shift() };
          onload({ status: 200, responseText: JSON.stringify({ command }) });
        }
      },
    };
    runInNewContext(source, context);
  });
  await done;
  return { replies, bulkCalls, state, actions, requests };
}

test("mixed batch persists and refreshes the app once per change kind", async () => {
  const created = { _id: "new", db: "Tasks", title: "New", parentId: "unassigned", day: "unassigned" };
  const { replies, bulkCalls, state, actions } = await run([
    { method: "write", data: { docs: [
      created, { _id: task._id, _rev: task._rev, _deleted: true, db: "Tasks", D: 1 },
    ] } },
    { method: "read", data: { keys: ["old", "new"] } },
  ]);
  assert.equal(bulkCalls, 1);
  assert.deepEqual(actions, ["DELETE_TASKS", "UPDATE_TASKS"]);
  assert.equal(state.tasks.old, undefined);
  assert.equal(state.tasks.new.title, "New");
  assert.equal(replies[0].ok, true);
  assert.deepEqual(replies[1].data.rows.map(r => r.doc?.title ?? null), [null, "New"]);
});

test("stale revisions and linked tasks cannot overwrite real state", async () => {
  const { replies, bulkCalls, state } = await run([
    { method: "write", data: { docs: [{ ...task, _rev: "0-stale", title: "Overwrite" }] } },
    { method: "write", data: { docs: [{ ...task, calId: "calendar" }] } },
  ]);
  assert.equal(bulkCalls, 0);
  assert.equal(replies[0].data[0].error, "conflict");
  assert.equal(replies[1].ok, false);
  assert.equal(state.tasks.old.title, "Old");
});

test("unknown Marvin build makes no localhost request", async () => {
  const result = await run([{ method: "ping" }], { expectedBundle: origin + "/next.js" });
  assert.equal(result.requests, 0);
  assert.equal(result.bulkCalls, 0);
});
