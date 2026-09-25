import SwiftUI

struct NewCallView: View {
    private let api = APIClient()
    static let voices: [(id: String, label: String)] = [
        ("default", "Default (Kore, female)"), ("Puck", "Male, upbeat (Puck)"), ("Charon", "Male, calm (Charon)"),
        ("Fenrir", "Male, excitable (Fenrir)"), ("Kore", "Female, firm (Kore)"), ("Aoede", "Female, breezy (Aoede)"),
    ]

    @State private var friends: [Friend] = []
    @State private var templates: [PromptTemplate] = []
    @State private var friendId: String = ""
    @State private var persona = ""
    @State private var scenario = ""
    @State private var context = ""
    @State private var reveal = ""
    @State private var voice = "default"
    @State private var maxMinutes = 3
    @State private var fromOwnNumber = false
    @State private var ownNumberAvailable = false
    @State private var showAddFriend = false
    @State private var activeCall: Call?
    @State private var errorMessage: String?
    @State private var starting = false

    private var canCall: Bool {
        !friendId.isEmpty && !persona.isEmpty && !scenario.isEmpty && !starting
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Friend") {
                    Picker("Friend", selection: $friendId) {
                        Text("Select…").tag("")
                        ForEach(friends) { Text($0.name).tag($0.id) }
                    }
                    Button("Add friend") { showAddFriend = true }
                }
                Section("Scenario") {
                    if !templates.isEmpty {
                        Menu("Load template") {
                            ForEach(templates) { t in
                                Button(t.title) {
                                    persona = t.persona; scenario = t.scenario
                                    context = t.context; reveal = t.reveal
                                }
                            }
                        }
                    }
                    TextField("Role (e.g. delivery guy who lost a pizza)", text: $persona, axis: .vertical)
                    TextField("What's the joke?", text: $scenario, axis: .vertical)
                    TextField("Inside jokes / context", text: $context, axis: .vertical)
                    TextField("Reveal (when and how)", text: $reveal, axis: .vertical)
                    Button("Save as template") { Task { await saveTemplate() } }
                        .disabled(persona.isEmpty || scenario.isEmpty)
                }
                Section("Call settings") {
                    Picker("Voice", selection: $voice) {
                        ForEach(Self.voices, id: \.id) { Text($0.label).tag($0.id) }
                    }
                    Stepper("Max duration: \(maxMinutes) min", value: $maxMinutes, in: 1...5)
                    if ownNumberAvailable {
                        Toggle("Call from my number", isOn: $fromOwnNumber)
                    }
                }
                if let errorMessage {
                    Section { Text(errorMessage).foregroundStyle(.red) }
                }
                Section {
                    Button {
                        Task { await startCall() }
                    } label: {
                        Label(starting ? "Starting…" : "Call", systemImage: "phone.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .disabled(!canCall)
                }
            }
            .navigationTitle("New call")
            .task { await load() }
            .refreshable { await load() }
            .sheet(isPresented: $showAddFriend) {
                AddFriendView { friend in
                    friends.append(friend)
                    friendId = friend.id
                }
            }
            .fullScreenCover(item: $activeCall) { call in
                LiveCallView(callId: call.id)
            }
        }
    }

    private func load() async {
        do {
            friends = try await api.friends()
            templates = try await api.templates()
            ownNumberAvailable = try await api.options().ownNumberAvailable
            if !ownNumberAvailable { fromOwnNumber = false }
        } catch { errorMessage = error.localizedDescription }
    }

    private func startCall() async {
        starting = true
        defer { starting = false }
        do {
            errorMessage = nil
            activeCall = try await api.startCall(NewCall(
                friendId: friendId, persona: persona, scenario: scenario,
                context: context, reveal: reveal, voice: voice,
                maxDurationSeconds: maxMinutes * 60, fromOwnNumber: fromOwnNumber))
        } catch { errorMessage = error.localizedDescription }
    }

    private func saveTemplate() async {
        do {
            let title = String(scenario.prefix(40))
            templates.append(try await api.addTemplate(NewTemplate(
                title: title, persona: persona, scenario: scenario, context: context, reveal: reveal)))
        } catch { errorMessage = error.localizedDescription }
    }
}

struct AddFriendView: View {
    @Environment(\.dismiss) private var dismiss
    let onAdded: (Friend) -> Void
    @State private var name = ""
    @State private var phone = ""
    @State private var errorMessage: String?

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
                TextField("Name", text: $name)
                TextField("Phone (+306…)", text: $phone)
                    .keyboardType(.phonePad)
                if !phone.isEmpty && !phoneValid {
                    Text("Use international format, e.g. +306912345678").font(.caption).foregroundStyle(.secondary)
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
            }
            .navigationTitle("Add friend")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        Task {
                            do {
                                onAdded(try await APIClient().addFriend(NewFriend(name: name.trimmingCharacters(in: .whitespacesAndNewlines), phoneNumber: normalizedPhone)))
                                dismiss()
                            } catch { errorMessage = error.localizedDescription }
                        }
                    }
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || !phoneValid)
                }
            }
        }
    }
}
