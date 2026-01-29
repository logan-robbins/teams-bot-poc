# Teams App Manifest

## Files Required

- `manifest.json` - App configuration ✅
- `color.png` - 192x192px app icon (required)
- `outline.png` - 32x32px outline icon (required)

## Creating Icon Files

You need to create two PNG images:

### color.png (192x192px)
```bash
# Option 1: Use ImageMagick to create a simple placeholder
convert -size 192x192 xc:'#0078D4' -fill white -pointsize 120 -gravity center -annotate +0+0 'TB' color.png

# Option 2: Use online tool
# Go to https://www.canva.com and create 192x192 image with "TB" text
```

### outline.png (32x32px)
```bash
# Option 1: Use ImageMagick
convert -size 32x32 xc:white -fill '#0078D4' -pointsize 24 -gravity center -annotate +0+0 'TB' outline.png

# Option 2: Use online tool
# Go to https://www.canva.com and create 32x32 image with "TB" text
```

## Before Uploading to Teams

1. **Update manifest.json:**
   - Replace `CHANGE_ME.ngrok-free.app` with your actual ngrok subdomain
   - Replace `0.botpoc.YOURDOMAIN.com` with your actual media domain

2. **Create the ZIP file:**
   ```bash
   cd manifest
   zip teams-bot-poc.zip manifest.json color.png outline.png
   ```

3. **Upload to Teams:**
   - Teams → Apps → Upload a custom app
   - Select `teams-bot-poc.zip`
   - Click Add

## Manifest Validation

The critical fields for calling bots (per S1):
- `bots[0].supportsCalling: true` ✅
- `bots[0].supportsVideo: false` ✅ (audio-only POC)
- `validDomains` must include your ngrok and media domains ✅
