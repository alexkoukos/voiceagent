import SwiftUI
import UserNotifications

/// Which handoff a tapped notification asked to open.
@MainActor @Observable
final class PushRouter {
    static let shared = PushRouter()
    var handoffId: String?
}

/// Sends the APNs token to the backend once we know which business this device follows.
/// Push needs a paid Apple developer account (Push Notifications capability); without it
/// registration just fails and the app falls back to live updates while open.
@MainActor
final class PushRegistration {
    static let shared = PushRegistration()
    var token: String? { didSet { send() } }
    var practiceId: String? {
        didSet {
            guard oldValue != practiceId else { return }
            send()
        }
    }

    /// Asked when the user opens the receptionist tab, not at first launch.
    func askPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            guard granted else { return }
            DispatchQueue.main.async { UIApplication.shared.registerForRemoteNotifications() }
        }
    }

    private func send() {
        guard let token, !Settings.apiKey.isEmpty else { return }
        #if DEBUG
        let sandbox = true
        #else
        let sandbox = false
        #endif
        let pid = practiceId
        Task { try? await APIClient().registerDevice(token: token, practiceId: pid, sandbox: sandbox) }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in PushRegistration.shared.token = hex }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        // Expected on a free developer account: no push, live updates only.
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async
        -> UNNotificationPresentationOptions { [.banner, .sound] }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        if let id = response.notification.request.content.userInfo["handoff_id"] as? String {
            await MainActor.run { PushRouter.shared.handoffId = id }
        }
    }
}
