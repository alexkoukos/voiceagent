import SwiftUI

// Design tokens: every spacing, radius and color in the app comes from here.
// Monochrome by design: black and white (inverted in dark mode) on translucent glass.
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
    static let control: CGFloat = 14
    static let card: CGFloat = 22
}

enum Palette {
    /// Black in light mode, white in dark mode. A fixed color, not `Color.primary`,
    /// because Liquid Glass renders `.primary` content as a lighter, vibrant grey.
    static let ink = Color(.label)
    /// The color that sits on top of `ink` (white on black, black on white).
    static let onInk = Color(.systemBackground)
    /// Faint ink wash for selected cards.
    static let inkSoft = Color(.label).opacity(0.06)
    static let background = Color(.systemGroupedBackground)
    /// Only for ending a call and deleting, following iOS conventions.
    static let danger = Color.red
}

// MARK: - Glass

extension View {
    /// Liquid Glass on iOS 26, frosted material before that.
    @ViewBuilder func glass(_ radius: CGFloat = Radius.card) -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(.regular, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
        } else {
            self.background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
        }
    }

    @ViewBuilder func glassCapsule() -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(.regular, in: Capsule())
        } else {
            self.background(.ultraThinMaterial, in: Capsule())
        }
    }
}

// MARK: - Presentational components (render input, emit events; no networking)

struct SectionTitle: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text).font(.title3.bold()).accessibilityAddTraits(.isHeader)
    }
}

struct ErrorBanner: View {
    let text: String
    var body: some View {
        Label(text, systemImage: "exclamationmark.triangle.fill")
            .font(.subheadline)
            .foregroundStyle(Palette.danger)
            .padding(Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.danger.opacity(0.1), in: RoundedRectangle(cornerRadius: Radius.control, style: .continuous))
    }
}

/// A filter chip in a horizontal picker: solid ink when selected, glass otherwise.
struct Chip: View {
    let name: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            if selected {
                label.foregroundStyle(Palette.onInk).background(Palette.ink, in: Capsule())
            } else {
                label.foregroundStyle(.primary).glassCapsule()
            }
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var label: some View {
        Text(name)
            .font(.body.weight(.semibold))
            .padding(.horizontal, Space.l)
            .frame(minHeight: 44)
    }
}

/// One line of the call transcript as a chat bubble: the caller in ink, the AI on glass.
struct TranscriptBubble: View {
    let entry: TranscriptEntry
    let friendName: String

    private var isAgent: Bool { entry.role == "agent" }

    var body: some View {
        VStack(alignment: isAgent ? .leading : .trailing, spacing: Space.xs) {
            Text(isAgent ? "AI" : friendName)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            if isAgent {
                bubble.foregroundStyle(.primary).glass(Radius.card)
            } else {
                bubble.foregroundStyle(Palette.onInk)
                    .background(Palette.ink, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
            }
        }
        .frame(maxWidth: .infinity, alignment: isAgent ? .leading : .trailing)
    }

    private var bubble: some View {
        Text(entry.text)
            .padding(.horizontal, Space.m)
            .padding(.vertical, Space.s)
    }
}

/// Full-width primary button: a solid black (white in dark mode) capsule, Apple style.
struct PrimaryButtonStyle: ButtonStyle {
    var color: Color = Palette.ink
    var textColor: Color = Palette.onInk
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .frame(maxWidth: .infinity, minHeight: 54)
            .foregroundStyle(isEnabled ? textColor : Color.secondary)
            .background(isEnabled ? color : Color(.systemGray5), in: Capsule())
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .opacity(configuration.isPressed ? 0.85 : 1)
            .animation(.snappy(duration: 0.15), value: configuration.isPressed)
    }
}
