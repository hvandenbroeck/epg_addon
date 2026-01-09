# Rebuild Instructions - Debug Logs Update

## You Need to Rebuild the Docker Container

The UI appears empty because the Docker container is running the **old code** (before the debug logs changes). The `/api/logs` endpoint calls are returning 200, but the old container doesn't have the debug tab UI.

## How to Rebuild and Restart

### Option 1: Using Home Assistant Supervisor (Recommended)
1. Go to **Settings** → **Add-ons**
2. Find **EPG Addon**
3. Click **Rebuild**
4. Wait for rebuild to complete
5. Click **Restart**

### Option 2: Manual Docker Rebuild
```bash
# Stop the current container
docker stop addon_local_epg_addon

# Rebuild the image
cd /addons/epg_addon
docker build -t local/amd64-addon-epg_addon:1.0.2 .

# Restart the container (Home Assistant will do this automatically)
# Or manually:
docker start addon_local_epg_addon
```

### Option 3: Quick Test Build
If you want to test without affecting the running addon:
```bash
cd /addons/epg_addon

# Build with a test tag
docker build -t epg_addon:test .

# Run in test mode
docker run -p 8100:8099 \
  -v $(pwd)/data:/data \
  -v $(pwd)/db.json:/app/db.json \
  -e SUPERVISOR_TOKEN="your_token_here" \
  epg_addon:test

# Access at http://localhost:8100
```

## What Changed

The new version includes:
- ✅ Debug Logs tab in the UI (tab navigation at the top)
- ✅ TinyDB logging handler (stores logs in db.json)
- ✅ Filter logs by level, module, and search text
- ✅ Auto-refresh every 10 seconds
- ✅ Color-coded log levels
- ✅ Log statistics and pagination

## Verifying the Update

After rebuilding, you should see:
1. **Two tabs** at the top: "Dashboard" and "Debug Logs"
2. Click "Debug Logs" to see the new interface
3. You'll see filter dropdowns and log entries

## If Still Empty After Rebuild

If the Debug Logs tab shows "No logs match the current filters":
- This is normal if the addon just started
- Logs will appear as the application runs
- Try clicking "Refresh" button
- Make sure auto-refresh is enabled (⏸️ Auto-Refresh: ON)
- Wait for the optimization to run (scheduled at 16:05 daily)

## Current Container Info
- Running: `addon_local_epg_addon`
- Image: `local/amd64-addon-epg_addon:1.0.2`
- Built: 3 minutes ago (before the changes)
- **Status**: Needs rebuild to get new features

## Files That Changed
- `src/log_handler.py` - NEW
- `optimization_plan.py` - Added TinyDB logging handler
- `web/server.py` - Added /api/logs endpoints
- `web/templates/index.html` - Added debug tab UI
- `web/static/style.css` - Added debug tab styles
