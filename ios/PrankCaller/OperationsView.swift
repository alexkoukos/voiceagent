import PhotosUI
import SwiftUI
import VisionKit

// Running a practice after go-live: imports (O1, O2), forwarding (O5), patient data (OP8),
// cost cap and blocking (OP10), phone PIN (OP2), offboarding (OP7), alerts (OP9).

// MARK: Alerts

struct AlertsSection: View {
    @Binding var alerts: [OpsAlert]
    var onChange: () async -> Void
    @State private var errorMessage: String?

    var body: some View {
        if !alerts.isEmpty {
            Section {
                ForEach(alerts) { a in
                    VStack(alignment: .leading, spacing: Space.xs) {
                        HStack(alignment: .firstTextBaseline) {
                            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Palette.danger)
                            Text(a.subject).font(.body.weight(.semibold))
                        }
                        if !a.body.isEmpty { Text(a.body).font(.subheadline).foregroundStyle(.secondary) }
                        HStack {
                            Text(a.createdAt, format: .relative(presentation: .named))
                            if a.escalatedAt != nil { Text("· στάλθηκε στον αναπληρωτή") }
                        }
                        .font(.footnote).foregroundStyle(.secondary)
                    }
                    .swipeActions {
                        Button("Το είδα") { Task { await ack(a) } }.tint(Palette.ink)
                    }
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(Palette.danger) }
            } header: {
                Text("Ειδοποιήσεις (\(alerts.count))")
            } footer: {
                Text("Σύρε αριστερά «Το είδα». Όσες μείνουν 30′ πάνε και στον αναπληρωτή.")
            }
        }
    }

    private func ack(_ a: OpsAlert) async {
        do { try await APIClient().ackAlert(a.id); await onChange() }
        catch { errorMessage = friendlyMessage(error) }
    }
}

// MARK: Imports (O1, O2)

struct ImportView: View {
    let practice: Practice
    @State private var query = ""
    @State private var website = ""
    @State private var photo: PhotosPickerItem?
    @State private var scanning = false
    @State private var busy: String?
    @State private var result: String?
    @State private var errorMessage: String?

