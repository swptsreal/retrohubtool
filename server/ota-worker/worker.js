/**
 * RetroHub OTA Worker
 *
 * Serves the release channel (manifest.json + files/ + catalog/) by proxying
 * the GitHub raw upstream. Caching ignores the app's cache-busting query
 * (?_t=...) so repeated downloads hit Cloudflare instead of GitHub; the
 * manifest is always fetched fresh so a new release is seen immediately.
 *
 * A token-protected /__purge endpoint clears cached files after a release,
 * so a freshly pushed file is not served stale for the rest of its TTL.
 */

const DEFAULT_UPSTREAM = "https://raw.githubusercontent.com/swptsreal/retrohubtool/develop";

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

function decorate(res, isManifest) {
  const headers = new Headers(res.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", isManifest ? "no-store" : "public, max-age=300");
  return new Response(res.body, { status: res.status, headers });
}

function purgeTokenFrom(request) {
  const auth = request.headers.get("authorization") || "";
  return request.headers.get("x-purge-token") || auth.replace(/^Bearer\s+/i, "");
}

async function handlePurge(request, env, url, upstream) {
  try {
    return await runPurge(request, env, url, upstream);
  } catch (e) {
    return jsonResponse({ ok: false, error: String((e && e.stack) || e) }, 500);
  }
}

async function runPurge(request, env, url, upstream) {
  const expected = env.PURGE_TOKEN;
  if (!expected) {
    return jsonResponse({ ok: false, error: "purge disabled (set PURGE_TOKEN)" }, 403);
  }
  if (purgeTokenFrom(request) !== expected) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }

  const cache = caches.default;
  const paths = url.searchParams.getAll("path");
  let targets = [];

  if (paths.length) {
    targets = paths.map((p) => upstream + (p.startsWith("/") ? p : "/" + p));
  } else {
    // No path given: purge every file the manifest references (code + catalog
    // + runtime). Good enough for "clear everything after a release".
    let manifest;
    try {
      const res = await fetch(upstream + "/manifest.json", {
        headers: { "User-Agent": "RetroHub-OTA-Worker" },
      });
      manifest = await res.json();
    } catch (e) {
      return jsonResponse({ ok: false, error: "manifest fetch failed: " + e.message }, 502);
    }
    for (const f of manifest.files || []) {
      if (f && f.path) targets.push(upstream + "/files/" + f.path);
    }
    const cat = manifest.catalog;
    if (cat && cat.url) targets.push(upstream + "/" + String(cat.url).replace(/^\/+/, ""));
    for (const rf of (manifest.runtime && manifest.runtime.files) || []) {
      if (rf && rf.url) targets.push(upstream + "/" + String(rf.url).replace(/^\/+/, ""));
    }
  }

  let deleted = 0;
  for (const target of targets) {
    try {
      const ok = await cache.delete(new Request(target, { method: "GET" }));
      if (ok) deleted++;
    } catch (e) {
      // individual miss/erroneous key: skip
    }
  }
  return jsonResponse({ ok: true, purged: deleted, total: targets.length });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const upstream = (env.UPSTREAM || DEFAULT_UPSTREAM).replace(/\/+$/, "");

    if (url.pathname === "/" || url.pathname === "/health") {
      return jsonResponse({
        ok: true,
        service: "RetroHub OTA",
        upstream,
        purge: Boolean(env.PURGE_TOKEN),
      });
    }

    if (url.pathname === "/__purge") {
      return handlePurge(request, env, url, upstream);
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const target = upstream + url.pathname;
    const isManifest = url.pathname.endsWith("manifest.json");

    const cache = caches.default;
    const cacheKey = new Request(target, { method: "GET" });

    const cacheable = request.method === "GET" && !isManifest;

    if (cacheable) {
      const hit = await cache.match(cacheKey);
      if (hit) return decorate(hit, false);
    }

    const res = await fetch(target, {
      method: request.method,
      headers: { "User-Agent": "RetroHub-OTA-Worker" },
    });

    // Only cache GET responses: caching a HEAD (empty body) under the GET key
    // would poison every later download of that file.
    if (cacheable && res.ok) {
      ctx.waitUntil(cache.put(cacheKey, res.clone()).catch(() => {}));
    }
    return decorate(res, isManifest);
  },
};
