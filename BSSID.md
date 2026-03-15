# BSSID Access on macOS — Enabling Roaming Detection

## The problem

Starting with macOS Sonoma (14.0), Apple restricts access to the BSSID
(the MAC address of the WiFi access point you're connected to). Without
BSSID access, netmon cannot detect AP roaming — when your Mac silently
switches from one access point to another, which is a common cause of
brief call drops.

When BSSID is unavailable, netmon shows `?` in the BSSID field and
roaming detection is disabled.

## Why Apple restricts it

BSSID can be used for location tracking (WiFi fingerprinting). Apple
requires apps to have Location Services authorization before exposing it.

## How netmon reads BSSID

netmon uses a compiled Swift helper that calls:

```swift
CWWiFiClient.shared().interface()?.bssid()
```

This CoreWLAN API returns `nil` unless the calling process has Location
Services authorization.

## How to enable BSSID access

### Step 1: Enable Location Services globally

**System Settings > Privacy & Security > Location Services** — toggle ON.

### Step 2: Grant location access to your terminal

The Location Services permission is granted to the **parent application**
(the terminal emulator running netmon), not to the shell or netmon itself.

- **Terminal.app**: Should appear in the Location Services list after the
  first attempt. Toggle it ON.
- **iTerm2**: Same — find it in Location Services and toggle ON.
- **Alacritty / Kitty / other terminals**: May need to be added manually.
  Run netmon once, then check if the terminal appears in the list.
- **VS Code terminal**: Grant location access to "Visual Studio Code".
- **SSH sessions**: BSSID access is not available over SSH since there is
  no local GUI application to grant permission to.

### Step 3: Restart netmon

After granting permission, restart the collector:

```bash
./netmon.sh stop
./netmon.sh monitor
```

### Verifying it works

Check the BSSID field in the TUI dashboard (WiFi Details panel). If it
shows a MAC address like `aa:bb:cc:dd:ee:ff`, BSSID access is working.
If it shows `?`, the permission is not granted.

You can also test directly:

```bash
# This should print your AP's MAC address
/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport -I | grep BSSID
```

If `airport -I` shows BSSID but netmon doesn't, the Swift helper may need
recompilation. Delete the cached binary and restart:

```bash
rm -f ~/call-network-logs/.wifi_helper
./netmon.sh monitor
```

## What roaming detection does

When BSSID access is enabled, netmon:

1. **Logs roaming events** to the diagnostics CSV with the old and new
   BSSID values: `"AP roaming: BSSID changed from aa:bb:cc to dd:ee:ff"`
2. **Shows roaming in the TUI** diagnostics panel and the "Roaming" field
   in WiFi Details
3. **Charts roaming events** as diagnostic markers on the timeline

Roaming typically causes a 50-500ms interruption. Frequent roaming (more
than 2-3 times per session) suggests the Mac is between two APs with
similar signal strength — moving closer to one AP usually fixes it.

## Alternatives without BSSID

Even without BSSID access, netmon detects some roaming-like events:
- **Channel changes** — if the channel changes, you likely roamed
- **Signal strength jumps** — sudden RSSI changes suggest a new AP
- **Brief connectivity drops** — correlated with signal changes

These heuristics are less precise than BSSID tracking but still useful.
