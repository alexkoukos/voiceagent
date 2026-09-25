import SwiftUI

/// Main screen: pick a friend, pick a prank, call. Everything else is tucked away.
struct NewCallView: View {
    private let api = APIClient()
    static let voices: [(id: String, label: String)] = [
        ("default", "Γυναικεία, ήρεμη (Kore)"), ("Aoede", "Γυναικεία, ανάλαφρη (Aoede)"),
        ("Puck", "Αντρική, κεφάτη (Puck)"), ("Charon", "Αντρική, ήρεμη (Charon)"),
        ("Fenrir", "Αντρική, ενθουσιώδης (Fenrir)"),
    ]
    private static let customId = "custom"

    @State private var friends: [Friend] = []
    @State private var templates: [PromptTemplate] = []
    @State private var loaded = false
    @State private var friendId = ""
    @State private var prankId = ""
    @State private var persona = ""
    @State private var scenario = ""
    @State private var context = ""
    @State private var reveal = ""
    @State private var voice = "default"
    @State private var maxMinutes = 3
    @State private var fromOwnNumber = false
    @State private var ownNumberAvailable = false
    @State private var showAddFriend = false
    @State private var showSettings = false
    @State private var liveCall: LiveCallRequest?
    @State private var errorMessage: String?
    @State private var notice: String?
    @State private var starting = false

