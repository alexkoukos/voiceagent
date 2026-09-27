import Foundation
import LocalAuthentication
import Security

/// Device-bound secrets. A fresh app unlock supplies the reusable authentication context.
enum Keychain {
    private static let service = "PrankCaller"
    private static let mutex = NSLock()
    private static var storedContext: LAContext?
    static var context: LAContext? {
        get { mutex.withLock { storedContext } }
        set { mutex.withLock { storedContext = newValue } }
    }

    private static func query(_ key: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: key]
    }

    static func get(_ key: String) -> String? {
        guard let context else { return nil }
        var q = query(key)
        q[kSecUseAuthenticationContext as String] = context
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func set(_ value: String, for key: String) throws {
        guard let context else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(errSecAuthFailed)) }
        var q = query(key)
        q[kSecUseAuthenticationContext as String] = context
        if value.isEmpty {
            let status = SecItemDelete(q as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else {
                throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
            }
            return
        }
        var error: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly,
                [.biometryCurrentSet, .or, .devicePasscode], &error) else {
            throw error!.takeRetainedValue() as Error
        }
        let attributes: [String: Any] = [kSecValueData as String: Data(value.utf8), kSecAttrAccessControl as String: access]
        // Update in place: a failed write must never delete the previous credential.
        var status = SecItemUpdate(q as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            q.merge(attributes) { _, new in new }
            status = SecItemAdd(q as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }

    static func lock() {
        context?.invalidate()
        context = nil
    }
}
