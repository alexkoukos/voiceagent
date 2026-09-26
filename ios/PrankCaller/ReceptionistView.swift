import SwiftUI

/// The receptionist (2.0): calls answered for each business, messages, appointments and metrics.
struct ReceptionistView: View {
    private let api = APIClient()
    @AppStorage("practiceId") private var practiceId = ""
    @State private var practices: [Practice] = []
    @State private var tab: Tab = .calls
    @State private var calls: [ReceptionistCall] = []
    @State private var messages: [PracticeMessage] = []
    @State private var appointments: [Appointment] = []
    @State private var metrics: PracticeMetrics?
    @State private var handoffs: [Handoff] = []
    @State private var outcome: String?
    @State private var loaded = false
    @State private var errorMessage: String?
    @State private var joining: Handoff?
    @State private var creating = false
    @Environment(PushRouter.self) private var push

    enum Tab: String, CaseIterable, Identifiable {
        case calls = "Κλήσεις", messages = "Μηνύματα", appointments = "Ραντεβού", metrics = "Μετρήσεις"
        var id: String { rawValue }
    }

    private var practice: Practice? { practices.first { $0.id == practiceId } ?? practices.first }

    var body: some View {
        NavigationStack {
            List {
                if let errorMessage { ErrorBanner(text: errorMessage).listRowSeparator(.hidden) }
                ForEach(handoffs) { h in
                    Button { joining = h } label: { HandoffBanner(handoff: h) }
                        .listRowBackground(Palette.ink)
                }
                Picker("", selection: $tab) {
                    ForEach(Tab.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets())
                switch tab {
                case .calls: callsSection
                case .messages: messagesSection
                case .appointments: appointmentsSection
                case .metrics: metricsSection
                }
            }
            .listStyle(.insetGrouped)
            .overlay { emptyState }
            .navigationTitle(practice?.name ?? "Γραμματεία")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if practices.count > 1 {
                    ToolbarItem(placement: .topBarLeading) {
                        Menu {
                            Picker("Επιχείρηση", selection: $practiceId) {
                                ForEach(practices) { Text($0.name).tag($0.id) }
                            }
                        } label: { Image(systemName: "building.2") }
                        .accessibilityLabel("Επιχείρηση")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { creating = true } label: { Image(systemName: "plus") }
                        .accessibilityLabel("Νέα επιχείρηση")
                }
                if let practice {
                    ToolbarItem(placement: .topBarTrailing) {
                        NavigationLink { PracticeSettingsView(practice: practice) } label: {
                            Image(systemName: "slider.horizontal.3")
                        }
                        .accessibilityLabel("Ρυθμίσεις επιχείρησης")
                    }
                }
                if let slug = practice?.slug, let url = URL(string: Settings.baseURL + "/demo/\(slug)") {
                    ToolbarItem(placement: .topBarTrailing) {
                        ShareLink(item: url) { Image(systemName: "square.and.arrow.up") }
                            .accessibilityLabel("Σύνδεσμος demo")
                    }
                }
            }
            .navigationDestination(for: String.self) { id in
                if let pid = practice?.id { ReceptionistCallView(practiceId: pid, callId: id) }
            }
            .sheet(isPresented: $creating) {
                NewPracticeView { p in
                    practices.append(p)
                    practiceId = p.id
                }
            }
            .fullScreenCover(item: $joining) { h in
                if let pid = practice?.id { HandoffView(practiceId: pid, handoff: h) }
            }
            .task(id: practice?.id) { await follow() }
            .refreshable { await load() }
            .onChange(of: push.handoffId) { _, id in
                if let id { Task { await load(); joining = handoffs.first { $0.id == id }; push.handoffId = nil } }
            }
        }
    }

    // MARK: Sections

    @ViewBuilder private var callsSection: some View {
        Section {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: Space.s) {
                    FriendChip(name: "Όλες", selected: outcome == nil) { outcome = nil }
                    ForEach(["booked", "message_taken", "info_given", "transferred", "abandoned", "failed"], id: \.self) { o in
                        FriendChip(name: ReceptionistCall.label(o), selected: outcome == o) { outcome = o }
                    }
                }
                .padding(.vertical, Space.xs)
            }
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets())
            .onChange(of: outcome) { Task { await load() } }
        }
        Section {
            ForEach(calls) { c in
                NavigationLink(value: c.id) { ReceptionistCallRow(call: c) }
            }
        }
    }

    @ViewBuilder private var messagesSection: some View {
        ForEach(messages) { m in
            MessageRow(message: m)
                .swipeActions {
                    Button(m.status == "done" ? "Νέο" : "Έγινε") { Task { await toggle(m) } }
                        .tint(Palette.ink)
                }
        }
    }

    @ViewBuilder private var appointmentsSection: some View {
        ForEach(appointments) { a in
            VStack(alignment: .leading, spacing: Space.xs) {
                HStack {
                    Text(a.customerName).font(.body.weight(.semibold))
                    Spacer()
                    Text(a.startsAt, format: .dateTime.weekday(.abbreviated).day().month().hour().minute())
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Text([a.serviceName, a.customerPhone].compactMap { $0 }.joined(separator: " · "))
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            .swipeActions {
                Button("Ακύρωση", role: .destructive) { Task { await cancel(a) } }
            }
        }
    }

    @ViewBuilder private var metricsSection: some View {
        if let m = metrics {
            Section("Τελευταίες \(m.days) μέρες") {
                MetricRow(title: "Κλήσεις που απαντήθηκαν", value: "\(m.answered)")
                MetricRow(title: "Ραντεβού από τον βοηθό", value: "\(m.bookings)",
                          note: m.valueEstimateEur > 0 ? "≈ \(Int(m.valueEstimateEur))€" : nil,
                          good: m.bookings >= m.guaranteeThreshold)
                MetricRow(title: "Λύθηκαν χωρίς άνθρωπο", value: pct(m.resolvedWithoutHumanPct), note: "στόχος ≥ 80%",
                          good: (m.resolvedWithoutHumanPct ?? 0) >= 80)
                MetricRow(title: "Σε άνθρωπο", value: pct(m.handoffPct), note: "στόχος ≤ 15%", good: (m.handoffPct ?? 0) <= 15)
                MetricRow(title: "Έκλεισαν χωρίς αποτέλεσμα", value: pct(m.abandonedPct), note: "στόχος ≤ 10%",
                          good: (m.abandonedPct ?? 0) <= 10)
            }
            Section("Ακρίβεια (από τους ελέγχους σου)") {
                MetricRow(title: "Σωστή δρομολόγηση", value: pct(m.routingAccuracyPct),
                          note: "\(m.routingReviewed) έλεγχοι · στόχος ≥ 95%", good: (m.routingAccuracyPct ?? 0) >= 95)
                MetricRow(title: "Σωστά ραντεβού", value: pct(m.bookingAccuracyPct),
                          note: "\(m.bookingReviewed) έλεγχοι · στόχος ≥ 95%", good: (m.bookingAccuracyPct ?? 0) >= 95)
            }
            Section("Λειτουργία") {
                MetricRow(title: "Χρόνος απόκρισης (διάμεσος)", value: m.latencyMsMedian.map { "\($0) ms" } ?? "—",
                          note: "στόχος < 1200 ms", good: (m.latencyMsMedian ?? 99999) < 1200)
                MetricRow(title: "Ειδοποίηση σε 60″", value: pct(m.notifiedWithin60sPct), note: "στόχος 99%",
                          good: (m.notifiedWithin60sPct ?? 0) >= 99)
                MetricRow(title: "Κόστος ανά λεπτό", value: m.costPerMinuteEur.map { String(format: "%.3f€", $0) } ?? "—",
                          note: "στόχος ≤ 0,05€", good: (m.costPerMinuteEur ?? 1) <= 0.05)
                MetricRow(title: "Κόστος σύνολο", value: String(format: "%.2f€", m.costTotalEur))
            }
            Section("Αποτελέσματα") {
                ForEach(m.outcomes.sorted { $0.value > $1.value }, id: \.key) { k, v in
                    MetricRow(title: ReceptionistCall.label(k), value: "\(v)")
                }
            }
        }
    }

    private func pct(_ v: Double?) -> String { v.map { String(format: "%.0f%%", $0) } ?? "—" }

    @ViewBuilder private var emptyState: some View {
        if loaded && errorMessage == nil {
            if practices.isEmpty {
                ContentUnavailableView {
                    Label("Καμία επιχείρηση", systemImage: "building.2")
                } description: {
                    Text("Ξεκίνα από έναν κλάδο και συμπλήρωσε τα υπόλοιπα στη λίστα έναρξης.")
                } actions: {
                    Button("Νέα επιχείρηση") { creating = true }.buttonStyle(.borderedProminent).tint(Palette.ink)
                }
            } else if tab == .calls && calls.isEmpty {
                ContentUnavailableView("Καμία κλήση", systemImage: "phone.arrow.down.left",
                                       description: Text("Οι κλήσεις που απαντά ο βοηθός θα εμφανίζονται εδώ."))
            } else if tab == .messages && messages.isEmpty {
                ContentUnavailableView("Κανένα μήνυμα", systemImage: "envelope")
            } else if tab == .appointments && appointments.isEmpty {
                ContentUnavailableView("Κανένα ραντεβού", systemImage: "calendar")
            }
        }
    }

    // MARK: Data

    /// Loads now, then again whenever the backend says something changed (falls back to polling).
    private func follow() async {
        await load()
        guard let pid = practice?.id else { return }
        while !Task.isCancelled {
            if let socket = try? api.practiceUpdates(pid) {
                socket.resume()
                while !Task.isCancelled, (try? await socket.receive()) != nil { await load() }
                socket.cancel()
            }
            try? await Task.sleep(for: .seconds(5))
            await load()
        }
    }

    private func load() async {
        guard !Settings.apiKey.isEmpty else { loaded = true; return }
        do {
            if practices.isEmpty || practice == nil { practices = try await api.practices() }
            guard let pid = practice?.id else { loaded = true; return }
            if practiceId.isEmpty { practiceId = pid }
            async let c = api.practiceCalls(pid, outcome: outcome)
            async let m = api.messages(pid)
            async let a = api.appointments(pid)
            async let h = api.handoffs(pid)
            async let mt = api.metrics(pid)
            (calls, messages, appointments, handoffs) = try await (c, m, a, h)
            metrics = try? await mt
            PushRegistration.shared.practiceId = pid
            errorMessage = nil
        } catch { errorMessage = friendlyMessage(error) }
        loaded = true
    }

    private func toggle(_ m: PracticeMessage) async {
        guard let pid = practice?.id else { return }
        do { _ = try await api.setMessage(pid, m.id, done: m.status != "done"); await load() }
        catch { errorMessage = friendlyMessage(error) }
    }

    private func cancel(_ a: Appointment) async {
        guard let pid = practice?.id else { return }
        do { _ = try await api.cancelAppointment(pid, a.id); await load() }
        catch { errorMessage = friendlyMessage(error) }
    }
}

struct HandoffBanner: View {
    let handoff: Handoff
    var body: some View {
        HStack(spacing: Space.m) {
            Image(systemName: "person.wave.2.fill").font(.title2)
            VStack(alignment: .leading) {
                Text("Πελάτης περιμένει στη γραμμή").font(.body.weight(.semibold))
                Text("Πάτα για να μπεις στην κλήση").font(.footnote).opacity(0.8)
            }
            Spacer()
            Text(handoff.createdAt, style: .timer).font(.footnote.monospacedDigit())
        }
        .foregroundStyle(Palette.onInk)
        .padding(.vertical, Space.xs)
    }
}

struct ReceptionistCallRow: View {
    let call: ReceptionistCall
    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            HStack(alignment: .firstTextBaseline) {
                Label(call.outcomeText, systemImage: call.outcomeIcon).font(.body.weight(.semibold))
                Spacer()
                Text(call.createdAt, format: .relative(presentation: .named)).font(.footnote).foregroundStyle(.secondary)
            }
            Text(call.caller).font(.subheadline).foregroundStyle(.secondary)
            if let s = call.summary { Text(s).font(.subheadline).foregroundStyle(.secondary).lineLimit(2) }
            if !call.flags.isEmpty {
                Text(call.flags.map { ReceptionistCall.flagLabels[$0] ?? $0 }.joined(separator: " · "))
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(call.flags.contains("urgent") ? Palette.danger : .secondary)
            }
        }
        .padding(.vertical, Space.xs)
    }
}

