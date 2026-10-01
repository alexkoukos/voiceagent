import SwiftUI

/// Speed dial: a keypad like the Phone app, for calling a number without saving it.
struct DialPadView: View {
    @Binding var number: String

    private static let keys: [(digit: String, letters: String)] = [
        ("1", ""), ("2", "ABC"), ("3", "DEF"),
        ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
        ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
        ("*", ""), ("0", "+"), ("#", ""),
    ]
    private let columns = Array(repeating: GridItem(.fixed(78), spacing: Space.xl), count: 3)
    @State private var taps = 0
    /// A long press already acted; the tap that follows on release is ignored.
    @State private var skipNextTap = false

    var body: some View {
        VStack(spacing: Space.l) {
            HStack(spacing: Space.s) {
                // Balances the delete button so the number sits in the middle, as in the Phone app.
                if !number.isEmpty { Color.clear.frame(width: 44, height: 44) }
                Text(number.isEmpty ? "Πληκτρολόγησε αριθμό" : DialPadView.display(number))
                    .font(number.isEmpty ? .body : .system(size: 32, weight: .regular, design: .rounded))
                    .foregroundStyle(number.isEmpty ? .secondary : .primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.5)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .contextMenu {
                        Button { paste() } label: { Label("Επικόλληση", systemImage: "doc.on.clipboard") }
                        if !number.isEmpty {
                            Button { UIPasteboard.general.string = number } label: { Label("Αντιγραφή", systemImage: "doc.on.doc") }
                        }
                    }
                    .accessibilityLabel(number.isEmpty ? "Κανένας αριθμός" : "Αριθμός \(number)")
                if !number.isEmpty {
                    Button {
                        if skipNextTap { skipNextTap = false } else if !number.isEmpty { number.removeLast() }
                    } label: {
                        Image(systemName: "delete.left").font(.title2).frame(width: 44, height: 44)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .simultaneousGesture(LongPressGesture().onEnded { _ in number = ""; skipNextTap = true })
                    .accessibilityLabel("Διαγραφή ψηφίου")
                    .accessibilityHint("Κράτησε πατημένο για να σβήσεις όλο τον αριθμό")
                }
            }
            LazyVGrid(columns: columns, spacing: Space.l) {
                ForEach(Self.keys, id: \.digit) { key in
                    Button { type(key.digit) } label: {
                        VStack(spacing: 0) {
                            Text(key.digit).font(.system(size: 32, weight: .regular, design: .rounded))
                            Text(key.letters.isEmpty ? " " : key.letters)
                                .font(.caption2.weight(.semibold)).tracking(1.5)
                                .foregroundStyle(.secondary)
                        }
                        .frame(width: 78, height: 78)
                        .glass(39)
                        .contentShape(Circle())
                    }
                    .buttonStyle(.plain)
                    .simultaneousGesture(LongPressGesture(minimumDuration: 0.4).onEnded { _ in
                        // Long-press 0 for "+", as in the Phone app; only at the start of a number.
                        guard key.digit == "0" else { return }
                        skipNextTap = true
                        if number.isEmpty { number = "+"; taps += 1 }
                    })
                    .accessibilityLabel(key.digit == "0" ? "0, κράτησε για +" : key.digit)
                }
            }
            .sensoryFeedback(.impact(weight: .light), trigger: taps)
        }
        .frame(maxWidth: .infinity)
    }

    private func type(_ digit: String) {
        if skipNextTap { skipNextTap = false; return }
        guard number.count < 16 else { return }
        number += digit
        taps += 1
    }

    private func paste() {
        guard let text = UIPasteboard.general.string else { return }
        let kept = text.filter { $0.isNumber || $0 == "+" }
        number = String(kept.prefix(16))
    }

    /// A Greek number as people write it: "+30 690 762 6384" / "690 762 6384".
    static func display(_ raw: String) -> String {
        var digits = raw
        var prefix = ""
        if digits.hasPrefix("+30") { prefix = "+30 "; digits.removeFirst(3) }
        else if digits.hasPrefix("+") { return raw }
        guard digits.count == 10, digits.allSatisfy(\.isNumber) else { return prefix + digits }
        let chars = Array(digits)
        return prefix + String(chars[0..<3]) + " " + String(chars[3..<6]) + " " + String(chars[6...])
    }

    /// Enough digits to be a real number; the server normalises Greek numbers to +30.
    static func isDialable(_ raw: String) -> Bool {
        raw.filter(\.isNumber).count >= 8 && !raw.contains("*") && !raw.contains("#")
    }
}
