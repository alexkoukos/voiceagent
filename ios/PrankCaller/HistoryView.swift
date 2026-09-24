import SwiftUI
import AVKit

struct HistoryView: View {
    private let api = APIClient()
    @State private var calls: [Call] = []
    @State private var errorMessage: String?
    @State private var liveCall: Call?

    private var active: [Call] { calls.filter(\.isInProgress) }
    private var past: [Call] { calls.filter { !$0.isInProgress } }

    var body: some View {
        NavigationStack {
            List {
                if !active.isEmpty {
                    Section("Active calls") {
                        ForEach(active) { call in
                            Button { liveCall = call } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(call.scenario).lineLimit(1)
                                    Text(call.status.capitalized).font(.caption).foregroundStyle(.orange)
                                }
                            }
                        }
                    }
                }
                Section(active.isEmpty ? "" : "Past calls") {
                    ForEach(past) { call in
                        NavigationLink(value: call.id) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(call.scenario).lineLimit(1)
                                Text("\(call.status) · \(call.createdAt.formatted(date: .abbreviated, time: .shortened))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
            }
            .overlay { if calls.isEmpty { ContentUnavailableView("No calls yet", systemImage: "phone") } }
            .navigationTitle("History")
            .navigationDestination(for: String.self) { CallDetailView(callId: $0) }
            .fullScreenCover(item: $liveCall) { LiveCallView(callId: $0.id) }
            .task {
                while !Task.isCancelled {
                    await load()
                    try? await Task.sleep(for: .seconds(3))
                }
            }
            .refreshable { await load() }
        }
    }

    private func load() async {
        do { calls = try await api.calls(); errorMessage = nil } catch { errorMessage = error.localizedDescription }
    }
}

struct CallDetailView: View {
    let callId: String
    @State private var call: Call?
    @State private var player: AVPlayer?
    @State private var errorMessage: String?
    @State private var confirmDelete = false

    var body: some View {
        VStack {
            if let call {
                if call.recordingUrl != nil {
                    if let player {
                        VideoPlayer(player: player).frame(height: 80)
                    }
                    HStack {
                        Button { Task { await play() } } label: { Label(player == nil ? "Load recording" : "Restart", systemImage: "play.fill") }
                        Spacer()
                        Button("Delete recording", role: .destructive) { confirmDelete = true }
                    }
                    .padding(.horizontal)
                } else {
                    Text("No recording").foregroundStyle(.secondary)
                }
                TranscriptList(entries: call.transcriptEntries ?? [])
            }
            if let errorMessage { Text(errorMessage).foregroundStyle(.red).padding() }
        }
        .navigationTitle(call?.scenario ?? "Call")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .confirmationDialog("Delete this recording permanently?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { Task { await deleteRecording() } }
        }
    }

    private func load() async {
        do { call = try await APIClient().call(callId) } catch { errorMessage = error.localizedDescription }
    }

    private func play() async {
        do {
            let url = try await APIClient().recordingURL(callId)
            player = AVPlayer(url: url)
            player?.play()
        } catch { errorMessage = error.localizedDescription }
    }

    private func deleteRecording() async {
        do {
            try await APIClient().deleteRecording(callId)
            player = nil
            await load()
        } catch { errorMessage = error.localizedDescription }
    }
}
