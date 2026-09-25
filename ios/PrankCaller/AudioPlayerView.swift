import SwiftUI
import AVFoundation

/// Owns playback of one recording: the AVPlayer, current time and duration.
@MainActor @Observable
final class RecordingPlayer {
    private(set) var isPlaying = false
    private(set) var isLoading = false
    private(set) var current: Double = 0
    private(set) var duration: Double = 0
    var errorMessage: String?

    private var player: AVPlayer?
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?
    /// While the finger is on the scrubber, the observer must not move it.
    private var scrubbing = false
    private let load: () async throws -> URL

    init(load: @escaping () async throws -> URL) { self.load = load }

    func togglePlay() async {
        if player == nil { await prepare() }
        guard let player else { return }
        if isPlaying {
            player.pause()
        } else {
            if duration > 0, current >= duration - 0.25 { await seek(to: 0) }
            player.play()
        }
        isPlaying.toggle()
    }

    func skip(_ seconds: Double) async {
        if player == nil { await prepare() }
        await seek(to: min(max(current + seconds, 0), max(duration, 0)))
    }

    /// Scrubber drag: move the time label live, seek once the finger lifts.
    func scrub(to seconds: Double, ended: Bool) async {
        scrubbing = !ended
        current = seconds
        if ended { await seek(to: seconds) }
    }

    func stop() {
        player?.pause()
        isPlaying = false
    }

    private func seek(to seconds: Double) async {
        current = seconds
        await player?.seek(to: CMTime(seconds: seconds, preferredTimescale: 600),
                           toleranceBefore: .zero, toleranceAfter: .zero)
    }

    private func prepare() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let url = try await load()
            try? AVAudioSession.sharedInstance().setCategory(.playback)
            let item = AVPlayerItem(url: url)
            let p = AVPlayer(playerItem: item)
            if let d = try? await item.asset.load(.duration), d.isNumeric { duration = d.seconds }
            timeObserver = p.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.25, preferredTimescale: 600),
                                                     queue: .main) { [weak self] time in
                MainActor.assumeIsolated {
                    guard let self, !self.scrubbing else { return }
                    self.current = time.seconds
                    if self.duration == 0, let d = p.currentItem?.duration, d.isNumeric { self.duration = d.seconds }
                }
            }
            endObserver = NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification,
                                                                 object: item, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.isPlaying = false }
            }
            player = p
            errorMessage = nil
        } catch {
            errorMessage = friendlyMessage(error)
        }
    }

    func tearDown() {
        stop()
        if let timeObserver { player?.removeTimeObserver(timeObserver) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        timeObserver = nil; endObserver = nil; player = nil
    }
}

/// Spotify-style player: drag the bar to scrub, elapsed / remaining time, ±15 s.
struct AudioPlayerView: View {
    let player: RecordingPlayer
    let onDelete: () -> Void

    var body: some View {
        VStack(spacing: Space.l) {
            HStack {
                Text("Ηχογράφηση").font(.body.weight(.semibold))
                Spacer()
                Button(role: .destructive, action: onDelete) { Image(systemName: "trash") }
                    .frame(width: 44, height: 44)
                    .accessibilityLabel("Διαγραφή ηχογράφησης")
            }
            Scrubber(value: player.current, total: player.duration) { seconds, ended in
                Task { await player.scrub(to: seconds, ended: ended) }
            }
            HStack {
                Text(format(player.current))
                Spacer()
                Text("-" + format(max(player.duration - player.current, 0)))
            }
            .font(.caption.monospacedDigit())
            .foregroundStyle(.secondary)
            HStack(spacing: Space.xxl) {
                Button { Task { await player.skip(-15) } } label: { Image(systemName: "gobackward.15").font(.title2) }
                    .accessibilityLabel("Πίσω 15 δευτερόλεπτα")
                Button { Task { await player.togglePlay() } } label: {
                    ZStack {
                        Circle().fill(Palette.ink).frame(width: 64, height: 64)
                        if player.isLoading {
                            ProgressView().tint(Palette.onInk)
                        } else {
                            Image(systemName: player.isPlaying ? "pause.fill" : "play.fill")
                                .font(.title2).foregroundStyle(Palette.onInk)
                                .offset(x: player.isPlaying ? 0 : 2)
                        }
                    }
                }
                .accessibilityLabel(player.isPlaying ? "Παύση" : "Αναπαραγωγή")
                Button { Task { await player.skip(15) } } label: { Image(systemName: "goforward.15").font(.title2) }
                    .accessibilityLabel("Μπροστά 15 δευτερόλεπτα")
            }
            .foregroundStyle(.primary)
            .buttonStyle(.plain)
            if let error = player.errorMessage { ErrorBanner(text: error) }
        }
        .padding(Space.l)
        .glass()
    }

    private func format(_ seconds: Double) -> String {
        guard seconds.isFinite else { return "0:00" }
        return Duration.seconds(Int(seconds.rounded(.down))).formatted(.time(pattern: .minuteSecond))
    }
}

/// Thin draggable progress bar with a knob that grows while you drag.
struct Scrubber: View {
    let value: Double
    let total: Double
    let onChange: (Double, _ ended: Bool) -> Void
    @State private var dragging = false

    var body: some View {
        GeometryReader { geo in
            let fraction = total > 0 ? min(max(value / total, 0), 1) : 0
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.15)).frame(height: dragging ? 8 : 4)
                Capsule().fill(Palette.ink).frame(width: geo.size.width * fraction, height: dragging ? 8 : 4)
                Circle().fill(Palette.ink)
                    .frame(width: dragging ? 20 : 12, height: dragging ? 20 : 12)
                    .offset(x: geo.size.width * fraction - (dragging ? 10 : 6))
            }
            .frame(maxHeight: .infinity)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { g in
                        guard total > 0 else { return }
                        dragging = true
                        onChange(min(max(g.location.x / geo.size.width, 0), 1) * total, false)
                    }
                    .onEnded { g in
                        guard total > 0 else { return }
                        dragging = false
                        onChange(min(max(g.location.x / geo.size.width, 0), 1) * total, true)
                    }
            )
            .animation(.snappy(duration: 0.15), value: dragging)
        }
        .frame(height: 32)
        .accessibilityElement()
        .accessibilityLabel("Θέση ηχογράφησης")
        .accessibilityValue("\(Int(value)) από \(Int(total)) δευτερόλεπτα")
        .accessibilityAdjustableAction { direction in
            let step = direction == .increment ? 15.0 : -15.0
            onChange(min(max(value + step, 0), total), true)
        }
    }
}
