import AVFoundation
import Combine
import SwiftUI

struct ContentView: View {
    @AppStorage("apiBaseURL") private var apiBaseURL = BriefFetcher.defaultAPIBaseURL
    @AppStorage("accessKey") private var accessKey = ""
    @State private var spokenText = ""
    @State private var loading = false
    @State private var errorMessage: String?
    @StateObject private var speaker = Speaker()

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Server URL (no trailing slash)", text: $apiBaseURL, prompt: Text(BriefFetcher.defaultAPIBaseURL))
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                    SecureField("Access key (if you set DIGEST_ACCESS_KEY)", text: $accessKey)
                } header: {
                    Text("Your hosted inbox API")
                } footer: {
                    Text("Paste the base URL of your deployed API (https only in public). You can omit “https://”. Mac or 192.168… URLs only work on the same Wi‑Fi; for dinner/errands, use a cloud host (see README).")
                }

                Section {
                    Button {
                        Task { await fetchAndSpeak() }
                    } label: {
                        HStack {
                            if loading { ProgressView() }
                            Text("Summarise inbox")
                                .fontWeight(.semibold)
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .disabled(loading || apiBaseURL.trimmingCharacters(in: .whitespaces).isEmpty)

                    if !spokenText.isEmpty {
                        Button("Read aloud again") {
                            speaker.speak(spokenText)
                        }
                    }
                }

                if let e = errorMessage {
                    Section {
                        Text(e)
                            .foregroundStyle(.red)
                            .font(.footnote)
                    }
                }

                if !spokenText.isEmpty {
                    Section("Brief") {
                        Text(spokenText)
                            .font(.body)
                    }
                }
            }
            .navigationTitle("Inbox Brief")
        }
    }

    private func fetchAndSpeak() async {
        errorMessage = nil
        loading = true
        do {
            let text = try await BriefFetcher.fetchSpokenBrief()
            await MainActor.run {
                spokenText = text
                loading = false
                speaker.speak(text)
            }
        } catch {
            await MainActor.run {
                errorMessage = error.localizedDescription
                loading = false
            }
        }
    }
}

private final class Speaker: NSObject, ObservableObject {
    private let synth = AVSpeechSynthesizer()

    override init() {
        super.init()
    }

    func speak(_ text: String) {
        synth.stopSpeaking(at: .immediate)
        let u = AVSpeechUtterance(string: text)
        u.voice = AVSpeechSynthesisVoice(language: Locale.current.identifier)
        u.rate = AVSpeechUtteranceDefaultSpeechRate
        synth.speak(u)
    }
}

#Preview {
    ContentView()
}
