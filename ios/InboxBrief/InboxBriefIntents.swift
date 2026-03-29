import AppIntents
import Foundation

@available(iOS 16.0, *)
struct SummariseInboxIntent: AppIntent {
    static var title: LocalizedStringResource = "Summarise inbox"
    static var description = IntentDescription("Reads your inbox brief from your hosted server.")

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let text = try await BriefFetcher.fetchSpokenBrief()
        return .result(dialog: IntentDialog(text))
    }
}

@available(iOS 16.0, *)
struct InboxBriefShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: SummariseInboxIntent(),
            phrases: [
                "Summarise inbox in Inbox Brief",
                "Summarise my inbox in Inbox Brief"
            ],
            shortTitle: "Summarise inbox",
            systemImageName: "envelope.open"
        )
    }
}
