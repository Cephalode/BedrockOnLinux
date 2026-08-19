/* BedrockOnLinux — site behaviour.
   One job: point the download buttons at the current release. Progressive —
   with JavaScript off, every button already links to the releases page. */

(function () {
  "use strict";

  /* ---- download buttons ------------------------------------------------ */

  var API = "https://api.github.com/repos/Wyze3306/BedrockOnLinux/releases/latest";
  var CACHE_KEY = "bol-latest-release";
  var CACHE_MS = 6 * 60 * 60 * 1000;

  var PATTERNS = {
    appimage: /\.AppImage$/i,
    deb: /\.deb$/i,
    rpm: /\.rpm$/i,
    flatpak: /\.flatpak$/i,
    pyz: /\.pyz$/i
  };

  function size(bytes) {
    if (bytes < 1048576) return Math.round(bytes / 1024) + " KB";
    return (bytes / 1048576).toFixed(bytes < 10485760 ? 1 : 0) + " MB";
  }

  function apply(release) {
    if (!release || !release.assets) return;

    var found = {};
    release.assets.forEach(function (asset) {
      Object.keys(PATTERNS).forEach(function (kind) {
        if (!found[kind] && PATTERNS[kind].test(asset.name)) found[kind] = asset;
      });
    });

    document.querySelectorAll("[data-asset]").forEach(function (link) {
      var asset = found[link.getAttribute("data-asset")];
      if (!asset) return;
      link.href = asset.browser_download_url;
      var label = link.querySelector(".asset-size");
      if (label) label.textContent = size(asset.size);
    });

    var version = (release.tag_name || "").replace(/^v/, "");
    var main = document.getElementById("dl-main");
    var sub = document.getElementById("dl-main-sub");
    if (main && found.appimage) main.href = found.appimage.browser_download_url;
    if (sub && found.appimage) {
      sub.textContent = "AppImage · " + size(found.appimage.size) +
        (version ? " · " + version : "");
    }
  }

  function load() {
    try {
      var cached = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
      if (cached && Date.now() - cached.at < CACHE_MS) {
        apply(cached.release);
        return;
      }
    } catch (e) { /* no cache, ask the API */ }

    fetch(API, { headers: { Accept: "application/vnd.github+json" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (release) {
        apply(release);
        try {
          localStorage.setItem(CACHE_KEY,
            JSON.stringify({ at: Date.now(), release: release }));
        } catch (e) { /* private mode: fine, we just re-fetch next time */ }
      })
      .catch(function () { /* links already point at /releases/latest */ });
  }

  load();
})();
