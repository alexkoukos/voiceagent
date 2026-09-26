import SwiftUI

/// Changes after go-live (PRD OP2, OP3): approval queue, recording and its notice (G7),
/// closures and leave, the doctor's magic link, and every past change with one-tap rollback.
struct PracticeSettingsView: View {
    let practice: Practice
    private let api = APIClient()
    @State private var pending: [ConfigVersion] = []
    @State private var history: [ConfigVersion] = []
    @State private var closures: [Closure] = []
    @State private var staff: [StaffMember] = []
    @State private var link: AdminLink?
    @State private var addingClosure = false
    @State private var rollingBack: ConfigVersion?
    @State private var errorMessage: String?
    @State private var loaded = false
    @State private var alerts: [OpsAlert] = []
    @State private var recording: RecordingSettings?

    var body: some View {
        List {
            if let errorMessage { ErrorBanner(text: errorMessage).listRowSeparator(.hidden) }
            AlertsSection(alerts: $alerts) { await load() }
            approvalSection
            Section {
                NavigationLink { OnboardingView(practice: practice) } label: {
                    Label("Έναρξη λειτουργίας", systemImage: "checklist")
                }
                NavigationLink { ImportView(practice: practice) } label: {
                    Label("Εισαγωγή στοιχείων", systemImage: "square.and.arrow.down")
                }
                NavigationLink { CalendarView(practice: practice) } label: {
                    Label("Ημερολόγια Google", systemImage: "calendar")
                }
                NavigationLink { ForwardingView(practice: practice) } label: {
                    Label("Προώθηση κλήσεων", systemImage: "phone.arrow.right")
                }
                NavigationLink { PatientDataView(practice: practice) } label: {
                    Label("Στοιχεία πελάτη (GDPR)", systemImage: "person.text.rectangle")
                }
                NavigationLink { PracticeControlsView(practice: practice) } label: {
                    Label("Κόστος, αποκλεισμοί, PIN, αποχώρηση", systemImage: "gearshape")
                }
            }
            recordingSection
            closuresSection
            linkSection
            historySection
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Ρυθμίσεις")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $addingClosure) {
            AddClosureView(practiceId: practice.id, staff: staff) { Task { await load() } }
        }
        .confirmationDialog("Επαναφορά σε αυτή την έκδοση;", isPresented: Binding(
            get: { rollingBack != nil }, set: { if !$0 { rollingBack = nil } }
        ), titleVisibility: .visible, presenting: rollingBack) { v in
            Button("Επαναφορά") { Task { await rollback(v) } }
        } message: { v in
            Text("Ωράριο, υπηρεσίες, κανόνες, κλειστά και πληροφορίες γίνονται όπως ήταν μετά το «\(v.summary)». Η επαναφορά είναι κι αυτή νέα έκδοση.")
        }
    }

    // MARK: Sections