    private var needsSetup: Bool { Settings.apiKey.isEmpty }
    private var isCustom: Bool { prankId == Self.customId }
    private var selectedFriend: Friend? { friends.first { $0.id == friendId } }
    private var canCall: Bool {
        guard selectedFriend != nil, !starting else { return false }
        if isCustom { return !persona.trimmed.isEmpty && !scenario.trimmed.isEmpty }
        return templates.contains { $0.id == prankId }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Space.xxl) {
                    if needsSetup {
                        setupCard
                    } else {
                        friendSection
                        prankSection
                        optionsSection
                    }
                    if let errorMessage { ErrorBanner(text: errorMessage) }
                }
                .padding(Space.l)
                .padding(.bottom, Space.xxxl)
            }
            .background(Palette.background)
            .navigationTitle("Νέα φάρσα")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Ρυθμίσεις")
                }
            }
            .safeAreaInset(edge: .bottom) { if !needsSetup { callBar } }
            .task { await load() }
            .refreshable { await load() }
            .sheet(isPresented: $showAddFriend) {
                AddFriendView { friend in
                    friends.append(friend)
                    friendId = friend.id
                }
            }
            .sheet(isPresented: $showSettings, onDismiss: { Task { await load() } }) { SettingsView() }
            .fullScreenCover(item: $liveCall) { req in
                LiveCallView(callId: req.call.id, friendName: req.friendName, request: req.request)
            }
        }
    }

    // MARK: Sections

    private var setupCard: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            Text("Καλώς ήρθες! 👋").font(.title2.bold())
            Text("Σύνδεσε την εφαρμογή με τον server σου για να ξεκινήσεις.")
                .foregroundStyle(.secondary)
            Button("Σύνδεση") { showSettings = true }
                .buttonStyle(PrimaryButtonStyle())
                .padding(.top, Space.s)
        }
        .padding(Space.xl)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
    }

    private var friendSection: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            SectionTitle("Σε ποιον;")
            if loaded && friends.isEmpty {
                Button { showAddFriend = true } label: {
                    Label("Πρόσθεσε τον πρώτο σου φίλο", systemImage: "person.badge.plus")
                        .frame(maxWidth: .infinity, minHeight: 52)
                }
                .buttonStyle(.bordered)
                .tint(Palette.accent)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Space.s) {
                        ForEach(friends) { f in
                            FriendChip(name: f.name, selected: f.id == friendId) { friendId = f.id }
                        }
                        Button { showAddFriend = true } label: {
                            Image(systemName: "plus")
                                .font(.body.weight(.semibold))
                                .frame(width: 44, height: 44)
                                .background(Palette.card, in: Circle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Νέος φίλος")
                    }
                }
                .redacted(reason: loaded ? [] : .placeholder)
            }
        }
    }

    private var prankSection: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            SectionTitle("Ποια φάρσα;")
            if !loaded {
                ForEach(0..<3, id: \.self) { _ in
                    PrankCard(title: "Φάρσα φόρτωση", subtitle: "Περιγραφή της φάρσας που φορτώνει", selected: false) {}
                }
                .redacted(reason: .placeholder)
            } else {
                ForEach(templates) { t in
                    PrankCard(title: t.title, subtitle: t.scenario, selected: prankId == t.id) { select(t) }
                }
                PrankCard(title: "Δική μου φάρσα", subtitle: "Γράψε εσύ ποιος παίρνει και τι θα πει.",
                          selected: isCustom) { prankId = Self.customId }
                if isCustom { customFields }
            }
        }
    }

    private var customFields: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            Field("Ποιος παίρνει;", text: $persona, hint: "π.χ. υπάλληλος της ΔΕΗ")
            Field("Ποια είναι η φάρσα;", text: $scenario, hint: "π.χ. του λες ότι θα του κόψουν το ρεύμα για…")
            Field("Τι ξέρει ο AI για τον φίλο; (προαιρετικό)", text: $context, hint: "π.χ. είναι Ολυμπιακός, λέει συνέχεια «ρε φίλε»")
            Field("Πότε να αποκαλύψει τη φάρσα; (προαιρετικό)", text: $reveal, hint: "π.χ. μόλις θυμώσει")
            Button { Task { await saveTemplate() } } label: {
                Label("Αποθήκευση για επόμενη φορά", systemImage: "bookmark")
            }
            .disabled(persona.trimmed.isEmpty || scenario.trimmed.isEmpty)
            .tint(Palette.accent)
            if let notice { Text(notice).font(.footnote).foregroundStyle(Palette.success) }
        }
        .padding(Space.l)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
    }

    private var optionsSection: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: Space.l) {
                Picker("Φωνή", selection: $voice) {
                    ForEach(Self.voices, id: \.id) { Text($0.label).tag($0.id) }
                }
                .pickerStyle(.menu)
                Stepper("Μέγιστη διάρκεια: \(maxMinutes) λεπτά", value: $maxMinutes, in: 1...5)
                if ownNumberAvailable {
                    Toggle("Κλήση από το δικό μου νούμερο", isOn: $fromOwnNumber)
                        .tint(Palette.accent)
                }
            }
            .padding(.top, Space.m)
        } label: {
            Label("Ρυθμίσεις κλήσης", systemImage: "slider.horizontal.3")
                .font(.body.weight(.semibold))
                .foregroundStyle(.primary)
        }
        .tint(.secondary)
        .padding(Space.l)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
    }

    private var callBar: some View {
        Button { Task { await startCall() } } label: {
            Label(starting ? "Ξεκινάει…" : (selectedFriend.map { "Κάλεσε · \($0.name)" } ?? "Διάλεξε φίλο και φάρσα"),
                  systemImage: "phone.fill")
        }
        .buttonStyle(PrimaryButtonStyle())
        .disabled(!canCall)
        .padding(.horizontal, Space.l)
        .padding(.vertical, Space.m)
        .background(.bar)
    }

    // MARK: Actions

    private func select(_ t: PromptTemplate) {
        prankId = t.id
        persona = t.persona; scenario = t.scenario; context = t.context; reveal = t.reveal
    }

    private func load() async {
        guard !needsSetup else { return }
        do {
            async let f = api.friends()
            async let t = api.templates()
            async let o = api.options()
            (friends, templates) = try await (f, t)
            ownNumberAvailable = (try? await o)?.ownNumberAvailable ?? false
            if !ownNumberAvailable { fromOwnNumber = false }
            if friendId.isEmpty, friends.count == 1 { friendId = friends[0].id }
            errorMessage = nil
        } catch { errorMessage = friendlyMessage(error) }
        loaded = true
    }

    private func startCall() async {
        guard let friend = selectedFriend else { return }
        starting = true
        defer { starting = false }
        let request = NewCall(friendId: friend.id, persona: persona.trimmed, scenario: scenario.trimmed,
                              context: context.trimmed, reveal: reveal.trimmed, voice: voice,
                              maxDurationSeconds: maxMinutes * 60, fromOwnNumber: fromOwnNumber)
        do {
            errorMessage = nil
            let call = try await api.startCall(request)
            liveCall = LiveCallRequest(call: call, friendName: friend.name, request: request)
        } catch { errorMessage = friendlyMessage(error) }
    }

    private func saveTemplate() async {
        do {
            let t = try await api.addTemplate(NewTemplate(
                title: String(scenario.trimmed.prefix(40)), persona: persona.trimmed,
                scenario: scenario.trimmed, context: context.trimmed, reveal: reveal.trimmed))
            templates.append(t)
            prankId = t.id
            notice = "Αποθηκεύτηκε στις φάρσες σου."
        } catch { errorMessage = friendlyMessage(error) }
    }
}

