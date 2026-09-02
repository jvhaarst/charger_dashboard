/*
 * ChargeFinder grabber — runs as a bookmarklet ON a chargefinder.com station page.
 *
 * Why it has to run there: api.chargefinder.com answers 403 to any request whose
 * Origin isn't chargefinder.com, so the fetch must happen from that origin. This
 * decrypts the payload (AES-GCM + zlib) and hands you the plain JSON to drop into
 * the viewer page.
 *
 * Install: make a new bookmark, paste the contents of chargefinder-bookmarklet.txt
 * as its URL. Then open any station page on chargefinder.com and click it.
 */
(async () => {
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

  const get = async (path) => {
    const res = await fetch(API + path, { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status + " for " + path);
    return decrypt(await res.json());
  };

  function panel(html) {
    document.getElementById("cf-grab")?.remove();
    const el = document.createElement("div");
    el.id = "cf-grab";
    el.style.cssText =
      "position:fixed;inset:auto 16px 16px auto;z-index:2147483647;width:min(420px,92vw);" +
      "background:#171b21;color:#e8ecf1;border:1px solid #333b45;border-radius:12px;padding:14px;" +
      "font:13px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;" +
      "box-shadow:0 12px 32px rgba(0,0,0,.4)";
    el.innerHTML = html;
    document.body.appendChild(el);
    return el;
  }

  const btn =
    "cursor:pointer;font:inherit;padding:6px 12px;border-radius:7px;border:1px solid #3a444f;" +
    "background:#222932;color:#e8ecf1;margin-right:6px";

  try {
    const slug = location.pathname.split("/").filter(Boolean).pop();
    if (!slug || !/^[a-z0-9]{4,12}$/i.test(slug)) {
      panel("Open a ChargeFinder <b>station page</b> first, then click the bookmarklet.");
      return;
    }

    panel("Fetching <b>" + slug + "</b>…");
    const station = await get("/station/" + slug);
    const status =
      station.realtime && station.realtimeId ? await get("/status/" + station.realtimeId) : [];

    const sample = {
      v: 1,
      t: new Date().toISOString(),
      slug,
      url: location.href,
      station,
      status,
    };
    const json = JSON.stringify(sample);
    const free = status.filter((s) => s.status === 2).length;

    const el = panel(
      "<div style='font-weight:600;margin-bottom:2px'>" +
        station.title +
        "</div>" +
        "<div style='color:#98a2b0;margin-bottom:10px'>" +
        free +
        " of " +
        status.length +
        " connectors available · " +
        new Date().toLocaleTimeString() +
        "</div>" +
        "<button id='cf-copy' style=\"" +
        btn +
        "\">Copy JSON</button>" +
        "<button id='cf-dl' style=\"" +
        btn +
        "\">Download</button>" +
        "<button id='cf-x' style=\"" +
        btn +
        "\">Close</button>" +
        "<textarea id='cf-ta' readonly style='width:100%;height:70px;margin-top:10px;" +
        "background:#0f1216;color:#98a2b0;border:1px solid #333b45;border-radius:7px;padding:6px;" +
        "font:11px ui-monospace,Menlo,monospace'></textarea>"
    );
    el.querySelector("#cf-ta").value = json;

    el.querySelector("#cf-copy").onclick = async () => {
      const ta = el.querySelector("#cf-ta");
      try {
        await navigator.clipboard.writeText(json);
        el.querySelector("#cf-copy").textContent = "Copied ✓";
      } catch {
        ta.select();
        document.execCommand("copy");
        el.querySelector("#cf-copy").textContent = "Copied ✓";
      }
    };
    el.querySelector("#cf-dl").onclick = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([json], { type: "application/json" }));
      a.download = "chargefinder-" + slug + "-" + sample.t.slice(0, 19).replace(/[:T]/g, "") + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
    };
    el.querySelector("#cf-x").onclick = () => el.remove();
  } catch (err) {
    panel("Grab failed: " + (err && err.message ? err.message : err));
  }
})();
