import SwiftUI

struct Friend: Codable, Identifiable, Hashable {
    let id: String
    let name: String
    let phoneNumber: String
    /// Language a call to this friend starts in, from the phone prefix.
    let language: String?
}

struct PromptTemplate: Codable, Identifiable, Hashable {
    let id: String
    let title: String
    let persona: String
    let scenario: String
    let context: String
    let reveal: String
    /// Voice this preset prefers (e.g. a rough one for the grumpy caller); nil keeps the user's choice.
    let voice: String?

    /// The title without a leading emoji: the UI is monochrome.
    var displayTitle: String {
        String(title.drop { !$0.isLetter && !$0.isNumber }).trimmingCharacters(in: .whitespaces)
    }
}

struct TranscriptEntry: Codable, Identifiable {
    let role: String
    let text: String
    let createdAt: Date
    var id: String { "\(createdAt.timeIntervalSince1970)-\(role)-\(text.hashValue)" }
}

struct Call: Codable, Identifiable {
    let id: String
    let friendId: String
    let status: String
    let persona: String
    let scenario: String
    let voice: String
    let fromOwnNumber: Bool
    let recordingUrl: String?
    let durationSeconds: Int?
    let endReason: String?
    let createdAt: Date
    let startedAt: Date?
    let endedAt: Date?
    var transcriptEntries: [TranscriptEntry]?

    var isInProgress: Bool { ["pending", "queued", "dialing", "active"].contains(status) }
    /// The call ended without ever connecting (no answer, declined, ...), so it can be retried.
    var canRetry: Bool { status == "failed" }

    var statusText: String {
        switch status {
        case "pending", "dialing": return "Καλεί…"
        case "queued": return "Σε αναμονή για γραμμή"
        case "active": return "Σε εξέλιξη"
        case "completed": return "Ολοκληρώθηκε"
        case "cancelled": return "Ακυρώθηκε"
        case "failed":
            switch endReason {
            case "no_answer": return "Δεν απάντησε"
            case "declined": return "Απέρριψε την κλήση"
            case "unreachable": return "Ο αριθμός δεν είναι διαθέσιμος"
            default: return "Η κλήση απέτυχε"
            }
        default: return status
        }
    }

    var statusIcon: String {
        switch status {
        case "pending", "dialing": return "phone.arrow.up.right"
        case "queued": return "hourglass"
        case "active": return "waveform"
        case "completed": return "checkmark.circle.fill"
        case "cancelled": return "xmark.circle"
        default:
            switch endReason {
            case "no_answer": return "phone.badge.clock"
            case "declined": return "phone.down.fill"
            default: return "exclamationmark.triangle.fill"
            }
        }
    }

}

struct NewCall: Encodable {
    let friendId: String
    let persona: String
    let scenario: String
    let context: String
    let reveal: String
    let voice: String
    let maxDurationSeconds: Int
    let fromOwnNumber: Bool
    /// nil = the friend's language, from their phone prefix.
    let language: String?
}

struct ServerOptions: Decodable {
    let ownNumberAvailable: Bool
    let languages: [LanguageOption]?
}

struct LanguageOption: Decodable, Hashable, Identifiable {
    let code: String
    let name: String
    var id: String { code }
}

struct NewFriend: Encodable {
    let name: String
    let phoneNumber: String
}

struct NewTemplate: Encodable {
    let title: String
    let persona: String
    let scenario: String
    let context: String
    let reveal: String
}

struct RecordingLink: Decodable {
    let url: String
}
