import SwiftUI

struct LiveCallView: View {
    @Environment(\.dismiss) private var dismiss
    let callId: String
    @State private var call: Call?
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                HStack {
                    Text((call?.status ?? "starting").capitalized).font(.headline)
                    Spacer()
                    if let d = call?.durationSeconds { Text("\(d)s").monospacedDigit() }
                }
                .padding()

                TranscriptList(entries: call?.transcriptEntries ?? [])

                if let errorMessage { Text(errorMessage).foregroundStyle(.red).padding() }

                if call?.isInProgress ?? true {
                    Button(role: .destructive) {
                        Task { await hangUp() }
                    } label: {
                        Label("Hang up", systemImage: "phone.down.fill").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.red)
                    .padding()
                } else {
                    Button("Done") { dismiss() }
                        .buttonStyle(.borderedProminent)
                        .padding()
                }
            }
            .navigationTitle("Live call")
            .navigationBarTitleDisplayMode(.inline)
            .task { await poll() }
        }
    }

    private func poll() async {
        let api = APIClient()
        while !Task.isCancelled {
            do {
                call = try await api.call(callId)
                errorMessage = nil
                if call?.isInProgress == false { return }
            } catch { errorMessage = error.localizedDescription }
            try? await Task.sleep(for: .seconds(1))
        }
    }

    private func hangUp() async {
        do { call = try await APIClient().hangUp(callId) }
        catch { errorMessage = error.localizedDescription }
    }
}

struct TranscriptList: View {
    let entries: [TranscriptEntry]

    var body: some View {
        ScrollViewReader { proxy in
            List(entries) { e in
                VStack(alignment: .leading, spacing: 2) {
                    Text(e.role == "agent" ? "Agent" : "Friend")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(e.text)
                }
                .id(e.id)
            }
            .listStyle(.plain)
            .onChange(of: entries.count) {
                if let last = entries.last { proxy.scrollTo(last.id, anchor: .bottom) }
            }
        }
    }
}
