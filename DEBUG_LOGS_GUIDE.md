# Debug Logs Guide

## Overview

The EPG Addon now includes a comprehensive logging system with a web-based Debug Logs interface. This allows you to:

- View all application logs in real-time
- Filter logs by severity level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Filter logs by Python module/file
- Search logs by keyword
- Auto-refresh logs every 10 seconds
- Clear old logs
- Load more historical logs

## Features

### 1. **Multi-Level Filtering**

#### By Log Level
Filter logs by severity:
- **DEBUG**: Detailed diagnostic information
- **INFO**: General informational messages
- **WARNING**: Warning messages for potential issues
- **ERROR**: Error messages for failures
- **CRITICAL**: Critical errors that may cause system failure

#### By Module
Filter logs by the Python file that generated them:
- `optimizer` - Main optimization logic
- `devices` - Device control and actions
- `optimization` - Mathematical optimization routines
- `ha_client` - Home Assistant API client
- `price_fetcher` - Price data fetching
- `device_state_manager` - Device state management
- `load_watcher` - Power consumption monitoring
- And all other modules...

#### By Search Text
Search for specific keywords in log messages, module names, or filenames.

### 2. **Real-Time Updates**

- **Auto-Refresh**: Logs automatically refresh every 10 seconds when the Debug tab is active
- **Manual Refresh**: Click the 🔄 Refresh button to update immediately
- **Toggle Auto-Refresh**: Click the ⏸️ Auto-Refresh button to enable/disable automatic updates

### 3. **Log Statistics**

At the top of the Debug tab, you'll see a summary of logs by level:
- Total number of logs
- Count of DEBUG, INFO, WARNING, ERROR, and CRITICAL logs

### 4. **Pagination**

- Initially loads the last 100 logs
- Click "Load More" to fetch additional historical logs
- Shows total number of logs displayed

### 5. **Log Details**

Each log entry shows:
- **Timestamp**: When the log was created (in your local timezone)
- **Level**: Severity level with color coding
- **Module**: The Python file that generated the log
- **Function**: The function name where the log was created
- **Line**: Line number in the source file
- **Message**: The full log message
- **Exception Info**: Stack traces for errors (if available)

## Accessing the Debug Logs

1. Open the web UI at `http://<your-addon-ip>:8099`
2. Click the **"Debug Logs"** tab in the navigation bar
3. Use the filters to narrow down what you're looking for

## Use Cases

### Finding Errors
1. Set **Level** filter to "ERROR"
2. Review all error messages
3. Click "Load More" if needed to see historical errors

### Debugging a Specific Module
1. Select the module from the **Module** dropdown (e.g., "optimizer")
2. Optionally set **Level** to "DEBUG" to see detailed information
3. Use the search box to find specific keywords

### Monitoring Real-Time Activity
1. Leave filters at "All Levels" and "All Modules"
2. Enable **Auto-Refresh**
3. Watch logs update in real-time

### Investigating a Specific Issue
1. Use the **Search** box to find keywords related to your issue
2. Click through related logs to understand the context
3. Check exception stack traces for detailed error information

## Log Storage

- Logs are stored in TinyDB (`db.json`)
- Maximum of **5,000 log entries** are kept (oldest are automatically removed)
- Logs persist across restarts
- You can clear all logs using the 🗑️ Clear All button

## Performance Tips

1. **Use Filters**: Instead of scrolling through thousands of logs, use level and module filters
2. **Search Efficiently**: Use specific keywords to narrow down results
3. **Disable Auto-Refresh**: When analyzing logs, disable auto-refresh to prevent the view from jumping
4. **Clear Old Logs**: Periodically clear logs to keep the database small and queries fast

## Technical Details

### Log Handler
- Custom `TinyDBLoggingHandler` in `src/log_handler.py`
- Thread-safe log writing
- Automatic log rotation (keeps last 5,000 entries)

### API Endpoints
- `GET /api/logs` - Retrieve logs with filtering
  - Query params: `level`, `module`, `limit`, `offset`
- `POST /api/logs/clear` - Clear all logs

### Log Format
Each log entry contains:
```json
{
  "timestamp": "2026-01-07T12:34:56.789",
  "level": "INFO",
  "logger": "src.optimizer",
  "module": "optimizer",
  "filename": "optimizer.py",
  "funcName": "optimize_devices",
  "lineno": 123,
  "message": "Starting optimization...",
  "exc_info": "..." // Only if exception occurred
}
```

## Troubleshooting

### Logs Not Appearing
- Check that the optimization script is running
- Verify that `db.json` exists and is writable
- Check the file logs at `/data/logs/epg_addon.log`

### Slow Performance
- Clear old logs using the Clear All button
- Use more specific filters to reduce result set
- Reduce the number of logs loaded (default is 100)

### Missing Modules in Dropdown
- Module list is populated from existing logs
- If a module hasn't logged anything yet, it won't appear
- Start with "All Modules" to see everything

## Color Coding

- **Gray Badge**: DEBUG level
- **Cyan Badge**: INFO level
- **Yellow Badge**: WARNING level
- **Red Badge**: ERROR level
- **Dark Red Badge**: CRITICAL level

Error and critical log rows also have colored backgrounds for quick visual identification.
