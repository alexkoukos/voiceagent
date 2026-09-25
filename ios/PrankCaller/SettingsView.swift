import SwiftUI

/// Connection details and saved pranks. Opened from the gear on the main screen.
struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var baseURL = Settings.baseURL
    @State private var apiKey = Settings.apiKey
    @State private var testing = false
    @State private var result: (ok: Bool, text: String)?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://…", text: $baseURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    SecureField("Κλειδί", text: $apiKey)
                } header: {
                    Text("Σύνδεση με τον server")
                } footer: {
                    Text("Το κλειδί αποθηκεύεται με ασφάλεια στο Keychain του iPhone.")
                }
                Section {
                    Button { Task { await test() } } label: {
                        HStack {
                            Text(testing ? "Έλεγχος…" : "Αποθήκευση και έλεγχος")
                            Spacer()
                            if let result {
                                Label(result.text, systemImage: result.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                                    .labelStyle(.titleAndIcon)
                                    .font(.footnote)
                                    .foregroundStyle(result.ok ? Color.primary : Palette.danger)
                            }
                        }
                    }
                    .disabled(testing)
                }
                Section {
                    NavigationLink("Οι φάρσες μου") { TemplatesView() }
                }
            }
            .navigationTitle("Ρυθμίσεις")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Τέλος") { save(); dismiss() }
                }
            }
        }
    }

    private func save() {
        Settings.baseURL = baseURL
        Settings.apiKey = apiKey
        baseURL = Settings.baseURL
    }

    private func test() async {
        save()
        testing = true
        defer { testing = false }
        do {
            _ = try await APIClient().options()
            result = (true, "Συνδέθηκε")
        } catch { result = (false, friendlyMessage(error)) }
    }
}

struct TemplatesView: View {
    @State private var templates: [PromptTemplate] = []
    @State private var errorMessage: String?

    var body: some View {
        List {
            ForEach(templates) { t in
                VStack(alignment: .leading, spacing: Space.xs) {
                    Text(t.displayTitle).font(.body.weight(.semibold))
                    Text(t.scenario).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                }
                .padding(.vertical, Space.xs)
            }
            .onDelete { offsets in
                let doomed = offsets.map { templates[$0] }
                templates.remove(atOffsets: offsets)
                Task {
                    for t in doomed {
                        do { try await APIClient().deleteTemplate(t.id) } catch { errorMessage = friendlyMessage(error) }
                    }
                }
            }
            if let errorMessage { ErrorBanner(text: errorMessage) }
        }
        .overlay {
            if templates.isEmpty { ContentUnavailableView("Καμία φάρσα", systemImage: "theatermasks") }
        }
        .navigationTitle("Οι φάρσες μου")
        .toolbar { EditButton() }
        .task {
            do { templates = try await APIClient().templates() } catch { errorMessage = friendlyMessage(error) }
        }
    }
}
