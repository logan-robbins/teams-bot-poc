# Alfred — Teams App Manifest

This directory holds the Teams app sideload package contents. The deployed app is **Alfred** (Entra app id `ff4b0902-5ae8-450b-bf45-7e2338292554`, Azure Bot `alfred-bot-qmachina`).

## Files

| File | Purpose | Spec |
|---|---|---|
| `manifest.json` | Teams app manifest v1.21 | `id` and `bots[0].botId` must match the production Entra app id |
| `color.png` | Full-color icon | 192×192 PNG |
| `outline.png` | Outline icon | 32×32 PNG, transparent background |

`alfred.zip` is the build output (gitignored). Don't commit it.

## Build the sideload package

```bash
cd manifest
zip alfred.zip manifest.json color.png outline.png
```

Files must be at the archive root (no nested directory).

## Upload

See the **Deploy** section of the project root [`README.md`](../README.md) for upload paths (Teams Admin Center vs. single team / chat).

## Replace the icons

```bash
# color.png (192×192, full color)
convert -size 192x192 xc:'#0078D4' -fill white -pointsize 96 -gravity center \
  -annotate +0+0 'A' manifest/color.png

# outline.png (32×32, transparent background, white outline)
convert -size 32x32 xc:none -fill white -pointsize 24 -gravity center \
  -annotate +0+0 'A' manifest/outline.png
```

After regenerating, re-zip and re-upload (Teams treats sideload packages as immutable per upload — bumping `manifest.json:version` is the canonical way to ship a new package).

## Manifest validation checklist

Critical fields for a calling bot:

- `bots[0].supportsCalling: true`
- `bots[0].supportsVideo: false` (audio-only)
- `validDomains` includes `teamsbot.qmachina.com` and `media.qmachina.com`
- `bots[0].botId` and top-level `id` both equal the Entra app id (`ff4b0902-5ae8-450b-bf45-7e2338292554`)

Quick check:

```bash
jq '{id, botId: .bots[0].botId, calling: .bots[0].supportsCalling, video: .bots[0].supportsVideo, validDomains}' manifest/manifest.json
```