/// What the live-call screen needs, including the request so a missed call can be retried.
struct LiveCallRequest: Identifiable {
    let call: Call
    let friendName: String
    let request: NewCall
    var id: String { call.id }
}

struct SectionTitle: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text).font(.title3.bold()).accessibilityAddTraits(.isHeader)
    }
}

struct Field: View {
    let label: String
    @Binding var text: String
    let hint: String
    init(_ label: String, text: Binding<String>, hint: String) {
        self.label = label; self._text = text; self.hint = hint
    }
    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            Text(label).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
            TextField(hint, text: $text, axis: .vertical)
                .lineLimit(1...4)
                .padding(Space.m)
                .background(Palette.background, in: RoundedRectangle(cornerRadius: Radius.control, style: .continuous))
        }
    }
}

struct ErrorBanner: View {
    let text: String
    var body: some View {
        Label(text, systemImage: "exclamationmark.triangle.fill")
            .font(.subheadline)
            .foregroundStyle(Palette.danger)
            .padding(Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.danger.opacity(0.1), in: RoundedRectangle(cornerRadius: Radius.control, style: .continuous))
    }
}

extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}

struct AddFriendView: View {
    @Environment(\.dismiss) private var dismiss
    let onAdded: (Friend) -> Void
    @State private var name = ""
    @State private var phone = ""
    @State private var errorMessage: String?
    @State private var saving = false

    /// "+30 690 762-6384" / "0030 (690) 7626384" -> "+306907626384"; the backend normalizes the same way.
    private var normalizedPhone: String {
        var p = phone.filter { !" -().\u{00A0}".contains($0) && !$0.isWhitespace }
        if p.hasPrefix("00") { p = "+" + p.dropFirst(2) }
        return p
    }
    private var phoneValid: Bool { normalizedPhone.range(of: #"^\+\d{8,15}$"#, options: .regularExpression) != nil }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Όνομα", text: $name)
                        .textContentType(.name)
                    TextField("Τηλέφωνο, π.χ. +30 69…", text: $phone)
                        .keyboardType(.phonePad)
                        .textContentType(.telephoneNumber)
                } footer: {
                    if !phone.isEmpty && !phoneValid {
                        Text("Γράψε τον αριθμό με τον κωδικό χώρας, π.χ. +30 691 234 5678.")
                    }
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(Palette.danger) }
            }
            .navigationTitle("Νέος φίλος")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Άκυρο") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "…" : "Αποθήκευση") { Task { await save() } }
                        .disabled(name.trimmed.isEmpty || !phoneValid || saving)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            onAdded(try await APIClient().addFriend(NewFriend(name: name.trimmed, phoneNumber: normalizedPhone)))
            dismiss()
        } catch { errorMessage = friendlyMessage(error) }
    }
}
