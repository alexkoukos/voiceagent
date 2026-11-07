import SwiftUI
import LiveKit

/// A caller asked for a person (W1): see the live transcript, then join the call to talk
/// (the agent steps out) or just listen.
struct HandoffView: View {
    @Environment(\.dismiss) private var dismiss
    let practiceId: String
    let handoff: Handoff
    @State private var call: ReceptionistCall?
    @State private var room: Room?
    @State private var listenOnly = false
    @State private var connecting = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                VStack(spacing: Space.s) {
                    Text(call?.caller ?? "Πελάτης").font(.title.bold())
                    Label(room == nil ? "Περιμένει στη γραμμή" : (listenOnly ? "Ακούς την κλήση" : "Είσαι στην κλήση"),
                          systemImage: room == nil ? "hourglass" : "waveform")
                        .font(.subheadline.weight(.semibold))
                }
                .padding(Space.l)
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: Space.m) {
                            ForEach(call?.transcriptEntries ?? []) { e in
                                TranscriptBubble(entry: e, friendName: "Πελάτης").id(e.id)
                            }
                        }
                        .padding(Space.l)
                    }
                    .onChange(of: call?.transcriptEntries?.count) {
                        if let last = call?.transcriptEntries?.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
                    }
                }
                if let errorMessage { ErrorBanner(text: errorMessage).padding(.horizontal, Space.l) }
                actions.padding(Space.l)
            }
            .background(Palette.background)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Κλείσιμο") { Task { await leave(); dismiss() } }
                }
            }
            .task { await follow() }
        }
    }

    @ViewBuilder private var actions: some View {
        if room == nil {
            VStack(spacing: Space.s) {
                Button { Task { await join(listen: false) } } label: {
                    Label(connecting ? "Σύνδεση…" : "Μπες στην κλήση", systemImage: "phone.fill")
                }
                .buttonStyle(PrimaryButtonStyle())
                Button { Task { await join(listen: true) } } label: {
                    Label("Άκου μόνο", systemImage: "ear").frame(maxWidth: .infinity, minHeight: 44)
                }
                .tint(Palette.ink)
            }
            .disabled(connecting)
        } else {
            Button { Task { await leave() } } label: { Label("Έξοδος από την κλήση", systemImage: "phone.down.fill") }
                .buttonStyle(PrimaryButtonStyle(color: Palette.danger, textColor: .white))
        }
    }

    private func join(listen: Bool) async {
        connecting = true
        defer { connecting = false }
        do {
            let access = try await APIClient().joinHandoff(practiceId, handoff.id, listenOnly: listen)
            let r = Room()
            try await r.connect(url: access.url, token: access.token)
            if !listen { try await r.localParticipant.setMicrophone(enabled: true) }
            listenOnly = listen
            room = r
            errorMessage = nil
        } catch {
            errorMessage = (error as? APIError).map(friendlyMessage) ?? "Δεν ήταν δυνατή η σύνδεση στην κλήση."
        }
    }

    private func leave() async {
        await room?.disconnect()
        room = nil
    }

    private func follow() async {
        while !Task.isCancelled {
            if let c = try? await APIClient().practiceCall(practiceId, handoff.callId) { call = c }
            try? await Task.sleep(for: .seconds(1.5))
        }
    }
}