struct MessageRow: View {
    let message: PracticeMessage
    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            HStack(alignment: .firstTextBaseline) {
                if message.urgent { Image(systemName: "exclamationmark.circle.fill").foregroundStyle(Palette.danger) }
                Text(message.callerName.isEmpty ? "Χωρίς όνομα" : message.callerName).font(.body.weight(.semibold))
                Spacer()
                Text(message.createdAt, format: .relative(presentation: .named)).font(.footnote).foregroundStyle(.secondary)
            }
            Text(message.reason).font(.subheadline)
            HStack {
                if let n = message.callbackNumber, let url = URL(string: "tel:\(n)") {
                    Link(n, destination: url).font(.subheadline.weight(.semibold))
                }
                if !message.bestTime.isEmpty { Text("· \(message.bestTime)").font(.subheadline).foregroundStyle(.secondary) }
            }
        }
        .opacity(message.status == "done" ? 0.5 : 1)
        .padding(.vertical, Space.xs)
    }
}

struct MetricRow: View {
    let title: String
    let value: String
    var note: String? = nil
    var good: Bool? = nil
    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                if let note { Text(note).font(.footnote).foregroundStyle(.secondary) }
            }
            Spacer()
            Text(value).font(.body.monospacedDigit().weight(.semibold))
                .foregroundStyle(good == false ? Palette.danger : .primary)
        }
    }
}

