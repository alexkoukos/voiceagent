import SwiftUI

/// The call in progress: big status, live transcript, one action (hang up / done / retry).
struct LiveCallView: View {
    @Environment(\.dismiss) private var dismiss
    @State var callId: String
    let friendName: String
    /// The original request, so a call that wasn't answered can be retried in one tap.
    let request: NewCall?
    @State private var call: Call?
    @State private var errorMessage: String?
    @State private var busy = false
    @State private var elapsed = 0

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                header
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: Space.m) {
                            ForEach(call?.transcriptEntries ?? []) { e in
                                TranscriptBubble(entry: e, friendName: friendName).id(e.id)
                            }
                        }
                        .padding(Space.l)
                    }
                    .onChange(of: call?.transcriptEntries?.count) {
                        if let last = call?.transcriptEntries?.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
                    }
                }
                if let errorMessage { ErrorBanner(text: errorMessage).padding(.horizontal, Space.l) }
                actions
            }
            .background(Palette.background)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    if !(call?.isInProgress ?? true) { Button("Κλείσιμο") { dismiss() } }
                }
            }
            .task(id: callId) { await follow() }
            .task(id: callId) { await tick() }
        }
    }

    private var header: some View {
        VStack(spacing: Space.s) {
            Text(friendName).font(.largeTitle.bold())
            if let call {
                StatusBadge(call: call)
            } else {
                Label("Καλεί…", systemImage: "phone.arrow.up.right")
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.waiting)
            }
            if call?.status == "active" || call?.durationSeconds != nil {
                Text(Duration.seconds(call?.durationSeconds ?? elapsed).formatted(.time(pattern: .minuteSecond)))
                    .font(.title3.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, Space.xl)
    }

    @ViewBuilder private var actions: some View {
        VStack(spacing: Space.m) {
            if call?.isInProgress ?? true {
                Button { Task { await hangUp() } } label: {
                    Label(call?.status == "queued" ? "Ακύρωση" : "Κλείσε την κλήση", systemImage: "phone.down.fill")
                }
                .buttonStyle(PrimaryButtonStyle(color: Palette.danger))
                .disabled(busy)
            } else if call?.canRetry == true, request != nil {
                Button { Task { await retry() } } label: { Label("Δοκίμασε ξανά", systemImage: "arrow.clockwise") }
                    .buttonStyle(PrimaryButtonStyle())
                    .disabled(busy)
                Button("Τέλος") { dismiss() }.frame(minHeight: 44)
            } else {
                Button("Τέλος") { dismiss() }.buttonStyle(PrimaryButtonStyle())
            }
        }
        .padding(Space.l)
        .background(.bar)
    }

    // MARK: Actions

    private func follow() async {
        let api = APIClient()
        // Preferred: the backend pushes "changed" over a WebSocket and we refetch.
        if let socket = try? api.updates(callId) {
            socket.resume()
            defer { socket.cancel(with: .goingAway, reason: nil) }
            while !Task.isCancelled {
                do {
                    _ = try await socket.receive()
                    call = try await api.call(callId)
                    errorMessage = nil
                    if call?.isInProgress == false { return }
                } catch { break }
            }
        }
        // Fallback if the socket fails or drops: poll once a second.
        while !Task.isCancelled {
            do {
                call = try await api.call(callId)
                errorMessage = nil
                if call?.isInProgress == false { return }
            } catch { errorMessage = friendlyMessage(error) }
            try? await Task.sleep(for: .seconds(1))
        }
    }

    /// Local timer while the call is live; the server's duration takes over when it ends.
    private func tick() async {
        elapsed = 0
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(1))
            if call?.status == "active" { elapsed += 1 }
            if call?.isInProgress == false { return }
        }
    }

    private func hangUp() async {
        busy = true
        defer { busy = false }
        do { call = try await APIClient().hangUp(callId) } catch { errorMessage = friendlyMessage(error) }
    }

    private func retry() async {
        guard let request else { return }
        busy = true
        defer { busy = false }
        do {
            let next = try await APIClient().startCall(request)
            call = next
            errorMessage = nil
            callId = next.id
        } catch { errorMessage = friendlyMessage(error) }
    }
}
