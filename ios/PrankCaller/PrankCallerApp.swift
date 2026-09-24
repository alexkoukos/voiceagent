import SwiftUI

@main
struct PrankCallerApp: App {
    var body: some Scene {
        WindowGroup {
            TabView {
                NewCallView()
                    .tabItem { Label("New call", systemImage: "phone.arrow.up.right") }
                HistoryView()
                    .tabItem { Label("History", systemImage: "clock") }
                SettingsView()
                    .tabItem { Label("Settings", systemImage: "gear") }
            }
        }
    }
}
