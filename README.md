---
title: Video Resolve
emoji: ⬇️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# Video Resolve

Tiny stateless service behind John's iOS Shortcut: give it a public video link,
it returns a direct media URL (via yt-dlp) for the Shortcut to download and
save to Photos/Files. No files are stored on the server.

`POST /resolve` with `{"url": "..."}` and header `X-API-Key: <API_KEY secret>`.
`GET /` is a health check.
