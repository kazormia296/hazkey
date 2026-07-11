import Dispatch
import Foundation
import KanaKanjiConverterModule
import SwiftUtils

enum GrimodexDictionaryCategory: String, Equatable, Sendable {
    case person
    case place
    case noun
}

struct GrimodexDictionarySourceEntry: Sendable {
    let yomi: String
    let surface: String
    let category: GrimodexDictionaryCategory
    let priority: Int
    let entryID: String
}

struct GrimodexDictionaryBenchmarkReport: Sendable {
    let entryCount: Int
    let importMilliseconds: Double
    let warmP95Milliseconds: Double
    let residentMemoryKilobytes: Int?
    let residentMemoryDeltaKilobytes: Int?
    let candidateRank: Int?
}

enum GrimodexDictionarySpike {
    static let benchmarkCounts = [100, 500, 2_000, 5_000, 10_000]

    private static let fixedSourceEntries: [GrimodexDictionarySourceEntry] = [
        .init(
            yomi: "せつな",
            surface: "刹那",
            category: .person,
            priority: 2,
            entryID: "spike-setsuna"
        ),
        .init(
            yomi: "りゅうせいこう",
            surface: "龍星港",
            category: .place,
            priority: 1,
            entryID: "spike-ryuseiko"
        ),
    ]

    static var fixedEntries: [DicdataElement] {
        fixedSourceEntries.map(map)
    }

    static func isEnabled(environment: [String: String]) -> Bool {
        environment["GRIMODEX_IME_DICTIONARY_SPIKE"] == "1"
    }

    static func injectIfEnabled(
        into converter: KanaKanjiConverter,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> Int {
        guard isEnabled(environment: environment) else { return 0 }
        let entries = fixedEntries
        converter.importDynamicUserDictionary(entries)
        return entries.count
    }

    static func map(_ source: GrimodexDictionarySourceEntry) -> DicdataElement {
        DicdataElement(
            word: source.surface,
            ruby: source.yomi.toKatakana(),
            cid: cid(for: source.category),
            mid: MIDData.一般.mid,
            value: value(for: source.category, priority: source.priority)
        )
    }

    static func makeBenchmarkEntries(count: Int) -> [DicdataElement] {
        guard count > 0 else { return [] }
        let fixed = Array(fixedEntries.prefix(count))
        guard count > fixed.count else { return fixed }
        let generated = (fixed.count..<count).map { index in
            let category: GrimodexDictionaryCategory = switch index % 3 {
            case 0: .person
            case 1: .place
            default: .noun
            }
            return map(
                .init(
                    yomi: "ぐりもでっくすべんち\(index)",
                    surface: "Grimodex検証語\(index)",
                    category: category,
                    priority: index.isMultiple(of: 10) ? 2 : 1,
                    entryID: "benchmark-\(index)"
                )
            )
        }
        return fixed + generated
    }

    static func runBenchmarkIfConfigured(
        converter: KanaKanjiConverter,
        options: ConvertRequestOptions,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> GrimodexDictionaryBenchmarkReport? {
        guard
            let rawCount = environment["GRIMODEX_IME_DICTIONARY_BENCHMARK_COUNT"],
            let count = Int(rawCount),
            benchmarkCounts.contains(count)
        else {
            return nil
        }

        let entries = makeBenchmarkEntries(count: count)
        let residentMemoryBeforeImport = residentMemoryKilobytes()
        let importStarted = DispatchTime.now().uptimeNanoseconds
        converter.importDynamicUserDictionary(entries)
        let importFinished = DispatchTime.now().uptimeNanoseconds
        let residentMemoryAfterImport = residentMemoryKilobytes()

        var benchmarkOptions = options
        benchmarkOptions.N_best = 9
        benchmarkOptions.zenzaiMode = .off
        for _ in 0..<10 {
            converter.stopComposition()
            let composingText = qualityProbeText()
            _ = converter.requestCandidates(composingText, options: benchmarkOptions)
        }

        var samples: [Double] = []
        samples.reserveCapacity(50)
        var candidateRank: Int?
        for _ in 0..<50 {
            converter.stopComposition()
            let composingText = qualityProbeText()
            let started = DispatchTime.now().uptimeNanoseconds
            let result = converter.requestCandidates(composingText, options: benchmarkOptions)
            let finished = DispatchTime.now().uptimeNanoseconds
            samples.append(milliseconds(from: started, to: finished))
            candidateRank = result.mainResults.firstIndex { $0.text == "刹那" }.map { $0 + 1 }
        }
        samples.sort()
        let p95Index = min(samples.count - 1, (samples.count * 95) / 100)

        return GrimodexDictionaryBenchmarkReport(
            entryCount: count,
            importMilliseconds: milliseconds(from: importStarted, to: importFinished),
            warmP95Milliseconds: samples[p95Index],
            residentMemoryKilobytes: residentMemoryAfterImport,
            residentMemoryDeltaKilobytes: memoryDelta(
                before: residentMemoryBeforeImport,
                after: residentMemoryAfterImport
            ),
            candidateRank: candidateRank
        )
    }

    private static func qualityProbeText() -> ComposingText {
        var composingText = ComposingText()
        composingText.insertAtCursorPosition("せつな", inputStyle: .direct)
        return composingText
    }

    private static func memoryDelta(before: Int?, after: Int?) -> Int? {
        guard let before, let after else { return nil }
        return after - before
    }

    private static func cid(for category: GrimodexDictionaryCategory) -> Int {
        switch category {
        case .person: CIDData.人名一般.cid
        case .place: CIDData.地名一般.cid
        case .noun: CIDData.固有名詞.cid
        }
    }

    private static func value(
        for category: GrimodexDictionaryCategory,
        priority: Int
    ) -> PValue {
        let base: PValue = switch priority {
        case 3: -4
        case 2: -5
        default: -8
        }
        let categoryAdjustment: PValue = category == .person ? 0 : -1
        return base + categoryAdjustment
    }

    private static func milliseconds(from start: UInt64, to end: UInt64) -> Double {
        Double(end - start) / 1_000_000
    }

    private static func residentMemoryKilobytes() -> Int? {
        guard let status = try? String(contentsOfFile: "/proc/self/status", encoding: .utf8)
        else {
            return nil
        }
        guard let line = status.split(separator: "\n").first(where: { $0.hasPrefix("VmRSS:") })
        else {
            return nil
        }
        return line.split(whereSeparator: \.isWhitespace).dropFirst().first.flatMap {
            Int($0)
        }
    }
}
