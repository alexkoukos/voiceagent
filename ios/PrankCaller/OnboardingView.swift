import SwiftUI

// Onboarding a new business: pick a vertical template, fill in what is its own, then work
// through the go-live checklist (GET /practices/<id>/onboarding) during the visit.

// MARK: New business

struct NewPracticeView: View {
    var onCreated: (Practice) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var verticals: [VerticalInfo] = []
    @State private var vertical = ""
    @State private var name = ""
    @State private var number = ""
    @State private var email = ""
    @State private var slug = ""
    @State private var saving = false
    @State private var errorMessage: String?

    private var canSave: Bool {
        !vertical.isEmpty && !name.trimmingCharacters(in: .whitespaces).isEmpty && !saving
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Κλάδος", selection: $vertical) {
                        if vertical.isEmpty { Text("Επιλογή").tag("") }
                        ForEach(verticals) { Text($0.label).tag($0.id) }
                    }
                    TextField("Όνομα επιχείρησης", text: $name)
                } footer: {
                    Text("Ο κλάδος φέρνει έτοιμο ωράριο, υπηρεσίες και κανόνες. Τα αλλάζεις μετά, ή τα φέρνεις από Google και τιμοκατάλογο.")
                }
                Section {
                    TextField("Αριθμός του βοηθού (+30…)", text: $number)
                        .keyboardType(.phonePad).textContentType(.telephoneNumber)
                    TextField("Email επιχείρησης", text: $email)
                        .keyboardType(.emailAddress).textInputAutocapitalization(.never).autocorrectionDisabled()
                } footer: {
                    Text("Ο αριθμός μπορεί να μπει και αργότερα, όταν υπάρξει. Στο email πηγαίνουν οι περιλήψεις των κλήσεων.")
                }
                Section {
                    TextField("demo-link (προαιρετικό)", text: $slug)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                } footer: {
                    Text("Μικρά λατινικά, αριθμοί και παύλες, 6+ χαρακτήρες. Ανοίγει τη σελίδα δοκιμής /demo/…")
                }
                if let errorMessage { Section { Text(errorMessage).foregroundStyle(Palette.danger) } }
            }
            .navigationTitle("Νέα επιχείρηση")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Άκυρο") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Δημιουργία") { Task { await save() } }.disabled(!canSave)
                }
            }
            .task {
                do { verticals = try await APIClient().verticals() }
                catch { errorMessage = friendlyMessage(error) }
            }
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        var fields: [String: Any] = ["name": name.trimmingCharacters(in: .whitespaces)]
        let n = number.trimmingCharacters(in: .whitespaces)
        if !n.isEmpty { fields["phone_numbers"] = [n] }
        let e = email.trimmingCharacters(in: .whitespaces)
        if !e.isEmpty { fields["notifications"] = ["emails": [e]] }
        let s = slug.trimmingCharacters(in: .whitespaces).lowercased()
        if !s.isEmpty { fields["slug"] = s }
        do {
            let practice = try await APIClient().createPractice(vertical: vertical, fields: fields)
            onCreated(practice)
            dismiss()
        } catch APIError.badStatus(409, let body) {
            errorMessage = body.contains("number_in_use") ? "Ο αριθμός ανήκει ήδη σε άλλη επιχείρηση."
                : "Το demo-link χρησιμοποιείται ήδη."
        } catch {
            errorMessage = friendlyMessage(error)
        }
    }
}

// MARK: Go-live checklist

struct OnboardingView: View {
    let practice: Practice
    @State private var report: OnboardingReport?
    @State private var staff: [StaffMember] = []
    @State private var addingStaff = false
    @State private var dpaSigner = ""
    @State private var dpaDate = Date()
    @State private var errorMessage: String?

    private static let labels: [String: String] = [
        "hours": "Ωράριο", "services": "Υπηρεσίες και διάρκειες", "knowledge_base": "Πληροφορίες (διεύθυνση κ.λπ.)",
        "staff": "Προσωπικό", "calendars": "Ημερολόγια Google", "numbers": "Αριθμός του βοηθού",
        "forwarding": "Προώθηση κλήσεων", "ai_disclosure": "Λέει ότι είναι ψηφιακός βοηθός",
        "recording_notice": "Ενημέρωση για ηχογράφηση", "dpa": "Σύμβαση επεξεργασίας (DPA)",
        "email": "Email περιλήψεων", "sms": "SMS", "fallback_number": "Αριθμός αν πέσει ο βοηθός",
        "alerts": "Ειδοποιήσεις λειτουργίας", "encryption": "Κρυπτογράφηση δεδομένων", "test_call": "Δοκιμαστική κλήση",
    ]

