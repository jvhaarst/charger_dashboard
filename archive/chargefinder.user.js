// ==UserScript==
// @name         ChargeFinder live bridge
// @namespace    local.chargefinder.viewer
// @version      1.0
// @description  Lets the local viewer page pull ChargeFinder data itself, with no proxy.
// @author       -
// @match        file:///*chargefinder-viewer.html*
// @match        http://localhost:*/chargefinder-viewer.html*
// @match        http://127.0.0.1:*/chargefinder-viewer.html*
// @connect      api.chargefinder.com
// @grant        GM_xmlhttpRequest
// @run-at       document-start
// ==/UserScript==

/*
 * Why this works where the page alone doesn't: api.chargefinder.com returns 403
 * to any browser request carrying a foreign Origin header. GM_xmlhttpRequest is
 * issued from the extension rather than the page, so no Origin is attached and
 * the API answers normally.
 *
 * Install Tampermonkey (or Violentmonkey), add this script, and — for file://
 * pages — enable "Allow access to file URLs" for the extension in chrome://extensions.
 * The viewer picks the bridge up automatically and refreshes itself.
 */

(function () {
  "use strict";

  const API = "https://api.chargefinder.com";
  const KEY = "9ac6af64f912e44291c7989bb7da774a";

  const hexToBuf = (hex) =>
    new Uint8Array(hex.match(/[\da-fA-F]{2}/g).map((h) => parseInt(h, 16))).buffer;

  async function decrypt(payload) {
    if (!payload || !payload.e) return payload;
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(KEY),
      "AES-GCM",
      true,
      ["decrypt"]
    );
    const buf = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: hexToBuf(payload.i) },
      key,
      hexToBuf(payload.e + payload.a)
    );
    const bytes = new Uint8Array(buf);
    if (bytes[0] === 0x78) {
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate"));
      return JSON.parse(await new Response(stream).text());
    }
    return JSON.parse(new TextDecoder().decode(bytes));
  }

  function raw(path) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "GET",
        url: API + path,
        headers: { Accept: "application/json" },
        onload: (res) => {
          if (res.status !== 200) {
            reject(new Error("HTTP " + res.status + " for " + path));
            return;
          }
          try {
            resolve(JSON.parse(res.responseText));
          } catch (err) {
            reject(err);
          }
        },
        onerror: () => reject(new Error("network error for " + path)),
        ontimeout: () => reject(new Error("timeout for " + path)),
      });
    });
  }

  const bridge = async (path) => decrypt(await raw(path));

  // unsafeWindow reaches the page's own window; fall back for managers that
  // already run the script in the page context.
  const target = typeof unsafeWindow !== "undefined" ? unsafeWindow : window;
  target.cfLiveFetch = bridge;
})();