    var body: some View {
        Form {
            Section {
                TextField("Σύνδεσμος Google Maps ή όνομα και περιοχή", text: $query, axis: .vertical)
                    .textInputAutocapitalization(.never)
                Button(busy == "google" ? "Εισαγωγή…" : "Εισαγωγή από Google") { Task { await google() } }
                    .disabled(query.trimmingCharacters(in: .whitespaces).count < 2 || busy != nil)
            } header: {
                Text("Προφίλ Google")
            } footer: {
                Text("Ωράριο, διεύθυνση, τηλέφωνο, ιστοσελίδα.")
            }
            Section {
                if VNDocumentCameraViewController.isSupported {
                    Button { scanning = true } label: { Label("Σκανάρισμα τιμοκαταλόγου", systemImage: "doc.viewfinder") }
                        .disabled(busy != nil)
                }
                PhotosPicker(selection: $photo, matching: .images) {
                    Label("Φωτογραφία από τη συλλογή", systemImage: "photo")
                }
                .disabled(busy != nil)
                TextField("Ή σελίδα με τιμές, https://…", text: $website)
                    .keyboardType(.URL).textInputAutocapitalization(.never)
                if !website.isEmpty {
                    Button("Ανάγνωση σελίδας") { Task { await priceList(url: website) } }.disabled(busy != nil)
                }
                if busy == "prices" { HStack { ProgressView(); Text("Διαβάζω τις τιμές…") } }
            } header: {
                Text("Τιμοκατάλογος")
            } footer: {
                Text("Υπηρεσίες, τιμές και διάρκειες. Όσα δεν διαβάστηκαν σίγουρα αναφέρονται στην έγκριση.")
            }
            if let result {
                Section { Label(result, systemImage: "checkmark.circle.fill").foregroundStyle(.primary) } footer: {
                    Text("Τίποτα δεν ισχύει πριν το εγκρίνεις στις Ρυθμίσεις → Για έγκριση.")
                }
            }
            if let errorMessage { Section { Text(errorMessage).foregroundStyle(Palette.danger) } }
        }
        .navigationTitle("Εισαγωγή στοιχείων")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $scanning) {
            DocumentScanner { images in
                scanning = false
                if let jpeg = images.first?.jpegData(compressionQuality: 0.7) {
                    Task { await priceList(data: jpeg, mime: "image/jpeg") }
                }
            }
            .ignoresSafeArea()
        }
        .onChange(of: photo) { _, item in
            guard let item else { return }
            Task {
                if let data = try? await item.loadTransferable(type: Data.self),
                   let jpeg = UIImage(data: data)?.jpegData(compressionQuality: 0.7) {
                    await priceList(data: jpeg, mime: "image/jpeg")
                }
                photo = nil
            }
        }
    }

    private func google() async {
        busy = "google"; defer { busy = nil }
        do {
            let v = try await APIClient().importGoogle(practice.id, query: query)
            result = v.map { $0.summary } ?? "Ίδια με ό,τι υπάρχει ήδη."
            errorMessage = nil
        } catch { errorMessage = importMessage(error) }
    }

    private func priceList(data: Data? = nil, mime: String? = nil, url: String? = nil) async {
        busy = "prices"; defer { busy = nil }
        do {
            let v = try await APIClient().importPriceList(practice.id, PriceListUpload(
                dataBase64: data?.base64EncodedString(), mimeType: mime, url: url))
            result = v.map { $0.summary } ?? "Ίδιες τιμές με ό,τι υπάρχει ήδη."
            errorMessage = nil
        } catch { errorMessage = importMessage(error) }
    }

    private func importMessage(_ error: Error) -> String {
        if case APIError.badStatus(let code, let body) = error {
            if code == 503 { return "Λείπει κλειδί στον server (\(body.contains("maps") ? "GOOGLE_MAPS_API_KEY" : "GEMINI_API_KEY"))." }
            if body.contains("not_found") { return "Δεν βρέθηκε στο Google. Δοκίμασε όνομα και περιοχή." }
            if body.contains("nothing_found") { return "Δεν βρέθηκαν τιμές. Δοκίμασε πιο καθαρή φωτογραφία." }
        }
        return friendlyMessage(error)
    }
}

struct DocumentScanner: UIViewControllerRepresentable {
    let onScan: ([UIImage]) -> Void

    func makeUIViewController(context: Context) -> VNDocumentCameraViewController {
        let vc = VNDocumentCameraViewController()
        vc.delegate = context.coordinator
        return vc
    }
    func updateUIViewController(_ vc: VNDocumentCameraViewController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(onScan: onScan) }

    final class Coordinator: NSObject, VNDocumentCameraViewControllerDelegate {
        let onScan: ([UIImage]) -> Void
        init(onScan: @escaping ([UIImage]) -> Void) { self.onScan = onScan }
        func documentCameraViewController(_ c: VNDocumentCameraViewController, didFinishWith scan: VNDocumentCameraScan) {
            onScan((0..<scan.pageCount).map { scan.imageOfPage(at: $0) })
        }
        func documentCameraViewControllerDidCancel(_ c: VNDocumentCameraViewController) { onScan([]) }
        func documentCameraViewController(_ c: VNDocumentCameraViewController, didFailWithError error: Error) { onScan([]) }
    }
}

// MARK: Forwarding (O5)

struct ForwardingView: View {
    let practice: Practice
    @State private var full = false
    @State private var info: ForwardingInfo?
    @State private var copied: String?
    @State private var errorMessage: String?

    private static let labels = ["no_answer": "Δεν απαντάτε σε 20″", "busy": "Μιλάτε", "unreachable": "Εκτός δικτύου",
                                 "all_calls": "Όλες οι κλήσεις"]

