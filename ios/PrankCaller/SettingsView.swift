import SwiftUI

struct SettingsView: View {
    @State private var baseURL = Settings.baseURL
    @State private var apiKey = Settings.apiKey

    var body: some View {
        NavigationStack {
            Form {
                Section("Backend") {
                    TextField("Base URL", text: $baseURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .onChange(of: baseURL) { Settings.baseURL = baseURL }
                    SecureField("API key", text: $apiKey)
                        .onChange(of: apiKey) { Settings.apiKey = apiKey }
                }
            }
            .navigationTitle("Settings")
        }
    }
}
