import SwiftUI

// Design tokens: every spacing, radius and color in the app comes from here.
enum Space {
    static let xs: CGFloat = 4
    static let s: CGFloat = 8
    static let m: CGFloat = 12
    static let l: CGFloat = 16
    static let xl: CGFloat = 24
    static let xxl: CGFloat = 32
    static let xxxl: CGFloat = 48
}

enum Radius {
    static let control: CGFloat = 12
    static let card: CGFloat = 16
}

enum Palette {
    /// Coral accent (hsl 8°, 78%, 58%): buttons, selection, the app icon.
    static let accent = Color(hue: 8 / 360, saturation: 0.78, brightness: 0.93)
    /// Tinted background for selected cards; same hue as the accent.
    static let accentSoft = Color(hue: 8 / 360, saturation: 0.78, brightness: 0.93).opacity(0.12)
    static let card = Color(.secondarySystemGroupedBackground)
    static let background = Color(.systemGroupedBackground)
    static let success = Color.green
    static let danger = Color.red
    static let waiting = Color.orange
}

// MARK: - Presentational components (render input, emit events; no networking)

/// Status with icon + Greek text, so color is never the only signal.
struct StatusBadge: View {
    let call: Call

    var body: some View {
        Label(call.statusText, systemImage: call.statusIcon)
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(call.statusColor)
    }
}

/// A selectable prank card (radio-button behavior, rendered as a card).
struct PrankCard: View {
    let title: String
    let subtitle: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: Space.m) {
                VStack(alignment: .leading, spacing: Space.xs) {
                    Text(title).font(.body.weight(.semibold)).foregroundStyle(.primary)
                    Text(subtitle).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.title3)
                    .foregroundStyle(selected ? Palette.accent : Color(.tertiaryLabel))
            }
            .padding(Space.l)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? Palette.accentSoft : Palette.card, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: Radius.card, style: .continuous)
                    .strokeBorder(selected ? Palette.accent : .clear, lineWidth: 2)
            )
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// A friend "chip" in the horizontal picker.
struct FriendChip: View {
    let name: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(name)
                .font(.body.weight(.semibold))
                .padding(.horizontal, Space.l)
                .frame(minHeight: 44)
                .foregroundStyle(selected ? Color.white : Color.primary)
                .background(selected ? Palette.accent : Palette.card, in: Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// One line of the call transcript as a chat bubble.
struct TranscriptBubble: View {
    let entry: TranscriptEntry
    let friendName: String

    private var isAgent: Bool { entry.role == "agent" }

    var body: some View {
        VStack(alignment: isAgent ? .leading : .trailing, spacing: Space.xs) {
            Text(isAgent ? "AI" : friendName)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(entry.text)
                .padding(.horizontal, Space.m)
                .padding(.vertical, Space.s)
                .foregroundStyle(isAgent ? Color.primary : Color.white)
                .background(isAgent ? Palette.card : Palette.accent,
                            in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
        }
        .frame(maxWidth: .infinity, alignment: isAgent ? .leading : .trailing)
    }
}

/// Full-width primary button used for the one main action on a screen.
struct PrimaryButtonStyle: ButtonStyle {
    var color: Color = Palette.accent
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .frame(maxWidth: .infinity, minHeight: 52)
            .foregroundStyle(.white)
            .background(isEnabled ? color : Color(.systemGray3),
                        in: RoundedRectangle(cornerRadius: Radius.control, style: .continuous))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .opacity(configuration.isPressed ? 0.9 : 1)
    }
}