    var body: some View {
        Form {
            Section {
                Picker("Τρόπος", selection: $full) {
                    Text("Αναπληρωματικά").tag(false)
                    Text("Όλες οι κλήσεις").tag(true)
                }
                .pickerStyle(.segmented)
            } footer: {
                Text(full ? "Ο βοηθός απαντά σε κάθε κλήση." : "Ο βοηθός απαντά μόνο όταν δεν προλαβαίνετε.")
            }
            if let info {
                Section {
                    ForEach(info.codes, id: \.code) { c in code(Self.labels[c.what] ?? c.what, c.code) }
                } header: {
                    Text("Πληκτρολογήστε στο κινητό της επιχείρησης")
                } footer: {
                    Text("Cosmote, Vodafone, Nova. Κάθε κωδικός και πράσινο κουμπί. Για σταθερό, ρωτήστε τον πάροχο. Μετά κάντε μια δοκιμαστική κλήση.")
                }
                Section("Απενεργοποίηση") { code("Όλες οι προωθήσεις", info.off) }
            }
            if let errorMessage { Section { Text(errorMessage).foregroundStyle(Palette.danger) } }
        }
        .navigationTitle("Προώθηση κλήσεων")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: full) {
            do { info = try await APIClient().forwarding(practice.id, full: full); errorMessage = nil }
            catch {
                if case APIError.badStatus(409, _) = error { errorMessage = "Η επιχείρηση δεν έχει ακόμα αριθμό βοηθού." }
                else { errorMessage = friendlyMessage(error) }
            }
        }
    }

    private func code(_ title: String, _ value: String) -> some View {
        Button {
            UIPasteboard.general.string = value
            copied = value
        } label: {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).foregroundStyle(.primary)
                    Text(value).font(.body.monospaced()).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: copied == value ? "checkmark" : "doc.on.doc").foregroundStyle(.secondary)
            }
        }
        .accessibilityHint("Αντιγραφή")
    }
}

// MARK: Patient data (OP8)

struct PatientDataView: View {
    let practice: Practice
    @State private var phone = ""
    @State private var exportFile: URL?
    @State private var exported: CallerExport?
    @State private var confirmErase = false
    @State private var result: String?
    @State private var errorMessage: String?
    @State private var busy = false

