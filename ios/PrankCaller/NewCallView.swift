import SwiftUI

/// Main screen: pick a friend, describe the call (or start from a preset), call.
struct NewCallView: View {
    private let api = APIClient()
    // Stable keys; the agent maps each to an ElevenLabs (or Gemini) voice.
    static let voices: [(id: String, label: String)] = [
        ("default", "Γυναικεία, ήρεμη"), ("Aoede", "Γυναικεία, ανάλαφρη"),
        ("Puck", "Αντρική, κεφάτη"), ("Charon", "Αντρική, ήρεμη"),
        ("Fenrir", "Αντρική, ενθουσιώδης"), ("Algenib", "Αντρική, τραχιά"),
    ]
    @State private var friends: [Friend] = []
    @State private var templates: [PromptTemplate] = []
    @State private var loaded = false
    @State private var friendId = ""
    @State private var prankId = ""
    /// The whole call in the user's words; a preset just fills it in.
    @State private var scenario = ""
    @State private var voice = "Puck"
    @State private var maxMinutes = 3
    @State private var fromOwnNumber = false
    @State private var ownNumberAvailable = false
    @State private var languages: [LanguageOption] = []
    /// nil = automatic, from the friend's phone prefix.
    @State private var language: String?
    @State private var showAddFriend = false
    @State private var editingFriend: Friend?
    @State private var deletingFriend: Friend?
    @State private var showSettings = false
    @State private var liveCall: LiveCallRequest?
    @State private var errorMessage: String?
    @State private var notice: String?
    @State private var starting = false