    @ViewBuilder private var approvalSection: some View {
        Section {
            if pending.isEmpty {
                Text("Τίποτα για έγκριση").foregroundStyle(.secondary)
            }
            ForEach(pending) { v in
                VStack(alignment: .leading, spacing: Space.s) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(v.summary.capitalizedFirst).font(.body.weight(.semibold))
                        Spacer()
                        Text(v.createdAt, format: .relative(presentation: .named)).font(.footnote).foregroundStyle(.secondary)
                    }
                    if !v.author.isEmpty && v.author != "link" {
                        Text("Από: \(v.author)").font(.footnote).foregroundStyle(.secondary)
                    }
                    ForEach(v.previewLines, id: \.self) { Text($0).font(.subheadline) }
                    HStack(spacing: Space.m) {
                        Button("Έγκριση") { Task { await decide(v, approve: true) } }
                            .buttonStyle(.borderedProminent).tint(Palette.ink)
                        Button("Απόρριψη", role: .destructive) { Task { await decide(v, approve: false) } }
                            .buttonStyle(.bordered)
                    }
                    .padding(.top, Space.xs)
                }
                .padding(.vertical, Space.xs)
            }
        } header: {
            Text("Για έγκριση")
        } footer: {
            Text("Τιμές, υπηρεσίες και πληροφορίες από τον σύνδεσμο του γιατρού. Ο βοηθός τις λέει μόνο αφού τις εγκρίνεις.")
        }
    }

    @ViewBuilder private var recordingSection: some View {
        if let recording {
            Section {
                Toggle("Ηχογράφηση κλήσεων", isOn: recordingBinding(\.recordingEnabled, current: recording))
                Toggle("Ενημέρωση στον χαιρετισμό", isOn: recordingBinding(\.recordingNotice, current: recording))
                    .disabled(!recording.recordingEnabled)
            } header: {
                Text("Ηχογράφηση")
            } footer: {
                Text(recording.recordingEnabled
                     ? "Με την ενημέρωση ο βοηθός λέει στην αρχή ότι η κλήση ηχογραφείται και ότι μπορεί να σβηστεί. Ο νόμος τη ζητά όταν ηχογραφούμε."
                     : "Οι κλήσεις δεν ηχογραφούνται. Οι περιλήψεις και τα κείμενα των κλήσεων μένουν.")
            }
        }
    }

    private func recordingBinding(_ key: WritableKeyPath<RecordingSettings, Bool>,
                                  current: RecordingSettings) -> Binding<Bool> {
        Binding(
            get: { current[keyPath: key] },
            set: { value in
                var next = current
                next[keyPath: key] = value
                recording = next
                Task { await saveRecording(next, previous: current) }
            }
        )
    }

    @ViewBuilder private var closuresSection: some View {
        Section {
            ForEach(closures) { c in
                VStack(alignment: .leading, spacing: Space.xs) {
                    Text(c.range).font(.body.weight(.semibold))
                    Text([who(c), c.reason].compactMap { $0 }.joined(separator: " · "))
                        .font(.subheadline).foregroundStyle(.secondary)
                    if !c.toRebook.isEmpty {
                        Text("\(c.toRebook.count) ραντεβού θέλουν αλλαγή: "
                             + c.toRebook.map(\.customerName).joined(separator: ", "))
                            .font(.footnote.weight(.semibold)).foregroundStyle(Palette.danger)
                    }
                }
                .padding(.vertical, Space.xs)
                .swipeActions {
                    Button("Αφαίρεση", role: .destructive) { Task { await remove(c) } }
                }
            }
            Button { addingClosure = true } label: { Label("Κλειστά ή άδεια", systemImage: "plus") }
        } header: {
            Text("Κλειστά και άδειες")
        } footer: {
            Text("Ο βοηθός δεν κλείνει ραντεβού αυτές τις μέρες και το λέει στους πελάτες.")
        }
    }

    @ViewBuilder private var linkSection: some View {
        Section {
            if let link, let url = URL(string: link.url) {
                ShareLink(item: url, message: Text("Αλλαγές για τον ψηφιακό βοηθό")) {
                    Label("Αποστολή συνδέσμου", systemImage: "square.and.arrow.up")
                }
                Text("Λήγει \(link.expiresAt.formatted(.relative(presentation: .named)))")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Menu {
                Button("Για όλη την επιχείρηση") { Task { await makeLink(staffId: nil) } }
                ForEach(staff) { p in
                    Button("Μόνο άδειες: \(p.name)") { Task { await makeLink(staffId: p.id) } }
                }
            } label: {
                Label("Νέος σύνδεσμος", systemImage: "link")
            }
            Button("Απενεργοποίηση όλων των συνδέσμων", role: .destructive) { Task { await revokeLinks() } }
        } header: {
            Text("Σύνδεσμος για τον γιατρό")
        } footer: {
            Text("Ανοίγει στον browser, χωρίς εφαρμογή: ωράριο, κλειστά, τιμές, πληροφορίες. Ισχύει 3 μέρες.")
        }
    }

    @ViewBuilder private var historySection: some View {
        Section("Ιστορικό αλλαγών") {
            ForEach(history) { v in
                Button { rollingBack = v } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(v.summary.capitalizedFirst).foregroundStyle(.primary)
                            Spacer()
                            Text(v.createdAt, format: .dateTime.day().month().hour().minute())
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        Text(v.sourceLabel + (v.status == "rejected" ? " · απορρίφθηκε" : ""))
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                .disabled(v.status != "published")
            }
        }
    }

    private func who(_ c: Closure) -> String {
        guard let id = c.staffId else { return "Όλη η επιχείρηση" }
        return staff.first { $0.id == id }?.name ?? "Προσωπικό"
    }

    // MARK: Data

    private func load() async {
        do {
            async let v = api.versions(practice.id)
            async let c = api.closures(practice.id)
            async let s = api.staff(practice.id)
            let versions = try await v
            alerts = (try? await api.alerts()) ?? []
            recording = try? await api.recording(practice.id)
            (closures, staff) = try await (c, s)
            pending = versions.filter { $0.status == "pending" }
            history = versions.filter { $0.status != "pending" }
            errorMessage = nil
        } catch { errorMessage = friendlyMessage(error) }
        loaded = true
    }

    private func saveRecording(_ next: RecordingSettings, previous: RecordingSettings) async {
        do { recording = try await api.setRecording(practice.id, next) }
        catch { recording = previous; errorMessage = friendlyMessage(error) }
    }

    private func decide(_ v: ConfigVersion, approve: Bool) async {
        if approve, !(await AppLock.confirm("Έγκριση αλλαγής: \(v.summary)")) { return }
        do { try await api.decide(practice.id, v.id, approve: approve); await load() }
        catch { errorMessage = friendlyMessage(error) }
    }

    private func rollback(_ v: ConfigVersion) async {
        guard await AppLock.confirm("Επαναφορά ρυθμίσεων") else { return }
        do { try await api.rollback(practice.id, v.id); await load() }
        catch { errorMessage = friendlyMessage(error) }
    }

    private func remove(_ c: Closure) async {
        do { try await api.deleteClosure(practice.id, c.id); await load() }
        catch { errorMessage = friendlyMessage(error) }
    }

    private func makeLink(staffId: String?) async {
        guard await AppLock.confirm("Νέος σύνδεσμος για αλλαγές") else { return }
        do { link = try await api.createLink(practice.id, staffId: staffId) }
        catch { errorMessage = friendlyMessage(error) }
    }

    private func revokeLinks() async {
        do { try await api.revokeLinks(practice.id); link = nil }
        catch { errorMessage = friendlyMessage(error) }
    }
}

