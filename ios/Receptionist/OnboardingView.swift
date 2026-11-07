import SwiftUI

struct VerticalTemplate: Decodable, Identifiable {
    let id: String
    let label: String
}

struct NewPracticeView: View {
    var onCreated: (Practice) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var vertical = "dentist"
    @State private var templates: [VerticalTemplate] = []
    @State private var busy = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Επιχείρηση") {
                    TextField("Όνομα", text: $name)
                    Picker("Κλάδος", selection: $vertical) {
                        ForEach(templates) { Text($0.label).tag($0.id) }
                    }
                }
                Section {
                    Text("Μετά την προσθήκη: εισαγωγή από Google, έλεγχος τιμών, σύνδεση ημερολογίου και δοκιμή του βοηθού.")
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(Palette.danger) }
            }
            .navigationTitle("Νέα επιχείρηση")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Άκυρο") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(busy ? "…" : "Προσθήκη") { Task { await create() } }
                        .disabled(busy || name.trimmingCharacters(in: .whitespaces).isEmpty || templates.isEmpty)
                }
            }
            .task {
                do { templates = try await APIClient().verticals() }
                catch { errorMessage = friendlyMessage(error) }
            }
        }
    }

    private func create() async {
        guard await AppLock.confirm("Προσθήκη επιχείρησης") else { return }
        busy = true; defer { busy = false }
        do {
            var values = try await APIClient().vertical(vertical)
            values["name"] = .string(name.trimmingCharacters(in: .whitespaces))
            values["slug"] = .string("demo-" + UUID().uuidString.lowercased())
            let practice = try await APIClient().createPractice(values)
            onCreated(practice)
            dismiss()
        } catch { errorMessage = friendlyMessage(error) }
    }
}

/// Imported values remain a draft until a separate, authenticated approval.
struct DraftReviewView: View {
    let practiceId: String
    let version: ConfigVersion
    var onSaved: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var changes: [String: JSONValue]
    @State private var saving = false
    @State private var errorMessage: String?

    init(practiceId: String, version: ConfigVersion, onSaved: @escaping () -> Void) {
        self.practiceId = practiceId
        self.version = version
        self.onSaved = onSaved
        _changes = State(initialValue: version.changes)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section { Text(version.summary) }
                ForEach(changes.keys.sorted(), id: \.self) { key in
                    Section(DraftValueEditor.label(key)) {
                        DraftValueEditor(title: key, value: Binding(
                            get: { changes[key] ?? .null }, set: { changes[key] = $0 }))
                    }
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(Palette.danger) }
            }
            .navigationTitle("Έλεγχος εισαγωγής")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Άκυρο") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "…" : "Αποθήκευση") { Task { await save() } }.disabled(saving)
                }
            }
        }
    }

    private func save() async {
        saving = true; defer { saving = false }
        do {
            _ = try await APIClient().editDraft(practiceId, version.id, changes: changes)
            onSaved(); dismiss()
        } catch { errorMessage = friendlyMessage(error) }
    }
}

private struct DraftValueEditor: View {
    let title: String
    @Binding var value: JSONValue

    static func label(_ key: String) -> String {
        ["name": "Όνομα", "hours": "Ωράριο", "services": "Υπηρεσίες", "rules": "Κανόνες",
         "knowledge_base": "Πληροφορίες", "knowledgeBase": "Πληροφορίες", "price": "Τιμή",
         "duration_minutes": "Διάρκεια σε λεπτά", "durationMinutes": "Διάρκεια σε λεπτά",
         "mon": "Δευτέρα", "tue": "Τρίτη", "wed": "Τετάρτη", "thu": "Πέμπτη", "fri": "Παρασκευή",
         "sat": "Σάββατο", "sun": "Κυριακή", "date_hours": "Ειδικό ωράριο", "dateHours": "Ειδικό ωράριο",
         "slot_step_minutes": "Βήμα ώρας (λεπτά)", "buffer_minutes": "Κενό μεταξύ ραντεβού (λεπτά)",
         "max_days_ahead": "Μέγιστες ημέρες κράτησης", "min_notice_minutes": "Ελάχιστη προειδοποίηση (λεπτά)",
         "holidays": "Αργίες", "closures": "Κλειστά"] [key] ?? key
    }

    var body: some View { content }

    private var content: AnyView {
        switch value {
        case .string, .null:
            return AnyView(TextField(Self.label(title), text: Binding(
                get: { if case .string(let text) = value { return text }; return "" },
                set: { value = .string($0) }), axis: .vertical))
        case .number:
            return AnyView(TextField(Self.label(title), value: Binding(
                get: { if case .number(let number) = value { return number }; return 0 },
                set: { value = .number($0) }), format: .number).keyboardType(.decimalPad))
        case .bool:
            return AnyView(Toggle(Self.label(title), isOn: Binding(
                get: { value == .bool(true) }, set: { value = .bool($0) })))
        case .object(let values):
            return AnyView(ForEach(values.keys.sorted().filter { $0 != "id" }, id: \.self) { key in
                DraftValueEditor(title: key, value: Binding(
                    get: { if case .object(let current) = value { return current[key] ?? .null }; return .null },
                    set: { new in if case .object(var current) = value { current[key] = new; value = .object(current) } }))
            })
        case .array(let values):
            return AnyView(VStack(alignment: .leading, spacing: 12) {
                Text(Self.label(title)).font(.headline)
                if values.isEmpty { Text("Κανένα").foregroundStyle(.secondary) }
                ForEach(values.indices, id: \.self) { index in
                    DraftValueEditor(title: "\(index + 1)", value: Binding(
                        get: { if case .array(let current) = value, current.indices.contains(index) { return current[index] }; return .null },
                        set: { new in if case .array(var current) = value, current.indices.contains(index) { current[index] = new; value = .array(current) } }))
                    Divider()
                }
            })
        }
    }
}
