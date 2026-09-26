import LocalAuthentication
import SwiftUI

/// Face ID for the sensitive tabs (recordings, transcripts, patient data) and for publishing
/// changes (PRD). Passcode is the fallback. Locks again after 60 s in the background.
@Observable
final class AppLock {
    static let shared = AppLock()
    static let relockAfter: TimeInterval = 60

    private(set) var unlocked = false
    private var backgroundedAt: Date?

    /// Asks for Face ID (or the passcode). True when the owner confirmed.
    @MainActor
    static func confirm(_ reason: String) async -> Bool {
        let context = LAContext()
        context.localizedCancelTitle = "Άκυρο"
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else { return false }
        return (try? await context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason)) ?? false
    }

    @MainActor
    func unlock() async {
        if await Self.confirm("Ξεκλείδωμα κλήσεων και στοιχείων πελατών") { unlocked = true }
    }

    func phaseChanged(_ phase: ScenePhase) {
        switch phase {
        case .background:
            backgroundedAt = Date()
        case .active:
            if let t = backgroundedAt, Date().timeIntervalSince(t) > Self.relockAfter { unlocked = false }
            backgroundedAt = nil
        default:
            break
        }
    }
}

/// Shows `content` only after Face ID; covers it whenever the app is not in front, so the
/// app switcher never shows patient data.
struct Locked<Content: View>: View {
    @ViewBuilder let content: Content
    @Environment(\.scenePhase) private var phase
    private let lock = AppLock.shared

    var body: some View {
        ZStack {
            if lock.unlocked {
                content
                if phase != .active { cover }
            } else {
                cover.task { if phase == .active { await lock.unlock() } }
            }
        }
    }

    private var cover: some View {
        VStack(spacing: Space.l) {
            Image(systemName: "faceid").font(.system(size: 56, weight: .light))
            Text("Κλειδωμένο").font(.title3.weight(.semibold))
            Text("Κλήσεις, ηχογραφήσεις και στοιχεία πελατών.")
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            if phase == .active {
                Button("Ξεκλείδωμα") { Task { await lock.unlock() } }
                    .buttonStyle(PrimaryButtonStyle())
                    .frame(maxWidth: 240)
            }
        }
        .padding(Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.background)
    }
}
