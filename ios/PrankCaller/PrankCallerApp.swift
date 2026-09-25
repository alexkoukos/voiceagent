import SwiftUI

@main
struct PrankCallerApp: App {
    var body: some Scene {
        WindowGroup {
            TabView {
                NewCallView()
                    .tabItem { Label("Κλήση", systemImage: "phone.fill") }
                HistoryView()
                    .tabItem { Label("Ιστορικό", systemImage: "clock") }
            }
            .tint(Palette.ink)
        }
    }
}
