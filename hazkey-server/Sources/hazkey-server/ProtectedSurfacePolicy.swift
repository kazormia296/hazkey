import Foundation

/// Conservative output guard for surfaces whose spelling is more important
/// than an unconstrained language-model preference. Dictionary candidates are
/// trusted because the user or project explicitly supplied their surface;
/// generic and Zenzai candidates must preserve protected input fragments.
enum ProtectedSurfacePolicy {
    private static let asciiSymbolScalars: Set<UInt32> = Set(
        (Array(0x21...0x2F)
            + Array(0x3A...0x40)
            + Array(0x5B...0x60)
            + Array(0x7B...0x7E))
            .map(UInt32.init)
    )

    static func allows(
        _ candidate: ConverterCandidate,
        for rawInput: String
    ) -> Bool {
        switch candidate.provenance {
        case .projectDictionary, .personalDictionary, .temporaryDictionary,
             .builtInGuard:
            return true
        case .standard, .zenzai, .unknown:
            break
        }

        let protectedTokens = asciiTokens(in: rawInput)
        guard protectedTokens.allSatisfy({ candidate.text.contains($0) }) else {
            return false
        }
        return protectedSymbolStyleIsPreserved(
            input: rawInput,
            output: candidate.text
        )
    }

    static func asciiTokens(in text: String) -> [String] {
        var result: [String] = []
        var current = ""
        var hasAlphaNumeric = false

        func flush() {
            guard hasAlphaNumeric, !current.isEmpty else {
                current = ""
                hasAlphaNumeric = false
                return
            }
            result.append(current)
            current = ""
            hasAlphaNumeric = false
        }

        for scalar in text.unicodeScalars {
            if isASCIIAlphaNumeric(scalar.value) {
                current.unicodeScalars.append(scalar)
                hasAlphaNumeric = true
            } else if isTokenJoiner(scalar.value), hasAlphaNumeric {
                current.unicodeScalars.append(scalar)
            } else {
                flush()
            }
        }
        flush()
        return result
    }

    static func protectedSymbolStyleIsPreserved(
        input: String,
        output: String
    ) -> Bool {
        let inputSymbols = input.unicodeScalars.filter {
            asciiSymbolScalars.contains($0.value)
        }
        guard !inputSymbols.isEmpty else { return true }
        let outputSymbols = output.unicodeScalars.filter {
            asciiSymbolScalars.contains($0.value)
        }
        guard outputSymbols.count >= inputSymbols.count else { return false }

        // Preserve both the symbol identity and its ASCII/full-width style.
        // This intentionally compares the input sequence as a subsequence so
        // a candidate may add a safe delimiter without changing user syntax.
        var outputIndex = outputSymbols.startIndex
        for inputSymbol in inputSymbols {
            guard let match = outputSymbols[outputIndex...].firstIndex(
                where: { $0.value == inputSymbol.value }
            ) else {
                return false
            }
            outputIndex = outputSymbols.index(after: match)
        }
        return true
    }

    private static func isASCIIAlphaNumeric(_ value: UInt32) -> Bool {
        (0x30...0x39).contains(value)
            || (0x41...0x5A).contains(value)
            || (0x61...0x7A).contains(value)
    }

    private static func isTokenJoiner(_ value: UInt32) -> Bool {
        switch value {
        case 0x2D, 0x2E, 0x2F, 0x3A, 0x40, 0x5F, 0x25, 0x2B, 0x3D, 0x26,
             0x3F, 0x7E:
            return true
        default:
            return false
        }
    }
}