/// One receptionist call: outcome, summary, booking, routing, transcript, recording, review.
struct ReceptionistCallView: View {
    let practiceId: String
    let callId: String
    @State private var call: ReceptionistCall?
    @State private var player: RecordingPlayer?
    @State private var errorMessage: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.xl) {
                if let call {
                    VStack(alignment: .leading, spacing: Space.s) {
                        Label(call.outcomeText, systemImage: call.outcomeIcon).font(.title3.weight(.semibold))
                        Text(call.caller).foregroundStyle(.secondary)
                        if let d = call.durationSeconds {
                            Text("Διάρκεια \(Duration.seconds(d).formatted(.time(pattern: .minuteSecond)))")
                                .font(.subheadline).foregroundStyle(.secondary)
                        }
                        if let s = call.summary { Text(s).padding(.top, Space.s) }
                    }
                    if let a = call.appointment {
                        VStack(alignment: .leading, spacing: Space.xs) {
                            SectionTitle("Ραντεβού")
                            Text("\(a.customerName) · \(a.serviceName)")
                            Text(a.startsAt, format: .dateTime.weekday(.wide).day().month().hour().minute())
                                .foregroundStyle(.secondary)
                        }
                        .padding(Space.l).frame(maxWidth: .infinity, alignment: .leading).glass()
                    }
                    ForEach(call.messages ?? []) { MessageRow(message: $0).padding(Space.l).glass() }
                    review(call)
                    if call.recordingUrl != nil, let player { AudioPlayerView(player: player) {} }
                    if let routing = call.routing, !routing.isEmpty {
                        VStack(alignment: .leading, spacing: Space.xs) {
                            SectionTitle("Δρομολόγηση")
                            ForEach(routing) { r in
                                Text("\(r.rule)\(r.value.isEmpty ? "" : ": \(r.value)")\(r.path.isEmpty ? "" : " → \(r.path)")")
                                    .font(.footnote.monospaced()).foregroundStyle(.secondary)
                            }
                        }
                    }
                    if let entries = call.transcriptEntries, !entries.isEmpty {
                        VStack(alignment: .leading, spacing: Space.m) {
                            SectionTitle("Τι ειπώθηκε")
                            ForEach(entries) { TranscriptBubble(entry: $0, friendName: "Πελάτης") }
                        }
                    }
                } else if errorMessage == nil {
                    ProgressView().frame(maxWidth: .infinity).padding(Space.xxxl)
                }
                if let errorMessage { ErrorBanner(text: errorMessage) }
            }
            .padding(Space.l)
        }
        .background(Palette.background)
        .navigationTitle("Κλήση")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .onDisappear { player?.tearDown() }
    }

    private func review(_ call: ReceptionistCall) -> some View {
        VStack(alignment: .leading, spacing: Space.m) {
            SectionTitle("Έλεγχος")
            reviewRow("Σωστή δρομολόγηση;", value: call.routingCorrect) { v in
                await save(CallReview(routingCorrect: v, bookingCorrect: call.bookingCorrect))
            }
            if call.useCase == "booking" || call.appointment != nil {
                reviewRow("Σωστό ραντεβού;", value: call.bookingCorrect) { v in
                    await save(CallReview(routingCorrect: call.routingCorrect, bookingCorrect: v))
                }
            }
        }
        .padding(Space.l).glass()
    }

    private func reviewRow(_ title: String, value: Bool?, set: @escaping (Bool) async -> Void) -> some View {
        HStack {
            Text(title)
            Spacer()
            ForEach([true, false], id: \.self) { v in
                Button { Task { await set(v) } } label: {
                    Image(systemName: v ? "hand.thumbsup.fill" : "hand.thumbsdown.fill")
                        .frame(width: 44, height: 44)
                        .foregroundStyle(value == v ? Palette.onInk : .primary)
                        .background(value == v ? Palette.ink : Palette.inkSoft, in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(v ? "Σωστό" : "Λάθος")
            }
        }
    }

    private func save(_ r: CallReview) async {
        do { _ = try await APIClient().review(practiceId, callId, r); await load() }
        catch { errorMessage = friendlyMessage(error) }
    }

    private func load() async {
        do {
            call = try await APIClient().practiceCall(practiceId, callId)
            if call?.recordingUrl != nil, player == nil {
                let id = callId
                player = RecordingPlayer { try await APIClient().recordingURL(id) }
            }
        } catch { errorMessage = friendlyMessage(error) }
    }
}
