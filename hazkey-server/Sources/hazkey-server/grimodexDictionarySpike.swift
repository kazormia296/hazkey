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
        return (0..<count).map { index in
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
        let importStarted = DispatchTime.now().uptimeNanoseconds
        converter.importDynamicUserDictionary(entries)
        let importFinished = DispatchTime.now().uptimeNanoseconds

        var composingText = ComposingText()
        composingText.insertAtCursorPosition("ぐりもでっくすべんち0", inputStyle: .direct)
        var benchmarkOptions = options
        benchmarkOptions.N_best = 9
        for _ in 0..<10 {
            _ = converter.requestCandidates(composingText, options: benchmarkOptions)
        }

        var samples: [Double] = []
        samples.reserveCapacity(50)
        for _ in 0..<50 {
            let started = DispatchTime.now().uptimeNanoseconds
            _ = converter.requestCandidates(composingText, options: benchmarkOptions)
            let finished = DispatchTime.now().uptimeNanoseconds
            samples.append(milliseconds(from: started, to: finished))
        }
        samples.sort()
        let p95Index = min(samples.count - 1, (samples.count * 95) / 100)

        return GrimodexDictionaryBenchmarkReport(
            entryCount: count,
            importMilliseconds: milliseconds(from: importStarted, to: importFinished),
            warmP95Milliseconds: samples[p95Index],
            residentMemoryKilobytes: residentMemoryKilobytes()
        )
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