    var body: some View {
        List {
            if let errorMessage { ErrorBanner(text: errorMessage).listRowSeparator(.hidden) }
            if let report {
                statusSection(report)
                Section("Απαραίτητα") { ForEach(report.items.filter(\.required)) { row($0) } }
                Section("Προαιρετικά") { ForEach(report.items.filter { !$0.required }) { row($0) } }
                stepsSection
                staffSection
                dpaSection(report)
                confirmSection(report)
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Έναρξη λειτουργίας")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $addingStaff) {
            AddStaffView(practiceId: practice.id) { Task { await load() } }
        }
    }

    @ViewBuilder private func statusSection(_ r: OnboardingReport) -> some View {
        Section {
            if let live = r.liveAt {
                Label("Σε λειτουργία από \(String(live.prefix(10)))", systemImage: "checkmark.seal.fill")
            } else {
                Button { Task { await goLive() } } label: {
                    Label("Έναρξη λειτουργίας", systemImage: "play.circle.fill")
                }
                .disabled(!r.ready)
            }
        } footer: {
            if r.liveAt == nil {
                Text(r.ready ? "Όλα τα απαραίτητα είναι έτοιμα."
                     : "Λείπουν \(r.items.filter { $0.required && $0.status != "ok" }.count) απαραίτητα. Ο βοηθός απαντά ήδη στις κλήσεις: αυτό είναι καταγραφή, όχι διακόπτης.")
            }
        }
    }

    private func row(_ item: OnboardingItem) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.m) {
            Image(systemName: icon(item.status)).foregroundStyle(color(item.status))
            VStack(alignment: .leading, spacing: 2) {
                Text(Self.labels[item.id] ?? item.id).font(.body.weight(.semibold))
                if item.status == "not_configured" {
                    Text("Δεν έχει ρυθμιστεί στον server").font(.footnote.weight(.semibold)).foregroundStyle(.orange)
                }
                if !item.detail.isEmpty { Text(item.detail).font(.footnote).foregroundStyle(.secondary) }
            }
            Spacer()
            if let prd = item.prd { Text(prd).font(.caption2.monospaced()).foregroundStyle(.secondary) }
        }
    }

    private func icon(_ status: String) -> String {
        switch status {
        case "ok": return "checkmark.circle.fill"
        case "not_configured": return "wrench.and.screwdriver"
        case "warning": return "exclamationmark.circle"
        default: return "circle"
        }
    }

    private func color(_ status: String) -> Color {
        switch status {
        case "ok": return .green
        case "not_configured", "warning": return .orange
        default: return .secondary
        }
    }

    @ViewBuilder private var stepsSection: some View {
        Section("Βήματα") {
            NavigationLink { ImportView(practice: practice) } label: {
                Label("Google και τιμοκατάλογος", systemImage: "square.and.arrow.down")
            }
            NavigationLink { CalendarView(practice: practice) } label: {
                Label("Ημερολόγια Google", systemImage: "calendar")
            }
            NavigationLink { ForwardingView(practice: practice) } label: {
                Label("Κωδικοί προώθησης", systemImage: "phone.arrow.right")
            }
            if let slug = practice.slug, let url = URL(string: Settings.baseURL + "/demo/\(slug)") {
                Link(destination: url) { Label("Δοκιμαστική κλήση (demo)", systemImage: "waveform") }
            }
        }
    }

    @ViewBuilder private var staffSection: some View {
        Section("Προσωπικό") {
            ForEach(staff) { p in
                HStack { Text(p.name); Spacer(); Text(p.role).foregroundStyle(.secondary) }
            }
            Button { addingStaff = true } label: { Label("Προσθήκη", systemImage: "plus") }
        }
    }

    @ViewBuilder private func dpaSection(_ r: OnboardingReport) -> some View {
        Section {
            if let dpa = r.dpa {
                Label("Υπογράφηκε \(dpa.signedOn) από \(dpa.signedBy)", systemImage: "signature")
            } else {
                TextField("Ποιος υπέγραψε", text: $dpaSigner)
                DatePicker("Ημερομηνία", selection: $dpaDate, displayedComponents: .date)
                Button("Καταχώριση υπογραφής") { Task { await saveDpa() } }
                    .disabled(dpaSigner.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        } header: {
            Text("Σύμβαση επεξεργασίας δεδομένων")
        } footer: {
            Text("Το κείμενο της σύμβασης το δίνει ο δικηγόρος. Εδώ μόνο καταγράφεται ότι υπογράφηκε.")
        }
    }

    @ViewBuilder private func confirmSection(_ r: OnboardingReport) -> some View {
        Section {
            Toggle("Η προώθηση πληκτρολογήθηκε", isOn: confirmation(r, "forwarding", \.forwardingConfirmed))
            Toggle("Η δοκιμαστική κλήση πήγε καλά", isOn: confirmation(r, "test_call", \.testCallConfirmed))
        } header: {
            Text("Επιβεβαιώσεις")
        } footer: {
            Text("Μια ολοκληρωμένη κλήση στον αριθμό ή στο demo μετράει κι αυτή ως δοκιμή.")
        }
    }

    private func confirmation(_ r: OnboardingReport, _ id: String,
                              _ key: WritableKeyPath<OnboardingUpdate, Bool?>) -> Binding<Bool> {
        Binding(
            get: { r.items.first { $0.id == id }?.status == "ok" },
            set: { value in
                var u = OnboardingUpdate()
                u[keyPath: key] = value
                Task { await update(u) }
            }
        )
    }

    // MARK: Actions

    private func load() async {
        do {
            async let r = APIClient().onboarding(practice.id)
            async let s = APIClient().staff(practice.id)
            (report, staff) = try await (r, s)
            errorMessage = nil
        } catch { errorMessage = friendlyMessage(error) }
    }

    private func update(_ u: OnboardingUpdate) async {
        do { report = try await APIClient().updateOnboarding(practice.id, u); errorMessage = nil }
        catch { errorMessage = friendlyMessage(error) }
    }

    private func saveDpa() async {
        let c = Calendar.current.dateComponents([.year, .month, .day], from: dpaDate)
        let day = String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
        await update(OnboardingUpdate(dpa: DpaRecord(signedOn: day, signedBy: dpaSigner.trimmingCharacters(in: .whitespaces))))
    }

    private func goLive() async {
        guard await AppLock.confirm("Έναρξη λειτουργίας") else { return }
        do { report = try await APIClient().goLive(practice.id); errorMessage = nil }
        catch APIError.badStatus(409, _) { errorMessage = "Λείπουν ακόμα απαραίτητα βήματα."; await load() }
        catch { errorMessage = friendlyMessage(error) }
    }
}

// MARK: Staff

struct AddStaffView: View {
    let practiceId: String
    var onSaved: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var person = NewStaff()
    @State private var phone = ""
    @State private var email = ""
    @State private var errorMessage: String?

    private static let roles = [("doctor", "Γιατρός / επαγγελματίας"), ("secretary", "Γραμματεία"),
                                ("owner", "Ιδιοκτήτης"), ("staff", "Προσωπικό")]

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Όνομα", text: $person.name)
                    Picker("Ρόλος", selection: $person.role) {
                        ForEach(Self.roles, id: \.0) { Text($0.1).tag($0.0) }
                    }
                }
                Section {
                    TextField("Κινητό (για μεταφορά κλήσης)", text: $phone).keyboardType(.phonePad)
                    TextField("Email", text: $email)
                        .keyboardType(.emailAddress).textInputAutocapitalization(.never).autocorrectionDisabled()
                } footer: {
                    Text("Το ημερολόγιό του συνδέεται μετά από «Ημερολόγια Google».")
                }
                if let errorMessage { Section { Text(errorMessage).foregroundStyle(Palette.danger) } }
            }
            .navigationTitle("Νέο άτομο")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Άκυρο") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Αποθήκευση") { Task { await save() } }
                        .disabled(person.name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }

    private func save() async {
        var p = person
        p.phone = phone.isEmpty ? nil : phone
        p.email = email.isEmpty ? nil : email
        do {
            _ = try await APIClient().addStaff(practiceId, p)
            onSaved()
            dismiss()
        } catch { errorMessage = friendlyMessage(error) }
    }
}
