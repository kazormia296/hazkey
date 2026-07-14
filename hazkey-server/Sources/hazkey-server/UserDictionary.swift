import Foundation
import KanaKanjiConverterModule
import SwiftUtils

enum UserDictionaryLayer: String, Codable, CaseIterable, Sendable {
    case system
    case personal
    case project
    case temporary
}

struct UserDictionaryEntry: Equatable, Codable, Hashable, Sendable {
    let id: String
    var reading: String
    var surface: String
    var partOfSpeech: String
    var layer: UserDictionaryLayer

    init(
        id: String = UUID().uuidString,
        reading: String,
        surface: String,
        partOfSpeech: String,
        layer: UserDictionaryLayer = .personal
    ) {
        self.id = id
        self.reading = reading
        self.surface = surface
        self.partOfSpeech = partOfSpeech
        self.layer = layer
    }
}

enum UserDictionaryError: Error, Equatable {
    case emptyField
    case invalidField
    case duplicate
    case missingEntry
    case invalidImport
    case persistenceFailed
}

/// Immutable exact-reading lookup used by converter adapters. The snapshot
/// preserves the store's insertion order while normalizing both indexed and
/// queried readings to compatibility-composed Katakana.
struct UserDictionaryCandidateIndex: Equatable, Sendable {
    static let empty = UserDictionaryCandidateIndex(entries: [])

    private let entriesByRuby: [String: [UserDictionaryEntry]]
    let entryCount: Int

    init(entries: [UserDictionaryEntry]) {
        var entriesByRuby: [String: [UserDictionaryEntry]] = [:]
        var entryCount = 0
        for entry in entries {
            // Project entries are supplied by the scoped Grimodex snapshot and
            // system entries belong to the converter core. Neither may enter
            // the global user-dictionary overlay, including through an older
            // or manually modified persistence file.
            guard entry.layer == .personal || entry.layer == .temporary else {
                continue
            }
            let ruby = Self.normalizedRuby(entry.reading)
            guard !ruby.isEmpty else { continue }
            entriesByRuby[ruby, default: []].append(entry)
            entryCount += 1
        }
        self.entriesByRuby = entriesByRuby
        self.entryCount = entryCount
    }

    func entries(forRuby ruby: String) -> [UserDictionaryEntry] {
        entriesByRuby[Self.normalizedRuby(ruby)] ?? []
    }

    private static func normalizedRuby(_ ruby: String) -> String {
        ruby.precomposedStringWithCompatibilityMapping.toKatakana()
    }
}

/// CRUD/import/export is kept separate from converter learning.  This makes
/// project dictionary and personal dictionary policy explicit and prevents a
/// secure-input composition from silently changing dictionary ownership.
final class UserDictionaryStore {
    private let lock = NSLock()
    private var storedEntries: [UserDictionaryEntry]
    private var storedCandidateIndexSnapshot: UserDictionaryCandidateIndex
    private let persistenceURL: URL?

    var entries: [UserDictionaryEntry] {
        lock.lock()
        defer { lock.unlock() }
        return storedEntries
    }

    var candidateIndexSnapshot: UserDictionaryCandidateIndex {
        lock.lock()
        defer { lock.unlock() }
        return storedCandidateIndexSnapshot
    }

    init(
        entries: [UserDictionaryEntry] = [],
        persistenceURL: URL? = nil
    ) {
        let loadedEntries: [UserDictionaryEntry]
        if let persistenceURL,
           let data = try? Data(contentsOf: persistenceURL),
           let decoded = try? JSONDecoder().decode(
               [UserDictionaryEntry].self,
               from: data
           ) {
            loadedEntries = decoded
        } else {
            loadedEntries = entries
        }
        // Initialization cannot report individual invalid records. Filter with
        // the same rules as CRUD/import and deterministically retain the first
        // valid occurrence, so malformed persisted or injected state can never
        // publish a candidate.
        let initialEntries = Self.validatedFirstWins(loadedEntries)
        self.persistenceURL = persistenceURL
        self.storedEntries = initialEntries
        self.storedCandidateIndexSnapshot = UserDictionaryCandidateIndex(
            entries: initialEntries
        )
    }

    @discardableResult
    func add(_ entry: UserDictionaryEntry) throws -> UserDictionaryEntry {
        try Self.validate(entry)
        lock.lock()
        defer { lock.unlock() }
        guard !Self.isDuplicate(entry, in: storedEntries) else {
            throw UserDictionaryError.duplicate
        }
        var next = storedEntries
        next.append(entry)
        try replaceStoredEntries(next)
        return entry
    }

    func update(_ entry: UserDictionaryEntry) throws {
        try Self.validate(entry)
        lock.lock()
        defer { lock.unlock() }
        guard let index = storedEntries.firstIndex(where: { $0.id == entry.id }) else {
            throw UserDictionaryError.missingEntry
        }
        if Self.isDuplicate(entry, in: storedEntries, excluding: index) {
            throw UserDictionaryError.duplicate
        }
        var next = storedEntries
        next[index] = entry
        try replaceStoredEntries(next)
    }

