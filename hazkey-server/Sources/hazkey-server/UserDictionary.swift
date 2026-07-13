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

/// CRUD/import/export is kept separate from converter learning.  This makes
/// project dictionary and personal dictionary policy explicit and prevents a
/// secure-input composition from silently changing dictionary ownership.
final class UserDictionaryStore {
    private let lock = NSLock()
    private var storedEntries: [UserDictionaryEntry]
    private let persistenceURL: URL?

    var entries: [UserDictionaryEntry] {
        lock.lock()
        defer { lock.unlock() }
        return storedEntries
    }

    init(
        entries: [UserDictionaryEntry] = [],
        persistenceURL: URL? = nil
    ) {
        self.persistenceURL = persistenceURL
        if let persistenceURL,
           let data = try? Data(contentsOf: persistenceURL),
           let decoded = try? JSONDecoder().decode(
               [UserDictionaryEntry].self,
               from: data
           ) {
            self.storedEntries = decoded
        } else {
            self.storedEntries = entries
        }
    }

    @discardableResult
    func add(_ entry: UserDictionaryEntry) throws -> UserDictionaryEntry {
        try validate(entry)
        lock.lock()
        defer { lock.unlock() }
        guard !storedEntries.contains(where: {
            $0.id == entry.id
                || ($0.reading == entry.reading
                    && $0.surface == entry.surface
                    && $0.layer == entry.layer)
        }) else { throw UserDictionaryError.duplicate }
        var next = storedEntries
        next.append(entry)
        try persist(next)
        storedEntries = next
        return entry
    }

    func update(_ entry: UserDictionaryEntry) throws {
        try validate(entry)
        lock.lock()
        defer { lock.unlock() }
        guard let index = storedEntries.firstIndex(where: { $0.id == entry.id }) else {
            throw UserDictionaryError.missingEntry
        }
        if storedEntries.enumerated().contains(where: {
            index != $0.offset
                && $0.element.reading == entry.reading
                && $0.element.surface == entry.surface
                && $0.element.layer == entry.layer
        }) {
            throw UserDictionaryError.duplicate
        }
        var next = storedEntries
        next[index] = entry
        try persist(next)
        storedEntries = next
    }

    func remove(id: String) throws {
        lock.lock()
        defer { lock.unlock() }
        guard let index = storedEntries.firstIndex(where: { $0.id == id }) else {
            throw UserDictionaryError.missingEntry
        }
        var next = storedEntries
        next.remove(at: index)
        try persist(next)
        storedEntries = next
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
            try validate(entry)
            let duplicate = nextEntries.contains(where: {
                $0.id == entry.id
                    || ($0.reading == entry.reading
                        && $0.surface == entry.surface
                        && $0.layer == entry.layer)
            })
            if duplicate {
                if merge { continue }
                throw UserDictionaryError.duplicate
            }
            nextEntries.append(entry)
        }
        do {
            try persist(nextEntries)
        } catch {
            throw UserDictionaryError.persistenceFailed
        }
        storedEntries = nextEntries
    }

    private func validate(_ entry: UserDictionaryEntry) throws {
        guard !entry.reading.isEmpty, !entry.surface.isEmpty, !entry.partOfSpeech.isEmpty else {
            throw UserDictionaryError.emptyField
        }
        guard !entry.id.isEmpty,
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
