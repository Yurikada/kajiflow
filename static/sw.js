// KajiFlow service worker: 静的アセットの cache-first のみ。API はキャッシュしない。
// キャッシュ返却後にバックグラウンドでネットワーク更新する（stale-while-revalidate）
// ため、sw.js を変更しなくても静的アセットの更新は次回以降のロードで反映される。
// キャッシュ構造を変える場合は CACHE_NAME のバージョンを上げること
// （activate で旧バージョンのキャッシュは削除される）。

"use strict";

const CACHE_NAME = "kajiflow-static-v6";
const ASSETS = [
  "/",
  "/index.html",
  "/today.html",
  "/vault.html",
  "/manage.html",
  "/app.css",
  "/common.js",
  "/index.js",
  "/today.js",
  "/vault.js",
  "/manage.js",
  "/manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // API・非GET・別オリジンはキャッシュ対象外（常にネットワーク）
  if (
    event.request.method !== "GET" ||
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/api/") ||
    url.pathname === "/health"
  ) {
    return;
  }

  // 静的アセットは cache-first + バックグラウンド再検証（stale-while-revalidate）。
  // キャッシュがあれば即返し、裏でネットワーク取得してキャッシュを更新する。
  event.respondWith(
    caches.match(event.request).then((cached) => {
      // 再検証は HTTP キャッシュを条件付き検証にする（cache: "no-cache"）。
      // 既定だとヒューリスティック鮮度で HTTP キャッシュが旧内容を返し、
      // 旧ファイルを再キャッシュし続けて更新が届かない。
      // navigation リクエストは init 付き再構築ができないため URL で fetch する。
      const fetched = fetch(event.request.url, { cache: "no-cache" })
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return res;
        })
        .catch((err) => {
          // オフライン等: キャッシュがあればそれで応答済み。なければエラーを伝播。
          if (cached) return cached;
          throw err;
        });
      if (cached) {
        // 背景更新の失敗でコンソールに未処理拒否を出さない
        fetched.catch(() => {});
        return cached;
      }
      return fetched;
    })
  );
});