    func remove(id: String) throws {
        lock.lock()
        defer { lock.unlock() }
        guard let index = storedEntries.firstIndex(where: { $0.id == id }) else {
            throw UserDictionaryError.missingEntry
        }
        var next = storedEntries
        next.remove(at: index)
        try replaceStoredEntries(next)
    }

    func exportJSON() throws -> Data {
        lock.lock()
        let snapshot = storedEntries
        lock.unlock()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return try encoder.encode(snapshot)
    }

    func importJSON(_ data: Data, merge: Bool = false) throws {
        let imported: [UserDictionaryEntry]
        do {
            imported = try JSONDecoder().decode([UserDictionaryEntry].self, from: data)
        } catch {
            throw UserDictionaryError.invalidImport
        }
        lock.lock()
        defer { lock.unlock() }
        var nextEntries = merge ? storedEntries : []
        for entry in imported {
            try Self.validate(entry)
            if Self.isDuplicate(entry, in: nextEntries) {
                if merge { continue }
                throw UserDictionaryError.duplicate
            }
            nextEntries.append(entry)
        }
        try replaceStoredEntries(nextEntries)
    }

    private static func validatedFirstWins(
        _ entries: [UserDictionaryEntry]
    ) -> [UserDictionaryEntry] {
        var result: [UserDictionaryEntry] = []
        result.reserveCapacity(entries.count)
        for entry in entries {
            guard (try? validate(entry)) != nil,
                  !isDuplicate(entry, in: result) else {
                continue
            }
            result.append(entry)
        }
        return result
    }

    private static func isDuplicate(
        _ entry: UserDictionaryEntry,
        in entries: [UserDictionaryEntry],
        excluding excludedIndex: Int? = nil
    ) -> Bool {
        entries.enumerated().contains { index, existing in
            guard index != excludedIndex else { return false }
            return existing.id == entry.id
                || (existing.reading == entry.reading
                    && existing.surface == entry.surface
                    && existing.layer == entry.layer)
        }
    }

    private static func validate(_ entry: UserDictionaryEntry) throws {
        guard !entry.reading.isEmpty, !entry.surface.isEmpty, !entry.partOfSpeech.isEmpty else {
            throw UserDictionaryError.emptyField
        }
        guard entry.layer == .personal || entry.layer == .temporary,
              !entry.id.isEmpty,
              entry.id.unicodeScalars.count <= 128,
              entry.reading.unicodeScalars.count <= 256,
              entry.surface.unicodeScalars.count <= 256,
              entry.partOfSpeech.unicodeScalars.count <= 64,
              ![entry.id, entry.reading, entry.surface, entry.partOfSpeech]
                .contains(where: { value in
                    value.unicodeScalars.contains { scalar in
                        scalar.value < 0x20 || (0x7F...0x9F).contains(scalar.value)
                    }
                }) else {
            throw UserDictionaryError.invalidField
        }
    }

    /// Caller must hold `lock`. Entries and their immutable lookup snapshot are
    /// published together only after persistence succeeds.
    private func replaceStoredEntries(_ entries: [UserDictionaryEntry]) throws {
        let candidateIndex = UserDictionaryCandidateIndex(entries: entries)
        try persist(entries)
        storedEntries = entries
        storedCandidateIndexSnapshot = candidateIndex
    }

    private func persist(_ entries: [UserDictionaryEntry]) throws {
        guard let persistenceURL else { return }
        do {
            try FileManager.default.createDirectory(
                at: persistenceURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try encoder.encode(entries).write(to: persistenceURL, options: .atomic)
        } catch {
            throw UserDictionaryError.persistenceFailed
        }
    }
}

extension UserDictionaryEntry {
    var dictionaryElement: DicdataElement {
        let normalizedPartOfSpeech = partOfSpeech
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        let cid: Int = switch normalizedPartOfSpeech {
        case "person", "person_name", "name": CIDData.人名一般.cid
        case "surname", "family_name": CIDData.人名姓.cid
        case "given_name": CIDData.人名名.cid
        case "place", "place_name": CIDData.地名一般.cid
        case "organization", "organization_name": CIDData.固有名詞組織.cid
        case "proper_noun", "noun", "common_noun": CIDData.固有名詞.cid
        default: CIDData.固有名詞.cid
        }
        let mid: Int = switch normalizedPartOfSpeech {
        case "surname", "family_name": MIDData.人名姓.mid
        case "given_name": MIDData.人名名.mid
        case "organization", "organization_name": MIDData.組織.mid
        default: MIDData.一般.mid
        }
        return DicdataElement(
            word: surface.precomposedStringWithCompatibilityMapping,
            ruby: reading.precomposedStringWithCompatibilityMapping.toKatakana(),
            cid: cid,
            mid: mid,
            // Dynamic user entries must stay above the converter's pruning
            // threshold even for long readings. This matches Grimodex's
            // highest-priority project dictionary score.
            value: -4
        )
    }
}
