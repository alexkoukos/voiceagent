import SwiftUI

struct BillingState: Decodable {
    let pilotStartedOn: String?
    let paidPeriodStartsOn: String?
    let monthlyFee: String?
    let guaranteeThreshold: Int
    let drafts: [BillingDraftItem]
}
struct BillingDraftItem: Decodable, Identifiable {
    let id: String
    let periodStart: String
    let periodEnd: String
    let bookings: Int
    let threshold: Int
    let amount: String
    let status: String
}
struct BillingView: View {
    let practice: Practice
    @State private var state: BillingState?
    @State private var pilotDate = Date()
    @State private var fee = ""
    @State private var error: String?
    @State private var busy = false
    var body: some View {
        Form {
            if let state {
                if let start = state.paidPeriodStartsOn {
                    Section("Πιλοτική περίοδος") {
                        LabeledContent("Έναρξη πληρωμένης περιόδου", value: start)
                        LabeledContent("Μηνιαία τιμή", value: (state.monthlyFee ?? "—") + " €")
                        LabeledContent("Εγγύηση κρατήσεων", value: String(state.guaranteeThreshold))
                    }
                } else {
                    Section("Νέα πιλοτική περίοδος") {
                        DatePicker("Έναρξη", selection: $pilotDate, displayedComponents: .date)
                        TextField("Μηνιαία τιμή (€)", text: $fee).keyboardType(.decimalPad)
                        Button("Αποθήκευση") { Task { await configure() } }.disabled(busy || fee.isEmpty)
                    }
                }
                Section {
                    ForEach(state.drafts) { draft in
                        VStack(alignment: .leading, spacing: 4) {
                            Text("\(draft.periodStart) – \(draft.periodEnd)")
                            Text("\(draft.bookings) κρατήσεις · \(draft.amount) €")
                            Text(draft.status == "waived" ? "Δωρεάν βάσει εγγύησης" : "Αναμονή παρόχου τιμολόγησης")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                } header: { Text("Προετοιμασία τιμολόγησης") } footer: {
                    Text("Δεν έχει εκδοθεί τιμολόγιο και δεν έχει γίνει χρέωση. Απαιτείται σύνδεση με πάροχο myDATA.")
                }
            }
            if let error { Section { Text(error).foregroundStyle(Palette.danger) } }
        }
        .navigationTitle("Χρέωση και εγγύηση")
        .task { await load() }
    }
    private func load() async {
        do { state = try await APIClient().billing(practice.id) }
        catch { self.error = friendlyMessage(error) }
    }
    private func configure() async {
        guard await AppLock.confirm("Αποθήκευση πιλοτικής περιόδου και τιμής") else { return }
        busy = true; defer { busy = false }
        let format = DateFormatter()
        format.calendar = Calendar(identifier: .gregorian)
        format.locale = Locale(identifier: "en_US_POSIX")
        format.dateFormat = "yyyy-MM-dd"
        do {
            state = try await APIClient().configureBilling(practice.id, start: format.string(from: pilotDate),
                                                          fee: fee.replacingOccurrences(of: ",", with: "."))
            error = nil
        } catch { self.error = friendlyMessage(error) }
    }
}
