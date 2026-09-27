import SwiftUI

struct CalendarFeedItem: Decodable, Identifiable {
    let id: String
    let name: String
    let staffId: String?
}

struct CalendarFeedsView: View {
    let practice: Practice
    @State private var feeds: [CalendarFeedItem] = []
    @State private var staff: [StaffMember] = []
    @State private var selectedStaff = ""
    @State private var name = ""
    @State private var url = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        Form {
            Section {
                ForEach(feeds) { feed in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(feed.name)
                            Text(staff.first { $0.id == feed.staffId }?.name ?? "Όλη η επιχείρηση")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button(role: .destructive) { Task { await remove(feed) } } label: {
                            Image(systemName: "trash")
                        }.disabled(busy)
                    }
                }
            } header: { Text("Συνδεδεμένα ημερολόγια") }
            Section {
                TextField("Όνομα εργαλείου", text: $name)
                TextField("https://…/calendar.ics", text: $url)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                Picker("Δεσμεύει ώρες για", selection: $selectedStaff) {
                    Text("Όλη η επιχείρηση").tag("")
                    ForEach(staff) { Text($0.name).tag($0.id) }
                }
                Button(busy ? "Σύνδεση…" : "Σύνδεση iCal") { Task { await add() } }
                    .disabled(busy || name.isEmpty || url.isEmpty)
            } header: { Text("Άλλο εργαλείο κρατήσεων") } footer: {
                Text("Μόνο ανάγνωση. Οι δεσμευμένες ώρες δεν προσφέρονται για ραντεβού. Ανανέωση κάθε 5 λεπτά.")
            }
            if let error { Section { Text(error).foregroundStyle(Palette.danger) } }
        }
        .navigationTitle("Ημερολόγια iCal")
        .task { await load() }
    }

    private func load() async {
        do {
            feeds = try await APIClient().calendarFeeds(practice.id)
            staff = try await APIClient().staff(practice.id)
        } catch { self.error = friendlyMessage(error) }
    }
    private func add() async {
        guard await AppLock.confirm("Σύνδεση εξωτερικού ημερολογίου") else { return }
        busy = true; defer { busy = false }
        do {
            try await APIClient().addCalendarFeed(practice.id, name: name, url: url, staffId: selectedStaff)
            name = ""; url = ""; error = nil
            await load()
        } catch { self.error = friendlyMessage(error) }
    }
    private func remove(_ feed: CalendarFeedItem) async {
        guard await AppLock.confirm("Αποσύνδεση ημερολογίου") else { return }
        busy = true; defer { busy = false }
        do { try await APIClient().removeCalendarFeed(practice.id, feed.id); await load() }
        catch { self.error = friendlyMessage(error) }
    }
}
