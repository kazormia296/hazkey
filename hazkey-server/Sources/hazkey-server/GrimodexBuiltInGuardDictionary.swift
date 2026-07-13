import Foundation

struct GrimodexBuiltInGuardEntry: Equatable, Sendable {
    let reading: String
    let surface: String
    let annotation: String
}

/// A deliberately small, reviewable syntax guard. It is not a general
/// dictionary and is never learned; it only supplies high-confidence forms
/// that otherwise tend to drift in sentence-level ranking.
enum GrimodexBuiltInGuardDictionary {
    static let entries: [GrimodexBuiltInGuardEntry] = [
        GrimodexBuiltInGuardEntry(reading: "かんそくせい", surface: "可観測性", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "さいげんせい", surface: "再現性", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "じっこうせい", surface: "実効性", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "したほうが", surface: "した方が", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "おこなう", surface: "行う", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "および", surface: "及び", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "ならびに", surface: "並びに", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "あらかじめ", surface: "予め", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "とりあつかい", surface: "取り扱い", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "もとづく", surface: "基づく", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "かかわらず", surface: "関わらず", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "ひきつづき", surface: "引き続き", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "おおむね", surface: "概ね", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "おのおの", surface: "各々", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "すべて", surface: "全て", annotation: "構文ガード"),
        GrimodexBuiltInGuardEntry(reading: "みていく", surface: "見ていく", annotation: "構文ガード"),
    ]

    static var count: Int { entries.count }

    static func candidates(
        for reading: String,
        consumingCount: Int
    ) -> [ConverterCandidate] {
        let normalized = canonicalReading(reading)
        return entries.compactMap { entry in
            guard canonicalReading(entry.reading) == normalized else { return nil }
            return ConverterCandidate(
                text: entry.surface,
                annotation: entry.annotation,
                consumingCount: consumingCount,
                provenance: .builtInGuard
            )
        }
    }

    private static func canonicalReading(_ text: String) -> String {
        String(String.UnicodeScalarView(text.unicodeScalars.compactMap { scalar in
            if (0x3041...0x3096).contains(scalar.value) {
                return UnicodeScalar(scalar.value + 0x60)
            }
            return scalar
        }))
    }
}
