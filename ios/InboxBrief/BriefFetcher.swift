import Foundation

enum BriefFetcher {
    /// Keys must match `@AppStorage` in `ContentView`.
    /// Digest runs OpenAI + Graph on the server; default `URLSession` timeouts are often too short.
    private static let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 180
        config.timeoutIntervalForResource = 180
        config.waitsForConnectivity = true
        return URLSession(configuration: config)
    }()

    /// Trim whitespace/slashes; add `https://` if the user omitted the scheme (e.g. `myapp.up.railway.app`).
    static func normalizeBaseURL(_ raw: String) -> String {
        let t = raw.trimmingCharacters(in: .whitespaces.union(CharacterSet(charactersIn: "/")))
        guard !t.isEmpty else { return t }
        if t.contains("://") { return t }
        return "https://\(t)"
    }

    static func friendlyURLErrorMessage(code: Int) -> String {
        switch code {
        case NSURLErrorNotConnectedToInternet:
            return "No internet connection."
        case NSURLErrorCannotFindHost, NSURLErrorDNSLookupFailed:
            return "Could not find that host. Check spelling, or use the https URL from your host (Railway, Fly, etc.)—not a name that only works on your home Wi‑Fi."
        case NSURLErrorCannotConnectToHost:
            return "Could not connect. A Mac or PC address (192.168… or localhost) only works on the same Wi‑Fi with the server running. For anywhere else, deploy the API and paste its public https URL."
        case NSURLErrorTimedOut:
            return "Request timed out. The brief can take a minute; try again. If it keeps failing, check that your API is running and reachable."
        case NSURLErrorSecureConnectionFailed, NSURLErrorServerCertificateUntrusted:
            return "Secure connection failed. Use a valid https URL (or http only on your local network for testing)."
        default:
            return "Could not reach your inbox API. Use your deployed server’s https URL (see project README), not your Mac’s IP, unless you’re on the same Wi‑Fi."
        }
    }

    static func fetchSpokenBrief() async throws -> String {
        let raw = UserDefaults.standard.string(forKey: "apiBaseURL") ?? ""
        let base = normalizeBaseURL(raw)
        guard !base.isEmpty else {
            throw NSError(
                domain: "InboxBrief",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Set Server URL to your hosted API (e.g. https://….railway.app) — the same address where /digest works in a browser."]
            )
        }
        guard var comp = URLComponents(string: base + "/digest/spoken") else {
            throw NSError(
                domain: "InboxBrief",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "That doesn’t look like a valid URL. Include the host, e.g. https://your-service.up.railway.app"]
            )
        }
        let key = (UserDefaults.standard.string(forKey: "accessKey") ?? "")
            .trimmingCharacters(in: .whitespaces)
        if !key.isEmpty {
            comp.queryItems = [URLQueryItem(name: "access_key", value: key)]
        }
        guard let url = comp.url else {
            throw NSError(
                domain: "InboxBrief",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "Invalid URL after adding /digest/spoken."]
            )
        }
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(from: url)
        } catch {
            let ns = error as NSError
            if ns.domain == NSURLErrorDomain {
                let msg = friendlyURLErrorMessage(code: ns.code)
                throw NSError(domain: "InboxBrief", code: ns.code, userInfo: [NSLocalizedDescriptionKey: msg])
            }
            throw error
        }
        let text = String(data: data, encoding: .utf8) ?? ""
        guard let http = resp as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        guard http.statusCode == 200 else {
            throw NSError(
                domain: "InboxBrief",
                code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey: text.isEmpty ? "Server error \(http.statusCode)" : text]
            )
        }
        return text
    }
}
