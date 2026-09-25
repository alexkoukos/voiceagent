import SwiftUI

@main
struct PrankCallerApp: App {
    var body: some Scene {
        WindowGroup {
            #if DEBUG
            // Design check without a real call: launch with `-previewPlayerFile <audio file path>`.
            if let path = UserDefaults.standard.string(forKey: "previewPlayerFile") {
                PlayerPreview(url: URL(fileURLWithPath: path))
            } else {
                tabs
            }
            #else
            tabs
            #endif
        }
    }

    private var tabs: some View {
        TabView {
            NewCallView()
                .tabItem { Label("Κλήση", systemImage: "phone.fill") }
            HistoryView()
                .tabItem { Label("Ιστορικό", systemImage: "clock") }
        }
        .tint(Palette.ink)
    }
}

#if DEBUG
private struct PlayerPreview: View {
    let url: URL
    @State private var player: RecordingPlayer?

    var body: some View {
        NavigationStack {
            ScrollView {
                if let player { AudioPlayerView(player: player) {} .padding(Space.l) }
            }
            .background(Palette.background)
            .navigationTitle("Νίκος")
            .navigationBarTitleDisplayMode(.inline)
        }
        .tint(Palette.ink)
        .task {
            let p = RecordingPlayer { url }
            player = p
            await p.togglePlay()
            try? await Task.sleep(for: .seconds(3))
            await p.scrub(to: 9, ended: true)
        }
    }
}
#endif
