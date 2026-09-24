# iOS app (M4)

SwiftUI, iOS 17+, sideloaded from Xcode. Sources in `PrankCaller/`; the Xcode project is generated from `project.yml`.

```bash
brew install xcodegen
cd ios && xcodegen && open PrankCaller.xcodeproj
```

Set the backend URL in the app's Settings tab (default `http://localhost:8000`; on a real device use your Mac's LAN IP or the deployed URL).

Screens: new call form (friend, role, scenario, context, reveal, voice, max duration, saved templates), live call (polled status + transcript + Hang up), history with recording playback and delete.

Talks only to the backend REST API; no direct LiveKit/SIP/Gemini access. The sources type-check against the iOS 17 SDK but haven't been run in a simulator.
