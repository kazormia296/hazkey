import Foundation

/// Conservative output guard for surfaces whose spelling is more important
/// than an unconstrained language-model preference. Dictionary candidates are
/// trusted only when their provenance covers every node in the candidate;
/// generic and Zenzai candidates must preserve every contiguous protected span
/// from the input.
enum ProtectedSurfacePolicy {
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

        let inputLayout = protectedLayout(in: rawInput)
        guard !inputLayout.spans.isEmpty else { return true }

        // Compare maximal contiguous spans rather than a filtered scalar
        // stream. Filtering would accept an unprotected insertion at an
        // existing token/symbol boundary, for example
        // "https日本://example.com" for "https://example.com". Exact span
        // equality also preserves order, cardinality, token boundaries, and
        // symbol width while still allowing ordinary Japanese text outside a
        // protected span to be converted. Preserve the surrounding gap
        // topology as well: comparing only the spans would allow punctuation
        // to move across the beginning or end of the Japanese text (for
        // example, "かな。" -> "。仮名").
        return protectedLayout(in: candidate.text) == inputLayout
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
            isASCIISymbol($0.value)
        }
        guard !inputSymbols.isEmpty else { return true }
        let outputSymbols = output.unicodeScalars.filter {
            isASCIISymbol($0.value)
        }
        guard outputSymbols.count >= inputSymbols.count else { return false }

        // Preserve the helper's original subsequence contract. `allows` uses
        // the stricter contiguous-span comparison independently.
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

    private struct ProtectedLayout: Equatable {
        let spans: [String]
        /// One entry before the first span, between each pair of spans, and
        /// after the last span. The value records whether that gap contains
        /// unprotected text; its length is intentionally ignored because
        /// kana-to-kanji conversion may change the scalar count.
        let populatedUnprotectedGaps: [Bool]
    }

    private static func protectedLayout(in text: String) -> ProtectedLayout {
        var spans: [String] = []
        var populatedUnprotectedGaps = [false]
        var span = ""

        func flushSpan() {
            guard !span.isEmpty else { return }
            spans.append(span)
            span = ""
            populatedUnprotectedGaps.append(false)
        }

        for scalar in text.unicodeScalars {
            if isProtectedScalar(scalar.value) {
                span.unicodeScalars.append(scalar)
            } else {
                flushSpan()
                populatedUnprotectedGaps[populatedUnprotectedGaps.count - 1] = true
            }
        }
        flushSpan()
        return ProtectedLayout(
            spans: spans,
            populatedUnprotectedGaps: populatedUnprotectedGaps
        )
    }

    private static func isProtectedAlphaNumeric(_ value: UInt32) -> Bool {
        isASCIIAlphaNumeric(value)
            || (0xFF10...0xFF19).contains(value)
            || (0xFF21...0xFF3A).contains(value)
            || (0xFF41...0xFF5A).contains(value)
    }

    private static func isProtectedScalar(_ value: UInt32) -> Bool {
        isProtectedAlphaNumeric(value) || isProtectedSymbol(value)
    }

    private static func isASCIIAlphaNumeric(_ value: UInt32) -> Bool {
        (0x30...0x39).contains(value)
            || (0x41...0x5A).contains(value)
            || (0x61...0x7A).contains(value)
    }

    private static func isASCIISymbol(_ value: UInt32) -> Bool {
        (0x21...0x2F).contains(value)
            || (0x3A...0x40).contains(value)
            || (0x5B...0x60).contains(value)
            || (0x7B...0x7E).contains(value)
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

    private static func isProtectedSymbol(_ value: UInt32) -> Bool {
        switch value {
        // ASCII punctuation and symbols.
        case 0x21...0x2F, 0x3A...0x40, 0x5B...0x60, 0x7B...0x7E:
            return true
        // CJK punctuation, including ideographic punctuation and brackets.
        case 0x3001...0x303F:
            return true
        // Full-width ASCII punctuation plus half-width CJK punctuation.
        case 0xFF01...0xFF0F, 0xFF1A...0xFF20, 0xFF3B...0xFF40,
             0xFF5B...0xFF65:
            return true
        // Full-width cent, pound, yen, won, and related symbol forms.
        case 0xFFE0...0xFFE6:
            return true
        default:
            return false
        }
    }
}
