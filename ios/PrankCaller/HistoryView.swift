import SwiftUI

struct HistoryView: View {
    private let api = APIClient()
    @State private var calls: [Call] = []
    @State private var friends: [String: String] = [:]
    @State private var loaded = false
    @State private var errorMessage: String?
    @State private var liveCall: Call?

    private func name(_ call: Call) -> String { friends[call.friendId] ?? "Φίλος" }

    var body: some View {
        NavigationStack {
            List {
                if let errorMessage { ErrorBanner(text: errorMessage).listRowSeparator(.hidden) }
                ForEach(calls) { call in
                    if call.isInProgress {
                        Button { liveCall = call } label: { CallRow(call: call, friendName: name(call)) }
                    } else {
                        NavigationLink(value: call.id) { CallRow(call: call, friendName: name(call)) }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .overlay {
                if loaded && calls.isEmpty && errorMessage == nil {
                    ContentUnavailableView("Καμία κλήση ακόμα", systemImage: "phone",
                                           description: Text("Οι κλήσεις σου θα εμφανίζονται εδώ, με ηχογράφηση και απομαγνητοφώνηση."))
                }
            }
            .navigationTitle("Ιστορικό")
            .navigationDestination(for: String.self) { id in
                CallDetailView(callId: id, friendName: calls.first { $0.id == id }.map(name) ?? "Φίλος")
            }
            .fullScreenCover(item: $liveCall) { LiveCallView(callId: $0.id, friendName: name($0), request: nil) }
            .task {
                while !Task.isCancelled {
                    await load()
                    try? await Task.sleep(for: .seconds(5))
                }
            }
            .refreshable { await load() }
        }
    }

    private func load() async {
        guard !Settings.apiKey.isEmpty else { loaded = true; return }
        do {
            async let c = api.calls()
            async let f = api.friends()
            let (cs, fs) = try await (c, f)
            calls = cs
            friends = Dictionary(fs.map { ($0.id, $0.name) }, uniquingKeysWith: { a, _ in a })
            errorMessage = nil
        } catch { errorMessage = friendlyMessage(error) }
        loaded = true
    }
}

/// One history row: who, which prank, how it went and when.
struct CallRow: View {
    let call: Call
    let friendName: String

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            HStack(alignment: .firstTextBaseline) {
                Text(friendName).font(.body.weight(.semibold)).foregroundStyle(.primary)
                Spacer()
                Text(call.createdAt, format: .relative(presentation: .named))
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Text(call.persona).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            HStack(spacing: Space.s) {
                StatusBadge(call: call).font(.footnote)
                if call.recordingUrl != nil {
                    Image(systemName: "waveform").font(.footnote).foregroundStyle(.secondary)
                        .accessibilityLabel("Έχει ηχογράφηση")
                }
            }
        }
        .padding(.vertical, Space.xs)
    }
}

struct CallDetailView: View {
    let callId: String
    let friendName: String
    @State private var call: Call?
    @State private var player: RecordingPlayer?
    @State private var errorMessage: String?
    @State private var confirmDelete = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.xl) {
                if let call {
                    VStack(alignment: .leading, spacing: Space.s) {
                        StatusBadge(call: call)
                        Text(call.persona).foregroundStyle(.secondary)
                        if let d = call.durationSeconds {
                            Text("Διάρκεια \(Duration.seconds(d).formatted(.time(pattern: .minuteSecond)))")
                                .font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                    if call.recordingUrl != nil, let player {
                        AudioPlayerView(player: player) { confirmDelete = true }
                    }
                    if let entries = call.transcriptEntries, !entries.isEmpty {
                        VStack(alignment: .leading, spacing: Space.m) {
                            SectionTitle("Τι ειπώθηκε")
                            ForEach(entries) { TranscriptBubble(entry: $0, friendName: friendName) }
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
        .navigationTitle(friendName)
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .onDisappear { player?.tearDown() }
        .confirmationDialog("Να διαγραφεί οριστικά η ηχογράφηση και η απομαγνητοφώνηση;",
                            isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Διαγραφή", role: .destructive) { Task { await deleteRecording() } }
            Button("Άκυρο", role: .cancel) {}
        }
    }

    private func load() async {
        do {
            call = try await APIClient().call(callId)
            if call?.recordingUrl != nil, player == nil {
                let id = callId
                player = RecordingPlayer { try await APIClient().recordingURL(id) }
            }
        } catch { errorMessage = friendlyMessage(error) }
    }

    private func deleteRecording() async {
        do {
            player?.tearDown()
            try await APIClient().deleteRecording(callId)
            player = nil
            await load()
        } catch { errorMessage = friendlyMessage(error) }
    }
}
