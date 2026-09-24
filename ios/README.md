# iOS app (M4)

SwiftUI, iOS 17+, sideloaded from Xcode. Sources in `PrankCaller/`; the Xcode project is generated from `project.yml`.

```bash
brew install xcodegen   # once
cd ios && xcodegen && open PrankCaller.xcodeproj
```

Set the backend URL in the app's Settings tab (default `http://localhost:8000`; on a real device use your Mac's LAN IP or the deployed URL).

Screens: new call form (friend, role, scenario, context, reveal, voice, max duration, saved templates), live call (polled status + transcript + Hang up), history with recording playback and delete.

Talks only to the backend REST API; no direct LiveKit/SIP/Gemini access. Live call updates arrive over a WebSocket, with a 1-second polling fallback. The project builds for the simulator with `xcodebuild` but hasn't been run yet. `PrankCaller.xcodeproj` is generated and gitignored.
