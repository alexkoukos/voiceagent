import SwiftUI
import AVKit

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
                    ContentUnavailableView("Καμία φάρσα ακόμα", systemImage: "theatermasks",
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
    @State private var player: AVPlayer?
    @State private var playing = false
    @State private var loadingAudio = false
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
                    if call.recordingUrl != nil { recordingCard }
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
        .onDisappear { player?.pause() }
        .confirmationDialog("Να διαγραφεί οριστικά η ηχογράφηση και η απομαγνητοφώνηση;",
                            isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Διαγραφή", role: .destructive) { Task { await deleteRecording() } }
            Button("Άκυρο", role: .cancel) {}
        }
    }

    private var recordingCard: some View {
        HStack(spacing: Space.l) {
            Button { Task { await togglePlay() } } label: {
                Image(systemName: playing ? "pause.fill" : "play.fill")
                    .font(.title2)
                    .frame(width: 56, height: 56)
                    .foregroundStyle(.white)
                    .background(Palette.accent, in: Circle())
            }
            .accessibilityLabel(playing ? "Παύση" : "Αναπαραγωγή")
            .disabled(loadingAudio)
            VStack(alignment: .leading, spacing: Space.xs) {
                Text("Ηχογράφηση").font(.body.weight(.semibold))
                Text(loadingAudio ? "Φορτώνει…" : "Πάτα για ακρόαση").font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            Button(role: .destructive) { confirmDelete = true } label: { Image(systemName: "trash") }
                .frame(width: 44, height: 44)
                .accessibilityLabel("Διαγραφή ηχογράφησης")
        }
        .padding(Space.l)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
    }

    private func load() async {
        do { call = try await APIClient().call(callId) } catch { errorMessage = friendlyMessage(error) }
    }

    private func togglePlay() async {
        if let player {
            if playing { player.pause() } else {
                if player.currentItem?.currentTime() == player.currentItem?.duration { await player.seek(to: .zero) }
                player.play()
            }
            playing.toggle()
            return
        }
        loadingAudio = true
        defer { loadingAudio = false }
        do {
            let url = try await APIClient().recordingURL(callId)
            try? AVAudioSession.sharedInstance().setCategory(.playback)
            let p = AVPlayer(url: url)
            NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification,
                                                   object: p.currentItem, queue: .main) { _ in playing = false }
            player = p
            p.play()
            playing = true
            errorMessage = nil
        } catch { errorMessage = friendlyMessage(error) }
    }

    private func deleteRecording() async {
        do {
            player?.pause()
            try await APIClient().deleteRecording(callId)
            player = nil
            playing = false
            await load()
        } catch { errorMessage = friendlyMessage(error) }
    }
}
