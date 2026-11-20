# Experimental native bridge

Use this for an opt-in, **local** apply in an already-open Marvin web tab. The
ordinary API remains the default; [experimental DB mode](experimental-database.md)
is the browser-independent option for bulk, cloud-verified changes.

1. Install `src/marvin_pilot/native_assets/marvin-pilot-native.user.js` in
   Tampermonkey using its normal install screen. It runs only on
   `https://app.amazingmarvin.com/` and sends requests only to Pilot on
   `127.0.0.1:48631`. It does not read plan files, sync credentials, or monitor a
   directory.
2. From a trusted terminal, run `marvin-pilot native pair`. In the Tampermonkey
   menu on Marvin, choose **Pair with Marvin Pilot** and paste the code once.
   Pilot stores its copy in the OS keyring. Treat it as a local credential;
   rerunning the command rotates it. Use **Forget Marvin Pilot pairing** to
   disconnect the tab.
3. Leave the paired tab open with the intended account signed in. Apply a normal,
   account-pinned plan:

   ```console
   marvin-pilot native probe plan.json
   marvin-pilot apply --native plan.json
   marvin-pilot revert --native path/to/native-receipt.json
   ```

The optional `probe` command checks pairing, account identity and access to
Marvin's local database without applying anything.

The usual approval and unattended-impact policy still applies. Native apply
opens a loopback listener only for the command, binds to one authenticated tab
with the exact expected Marvin account, and hands it one bounded request at a
time. It journals full before-images before sending. It checks document revisions
and verifies both the local store and Marvin's app state. It never silently
switches to the cloud or replays a write with an uncertain outcome. Use the
receipt when resolving interruptions.

Initial scope is plain, open tasks: create with optional existing parent,
rename, edit notes or estimate, and delete/recover via Pilot receipts. No project
writes, moves, completion, scheduled changes, external strategy side effects,
linked tasks, recurrence, attachments, reminders, or plan sets. Native deletes
do **not** appear in Marvin's in-app Trash. Revert works on native receipts and
still asks for human approval. A local receipt confirms local persistence,
**not** Cloudant sync or visibility on other devices.

The userscript is pinned to the tested Marvin 1.71.4 web build. An app update or
a browser that cannot access its app runtime leaves native mode unavailable; the
ordinary API and cloud DB commands still work. Never relax browser security to
force the bridge to run. For early testing, back up your data and start on your
developer account.