    private var normalized: String {
        var p = phone.filter { $0.isNumber || $0 == "+" }
        if p.hasPrefix("00") { p = "+" + p.dropFirst(2) }
        if !p.hasPrefix("+") && p.count == 10 { p = "+30" + p }
        return p
    }
    private var valid: Bool { normalized.range(of: #"^\+\d{8,15}$"#, options: .regularExpression) != nil }

    var body: some View {
        Form {
            Section {
                TextField("Τηλέφωνο πελάτη", text: $phone).keyboardType(.phonePad)
            } footer: {
                Text("Όταν ένας πελάτης ζητά τα στοιχεία του ή τη διαγραφή τους. Καταγράφεται για τον υπεύθυνο επεξεργασίας.")
            }
            Section {
                Button(busy ? "…" : "Εξαγωγή στοιχείων") { Task { await export() } }.disabled(!valid || busy)
                if let exported, let exportFile {
                    Text("\(exported.calls) κλήσεις · \(exported.appointments) ραντεβού · \(exported.messages) μηνύματα")
                        .font(.subheadline).foregroundStyle(.secondary)
                    ShareLink(item: exportFile) { Label("Αποστολή αρχείου", systemImage: "square.and.arrow.up") }
                }
            }
            Section {
                Button("Διαγραφή όλων των στοιχείων", role: .destructive) { confirmErase = true }.disabled(!valid || busy)
            } footer: {
                Text("Απομαγνητοφωνήσεις, ηχογραφήσεις, μηνύματα, όνομα και αριθμός. Τα μελλοντικά ραντεβού ακυρώνονται πρώτα από τα Ραντεβού.")
            }
            if let result { Section { Label(result, systemImage: "checkmark.circle.fill") } }
            if let errorMessage { Section { Text(errorMessage).foregroundStyle(Palette.danger) } }
        }
        .navigationTitle("Στοιχεία πελάτη")
        .navigationBarTitleDisplayMode(.inline)
        .confirmationDialog("Οριστική διαγραφή για \(normalized);", isPresented: $confirmErase, titleVisibility: .visible) {
            Button("Διαγραφή", role: .destructive) { Task { await erase() } }
        }
    }

    private func export() async {
        guard await AppLock.confirm("Εξαγωγή στοιχείων πελάτη") else { return }
        busy = true; defer { busy = false }
        do {
            let data = try await APIClient().exportCaller(practice.id, phone: normalized)
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("στοιχεία-\(normalized).json")
            try data.write(to: url, options: [.atomic, .completeFileProtection])
            exported = try JSONDecoder().decode(CallerExport.self, from: data)
            exportFile = url
            errorMessage = nil
        } catch { errorMessage = friendlyMessage(error) }
    }

    private func erase() async {
        guard await AppLock.confirm("Διαγραφή στοιχείων πελάτη") else { return }
        busy = true; defer { busy = false }
        do {
            let c = try await APIClient().eraseCaller(practice.id, phone: normalized)
            result = "Διαγράφηκαν: \(c["calls"] ?? 0) κλήσεις, \(c["messages"] ?? 0) μηνύματα, \(c["recordings"] ?? 0) ηχογραφήσεις."
            exported = nil; exportFile = nil; errorMessage = nil
        } catch {
            if case APIError.badStatus(409, _) = error {
                errorMessage = "Ο πελάτης έχει μελλοντικό ραντεβού. Ακύρωσέ το πρώτα από τα Ραντεβού."
            } else { errorMessage = friendlyMessage(error) }
        }
    }
}

// MARK: Cost, blocking, PIN, offboarding (OP10, OP2, OP7)

struct PracticeControlsView: View {
    let practice: Practice
    @State private var usage: PracticeUsage?
    @State private var cap = ""
    @State private var block = ""
    @State private var pin = ""
    @State private var csvFile: URL?
    @State private var confirmOffboard = false
    @State private var note: String?
    @State private var errorMessage: String?

    var body: some View {
        Form {
            Section {
                if let usage {
                    LabeledContent("Αυτόν τον μήνα", value: String(format: "%.2f€", usage.monthCostEur))
                }
                HStack {
                    TextField("Όριο, π.χ. 30", text: $cap).keyboardType(.decimalPad)
                    Text("€/μήνα").foregroundStyle(.secondary)
                    Button("Ορισμός") { Task { await setCap() } }
                }
            } header: {
                Text("Κόστος")
            } footer: {
                Text("Ειδοποίηση στο 80%. Στο 100% ο βοηθός δεν απαντά νέες κλήσεις μέχρι τον επόμενο μήνα. Κενό = χωρίς όριο.")
            }
            Section {
                ForEach(usage?.blockedNumbers ?? [], id: \.self) { n in
                    Text(n).swipeActions { Button("Ξεμπλοκάρισμα") { Task { await unblock(n) } }.tint(Palette.ink) }
                }
                HStack {
                    TextField("+30…", text: $block).keyboardType(.phonePad)
                    Button("Αποκλεισμός") { Task { await addBlock() } }.disabled(block.count < 8)
                }
            } header: {
                Text("Αποκλεισμένοι αριθμοί")
            } footer: {
                Text("Ο βοηθός κλείνει χωρίς να μιλήσει. Τρεις σιωπηλές κλήσεις σε μια μέρα αποκλείουν αυτόματα τον αριθμό.")
            }
            Section {
                SecureField("Νέο PIN (4-6 ψηφία)", text: $pin).keyboardType(.numberPad)
                Button("Ορισμός PIN") { Task { await setPin(pin) } }
                    .disabled(pin.range(of: #"^\d{4,6}$"#, options: .regularExpression) == nil)
                Button("Απενεργοποίηση αλλαγών από τηλέφωνο", role: .destructive) { Task { await setPin(nil) } }
            } header: {
                Text("Αλλαγές από τηλέφωνο")
            } footer: {
                Text("Το προσωπικό καλεί από το κινητό του (καταχωρημένο στο Προσωπικό), λέει το PIN και την αλλαγή. Με SMS δεν χρειάζεται PIN: απαντούν ΝΑΙ.")
            }
            Section {
                Button("Λήψη CSV κλήσεων και ραντεβού") { Task { await downloadCSV() } }
                if let csvFile { ShareLink(item: csvFile) { Label("Αποστολή CSV", systemImage: "square.and.arrow.up") } }
                if usage?.offboardedAt != nil {
                    Button("Επανενεργοποίηση") { Task { await reactivate() } }
                } else {
                    Button("Απενεργοποίηση επιχείρησης", role: .destructive) { confirmOffboard = true }
                }
            } header: {
                Text("Αποχώρηση")
            } footer: {
                Text(usage?.offboardedAt != nil
                     ? "Απενεργοποιημένη. Ηχογραφήσεις και απομαγνητοφωνήσεις διαγράφονται 30 μέρες μετά."
                     : "Ο βοηθός σταματά αμέσως, οι σύνδεσμοι λήγουν και ο ιδιοκτήτης παίρνει email με τον κωδικό ##002# για να σταματήσει η προώθηση.")
            }
            if let note { Section { Label(note, systemImage: "checkmark.circle.fill") } }
            if let errorMessage { Section { Text(errorMessage).foregroundStyle(Palette.danger) } }
        }
        .navigationTitle("Λειτουργία")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .confirmationDialog("Να απενεργοποιηθεί η «\(practice.name)»;", isPresented: $confirmOffboard, titleVisibility: .visible) {
            Button("Απενεργοποίηση", role: .destructive) { Task { await offboard() } }
        }
    }

    private func run(_ reason: String? = nil, _ f: () async throws -> Void) async {
        if let reason, !(await AppLock.confirm(reason)) { return }
        do { try await f(); errorMessage = nil; await load() } catch { errorMessage = friendlyMessage(error) }
    }

    private func load() async {
        do {
            usage = try await APIClient().usage(practice.id)
            if cap.isEmpty, let c = usage?.monthlyCostCapEur { cap = String(format: "%.0f", c) }
        } catch { errorMessage = friendlyMessage(error) }
    }
    private func setCap() async {
        let value = Double(cap.replacingOccurrences(of: ",", with: "."))
        await run { usage = try await APIClient().setCostCap(practice.id, value); note = "Το όριο αποθηκεύτηκε." }
    }
    private func addBlock() async {
        await run { usage = try await APIClient().block(practice.id, phone: block); block = "" }
    }
    private func unblock(_ n: String) async { await run { usage = try await APIClient().unblock(practice.id, phone: n) } }
    private func setPin(_ value: String?) async {
        await run("Αλλαγή PIN") {
            try await APIClient().setAdminPin(practice.id, pin: value)
            pin = ""
            note = value == nil ? "Οι αλλαγές από τηλέφωνο απενεργοποιήθηκαν." : "Το PIN ορίστηκε."
        }
    }
    private func downloadCSV() async {
        await run("Λήψη κλήσεων και ραντεβού") {
            let data = try await APIClient().exportCSV(practice.id)
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(practice.name).csv")
            try data.write(to: url, options: [.atomic, .completeFileProtection])
            csvFile = url
        }
    }
    private func offboard() async {
        await run("Απενεργοποίηση επιχείρησης") { try await APIClient().offboard(practice.id); note = "Απενεργοποιήθηκε." }
    }
    private func reactivate() async {
        await run("Επανενεργοποίηση επιχείρησης") { try await APIClient().reactivate(practice.id); note = "Ενεργή ξανά." }
    }
}
