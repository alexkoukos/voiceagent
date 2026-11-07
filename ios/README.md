# iOS app

The staff app: SwiftUI, iOS 17+. Sources are in `Receptionist/`; the Xcode project is generated from `project.yml` by [XcodeGen](https://github.com/yonaskolb/XcodeGen) and is not committed.

```bash
brew install xcodegen   # once
cd ios && xcodegen && open Receptionist.xcodeproj
```

Set the backend URL and API key in the app's Settings tab (on a device, use your Mac's LAN IP or the deployed URL).

The app shows the receptionist's calls (outcome filters, summary, routing, transcript, recording), messages, appointments, metrics and practice settings, and lets staff join or listen to a live call when the agent hands over. It talks to the backend's REST API, and to LiveKit only to join a handoff.