    private var needsSetup: Bool { Settings.apiKey.isEmpty }
    private var selectedFriend: Friend? { friends.first { $0.id == friendId } }
    private var canCall: Bool {
        selectedFriend != nil && !starting && !scenario.trimmed.isEmpty
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
            .navigationTitle("Νέα κλήση")
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
            .sheet(item: $editingFriend) { f in
                AddFriendView(editing: f) { updated in
                    if let i = friends.firstIndex(where: { $0.id == updated.id }) { friends[i] = updated }
                }
            }
            .confirmationDialog("Διαγραφή φίλου;", isPresented: Binding(
                get: { deletingFriend != nil }, set: { if !$0 { deletingFriend = nil } }
            ), titleVisibility: .visible, presenting: deletingFriend) { f in
                Button("Διαγραφή \(f.name)", role: .destructive) { Task { await deleteFriend(f) } }
            } message: { _ in
                Text("Οι παλιές κλήσεις μένουν στο ιστορικό.")
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
        .glass()
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
                .tint(Palette.ink)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Space.s) {
                        ForEach(friends) { f in
                            FriendChip(name: f.name, selected: f.id == friendId) { friendId = f.id }
                                .contextMenu {
                                    Button { editingFriend = f } label: { Label("Επεξεργασία", systemImage: "pencil") }
                                    Button(role: .destructive) { deletingFriend = f } label: { Label("Διαγραφή", systemImage: "trash") }
                                }
                        }
                        Button { showAddFriend = true } label: {
                            Image(systemName: "plus")
                                .font(.body.weight(.semibold))
                                .frame(width: 44, height: 44)
                                .glassCapsule()
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Νέος φίλος")
                    }
                }
                .scrollClipDisabled()
                .redacted(reason: loaded ? [] : .placeholder)
            }
        }
    }

    private var prankSection: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            SectionTitle("Τι θα γίνει;")
            if !templates.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Space.s) {
                        ForEach(templates) { t in
                            FriendChip(name: t.displayTitle, selected: prankId == t.id) { select(t) }
                        }
                    }
                }
                .scrollClipDisabled()
            }
            VStack(alignment: .leading, spacing: Space.m) {
                TextField("Γράψε ποιος παίρνει και τι θα γίνει, π.χ. «Είσαι υπάλληλος της ΔΕΗ και του λες ότι θα του κόψουν το ρεύμα γιατί το ψυγείο του καταναλώνει όσο ένα χωριό.»",
                          text: $scenario, axis: .vertical)
                    .lineLimit(5...14)
                    .onChange(of: scenario) { _, text in
                        // Edited away from the preset: it's the user's own call now.
                        if let t = templates.first(where: { $0.id == prankId }), t.scenario != text { prankId = "" }
                    }
                Button { Task { await saveTemplate() } } label: {
                    Label("Αποθήκευση ως σενάριο", systemImage: "bookmark")
                }
                .disabled(scenario.trimmed.isEmpty || !prankId.isEmpty)
                .tint(Palette.ink)
                if let notice { Text(notice).font(.footnote).foregroundStyle(.secondary) }
            }
            .padding(Space.l)
            .glass()
            .redacted(reason: loaded ? [] : .placeholder)
        }
    }

    private var optionsSection: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: Space.l) {
                Picker("Φωνή", selection: $voice) {
                    ForEach(Self.voices, id: \.id) { Text($0.label).tag($0.id) }
                }
                .pickerStyle(.menu)
                if !languages.isEmpty {
                    Picker("Γλώσσα", selection: $language) {
                        Text("Αυτόματα (\(languageName(selectedFriend?.language)))").tag(String?.none)
                        ForEach(languages) { Text($0.name).tag(Optional($0.code)) }
                    }
                    .pickerStyle(.menu)
                }
                Stepper("Μέγιστη διάρκεια: \(maxMinutes) λεπτά", value: $maxMinutes, in: 1...5)
                if ownNumberAvailable {
                    Toggle("Κλήση από το δικό μου νούμερο", isOn: $fromOwnNumber)
                        .tint(Palette.ink)
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
        .glass()
    }

    private var callBar: some View {
        Button { Task { await startCall() } } label: {
            Label(starting ? "Ξεκινάει…" : (selectedFriend.map { "Κάλεσε · \($0.name)" } ?? "Διάλεξε φίλο και γράψε τι θα γίνει"),
                  systemImage: "phone.fill")
        }
        .buttonStyle(PrimaryButtonStyle())
        .disabled(!canCall)
        .padding(.horizontal, Space.l)
        .padding(.vertical, Space.m)
        .background(.bar)
    }

    // MARK: Actions

    private func languageName(_ code: String?) -> String {
        languages.first { $0.code == code }?.name ?? "Ελληνικά"
    }

    private func select(_ t: PromptTemplate) {
        prankId = t.id
        scenario = t.scenario
        notice = nil
        if let v = t.voice, Self.voices.contains(where: { $0.id == v }) { voice = v }
    }

    private func load() async {
        guard !needsSetup else { return }
        do {
            async let f = api.friends()
            async let t = api.templates()
            async let o = api.options()
            (friends, templates) = try await (f, t)
            let options = try? await o
            ownNumberAvailable = options?.ownNumberAvailable ?? false
            languages = options?.languages ?? []
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
        let request = NewCall(friendId: friend.id, persona: "", scenario: scenario.trimmed,
                              context: "", reveal: "", voice: voice,
                              maxDurationSeconds: maxMinutes * 60, fromOwnNumber: fromOwnNumber,
                              language: language)
        do {
            errorMessage = nil
            let call = try await api.startCall(request)
            liveCall = LiveCallRequest(call: call, friendName: friend.name, request: request)
        } catch { errorMessage = friendlyMessage(error) }
    }

    private func deleteFriend(_ f: Friend) async {
        do {
            try await api.deleteFriend(f.id)
            friends.removeAll { $0.id == f.id }
            if friendId == f.id { friendId = "" }
        } catch { errorMessage = friendlyMessage(error) }
    }

    private func saveTemplate() async {
        do {
            let firstLine = scenario.trimmed.split(separator: "\n").first.map(String.init) ?? scenario.trimmed
            let t = try await api.addTemplate(NewTemplate(
                title: String(firstLine.prefix(40)), persona: "",
                scenario: scenario.trimmed, context: "", reveal: "", voice: voice))
            templates.append(t)
            prankId = t.id
            notice = "Αποθηκεύτηκε στα σενάριά σου."
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

/// Adds a friend, or edits one when `editing` is set.
struct AddFriendView: View {
    @Environment(\.dismiss) private var dismiss
    var editing: Friend? = nil
    let onSaved: (Friend) -> Void
    @State private var name: String
    @State private var phone: String

    init(editing: Friend? = nil, onSaved: @escaping (Friend) -> Void) {
        self.editing = editing
        self.onSaved = onSaved
        _name = State(initialValue: editing?.name ?? "")
        _phone = State(initialValue: editing?.phoneNumber ?? "")
    }
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
            .navigationTitle(editing == nil ? "Νέος φίλος" : "Επεξεργασία")
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
            let f = NewFriend(name: name.trimmed, phoneNumber: normalizedPhone)
            let api = APIClient()
            if let editing {
                onSaved(try await api.updateFriend(editing.id, f))
            } else {
                onSaved(try await api.addFriend(f))
            }
            dismiss()
        } catch { errorMessage = friendlyMessage(error) }
    }
}
