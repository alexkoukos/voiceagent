import SwiftUI

@main
struct PrankCallerApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

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

    // DEBUG: `-startTab 2` opens a tab directly (for simulator screenshots).
    @State private var tab = UserDefaults.standard.integer(forKey: "startTab")

    private var tabs: some View {
        TabView(selection: $tab) {
            NewCallView()
                .tabItem { Label("Κλήση", systemImage: "phone.fill") }.tag(0)
            HistoryView()
                .tabItem { Label("Ιστορικό", systemImage: "clock") }.tag(1)
            ReceptionistView()
                .tabItem { Label("Γραμματεία", systemImage: "phone.arrow.down.left") }.tag(2)
        }
        .tint(Palette.ink)
        .environment(PushRouter.shared)
        .onChange(of: tab, initial: true) { _, t in if t == 2 { PushRegistration.shared.askPermission() } }
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
