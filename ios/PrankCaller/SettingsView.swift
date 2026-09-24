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
                Section {
                    NavigationLink("Saved templates") { TemplatesView() }
                }
            }
            .navigationTitle("Settings")
        }
    }
}

struct TemplatesView: View {
    @State private var templates: [PromptTemplate] = []
    @State private var errorMessage: String?

    var body: some View {
        List {
            ForEach(templates) { t in
                VStack(alignment: .leading, spacing: 2) {
                    Text(t.title).font(.headline)
                    Text(t.scenario).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            .onDelete { offsets in
                let doomed = offsets.map { templates[$0] }
                templates.remove(atOffsets: offsets)
                Task {
                    for t in doomed {
                        do { try await APIClient().deleteTemplate(t.id) } catch { errorMessage = error.localizedDescription }
                    }
                }
            }
            if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
        }
        .navigationTitle("Templates")
        .task { templates = (try? await APIClient().templates()) ?? [] }
    }
}