struct AddClosureView: View {
    let practiceId: String
    let staff: [StaffMember]
    let onSaved: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var from = Date()
    @State private var to = Date()
    @State private var staffId = ""
    @State private var reason = ""
    @State private var saving = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    DatePicker("Από", selection: $from, in: Date()..., displayedComponents: .date)
                    DatePicker("Έως", selection: $to, in: from..., displayedComponents: .date)
                }
                Section {
                    Picker("Ποιος", selection: $staffId) {
                        Text("Όλη η επιχείρηση").tag("")
                        ForEach(staff) { Text($0.name).tag($0.id) }
                    }
                    TextField("Αιτία (προαιρετικό)", text: $reason)
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(Palette.danger) }
            }
            .onChange(of: from) { _, f in if to < f { to = f } }
            .navigationTitle("Κλειστά ή άδεια")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Άκυρο") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "…" : "Αποθήκευση") { Task { await save() } }.disabled(saving)
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            let r = reason.trimmingCharacters(in: .whitespacesAndNewlines)
            _ = try await APIClient().addClosure(practiceId, NewClosure(
                dateFrom: Closure.day(from), dateTo: Closure.day(to), staffId: staffId.isEmpty ? nil : staffId,
                reason: r.isEmpty ? nil : r))
            onSaved()
            dismiss()
        } catch { errorMessage = friendlyMessage(error) }
    }
}

private extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}
