# iOS app (M4)

SwiftUI, iOS 17+, sideloaded from Xcode. Sources in `PrankCaller/`; the Xcode project is generated from `project.yml`.

```bash
brew install xcodegen   # once
cd ios && xcodegen && open PrankCaller.xcodeproj
```

Set the backend URL in the app's Settings tab (default `http://localhost:8000`; on a real device use your Mac's LAN IP or the deployed URL).

One tab, "Γραμματεία": the receptionist's calls (outcome filters, detail with summary, routing, transcript and recording), messages, appointments, metrics, practice settings and handoff join/listen.

Talks to the backend REST API, and to LiveKit only to join or listen to a handoff. `PrankCaller.xcodeproj` is generated and gitignored.
