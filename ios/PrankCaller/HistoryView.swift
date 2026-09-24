import SwiftUI
import AVKit

struct HistoryView: View {
    private let api = APIClient()
    @State private var calls: [Call] = []
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            List(calls) { call in
                NavigationLink(value: call.id) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(call.scenario).lineLimit(1)
                        Text("\(call.status) · \(call.createdAt.formatted(date: .abbreviated, time: .shortened))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .overlay { if calls.isEmpty { ContentUnavailableView("No calls yet", systemImage: "phone") } }
            .navigationTitle("History")
            .navigationDestination(for: String.self) { CallDetailView(callId: $0) }
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private func load() async {
        do { calls = try await api.calls() } catch { errorMessage = error.localizedDescription }
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
                    HStack {
                        Button { Task { await play() } } label: { Label("Play recording", systemImage: "play.fill") }
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
