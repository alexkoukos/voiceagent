import SwiftUI

/// Connection details. Opened from the gear on the main screen.
struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var baseURL = Settings.baseURL
    @State private var apiKey = Settings.apiKey
    @State private var adminKey = Settings.adminKey
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
                    SecureField("Κλειδί Dialer", text: $apiKey)
                    SecureField("Κλειδί διαχειριστή", text: $adminKey)
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
            }
            .navigationTitle("Ρυθμίσεις")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Τέλος") { if save() { dismiss() } }
                }
            }
        }
    }

    private func save() -> Bool {
        do {
            try Keychain.set(apiKey.trimmingCharacters(in: .whitespacesAndNewlines), for: "apiKey")
            try Keychain.set(adminKey.trimmingCharacters(in: .whitespacesAndNewlines), for: "adminKey")
            Settings.baseURL = baseURL
            baseURL = Settings.baseURL
            return true
        } catch {
            result = (false, "Δεν αποθηκεύτηκε το κλειδί. Ξεκλείδωσε με Face ID ή κωδικό και δοκίμασε ξανά.")
            return false
        }
    }

    private func test() async {
        guard save() else { return }
        testing = true
        defer { testing = false }
        do {
            let options = try await APIClient().options()
            UserDefaults.standard.set(options.accountScope ?? "dialer", forKey: "accountScope")
            result = (true, "Συνδέθηκε")
        } catch { result = (false, friendlyMessage(error)) }
    }
}
