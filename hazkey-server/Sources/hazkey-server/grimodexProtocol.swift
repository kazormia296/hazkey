import Foundation
import KanaKanjiConverterModule
import SwiftUtils

enum GrimodexProtocolLimits {
    static let stateBytes = 65_536
    static let projectBytes = 16_777_216
    static let projectEntries = 20_000
    static let projectIDScalars = 128
    static let projectNameScalars = 256
    static let entryYomiScalars = 256
    static let entrySurfaceScalars = 256
    static let entryIDScalars = 128
    static let profileScalars = 400
    static let zenzaiConditionScalars = 200
    static let converterConditionScalars = 25
    static let timestampBytes = 64
}

enum GrimodexPathResolver {
    static func resolve(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> URL {
        if let override = environment["GRIMODEX_IME_ROOT"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        if let xdgDataHome = environment["XDG_DATA_HOME"], !xdgDataHome.isEmpty {
            return URL(fileURLWithPath: xdgDataHome, isDirectory: true)
                .appendingPathComponent("com.miyakey.grimodex/ime", isDirectory: true)
        }
        return homeDirectory
            .appendingPathComponent(".local/share/com.miyakey.grimodex/ime", isDirectory: true)
    }
}

protocol GrimodexFileReading: Sendable {
    func read(_ url: URL, maxBytes: Int) throws -> Data?
}

enum GrimodexFileReadError: Error, Equatable {
    case oversized(limit: Int)
}

struct GrimodexBoundedFileReader: GrimodexFileReading, Sendable {
    func read(_ url: URL, maxBytes: Int) throws -> Data? {
        guard let handle = FileHandle(forReadingAtPath: url.path) else {
            return nil
        }
        defer { try? handle.close() }
        let data = try handle.read(upToCount: maxBytes + 1) ?? Data()
        guard data.count <= maxBytes else {
            throw GrimodexFileReadError.oversized(limit: maxBytes)
        }
        return data
    }
}

struct GrimodexMappedDictionaryEntry: Equatable, Hashable, Sendable {
    let ruby: String
    let word: String
    let cid: Int
    let mid: Int
    let value: Float
    let entryID: String

    var dictionaryElement: DicdataElement {
        DicdataElement(
            word: word,
            ruby: ruby,
            cid: cid,
            mid: mid,
            value: PValue(value)
        )
    }
}

struct GrimodexProjectConditions: Equatable, Sendable {
    let topic: String?
    let style: String?
    let preference: String?

    static let empty = GrimodexProjectConditions(topic: nil, style: nil, preference: nil)
}

struct GrimodexIntegrationPayload: Equatable, Sendable {
    let projectID: String
    let projectName: String
    let dictionaryEntries: [GrimodexMappedDictionaryEntry]
    let conditions: GrimodexProjectConditions
}

enum GrimodexLoadDiagnostic: String, Equatable, Sendable {
    case loaded
    case inactive
    case invalidState
    case invalidSnapshot
    case stateChangedDuringRead
}

struct GrimodexLoadResult: Equatable, Sendable {
    let payload: GrimodexIntegrationPayload?
    let diagnostic: GrimodexLoadDiagnostic
}

struct GrimodexPublishedSnapshot: Equatable, Sendable {
    let generation: UInt64
    let payload: GrimodexIntegrationPayload?
    let diagnostic: GrimodexLoadDiagnostic
}

private struct GrimodexWireState: Decodable {
    let formatVersion: Int
    let activeProjectID: String?
    let updatedAt: String

    enum CodingKeys: String, CodingKey {
        case formatVersion = "format_version"
        case activeProjectID = "active_project_id"
        case updatedAt = "updated_at"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        formatVersion = try container.decode(Int.self, forKey: .formatVersion)
        guard container.contains(.activeProjectID) else {
            throw DecodingError.keyNotFound(
                CodingKeys.activeProjectID,
                .init(codingPath: decoder.codingPath, debugDescription: "active_project_id is required")
            )
        }
        activeProjectID = try container.decodeIfPresent(String.self, forKey: .activeProjectID)
        updatedAt = try container.decode(String.self, forKey: .updatedAt)
    }
}

private enum GrimodexWireCategory: String, Decodable, Sendable {
    case person
    case place
    case noun
}

private struct GrimodexWireEntry: Decodable, Sendable {
    let yomi: String
    let surface: String
    let category: GrimodexWireCategory
    let priority: Int
    let entryID: String

    enum CodingKeys: String, CodingKey {
        case yomi
        case surface
        case category
        case priority
        case entryID = "entry_id"
    }
}

private struct GrimodexWireZenzaiContext: Decodable, Sendable {
    let topic: String
    let style: String?
    let preference: String?

    enum CodingKeys: String, CodingKey {
        case topic
        case style
        case preference
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        topic = try container.decode(String.self, forKey: .topic)
        guard container.contains(.style) else {
            throw DecodingError.keyNotFound(
                CodingKeys.style,
                .init(codingPath: decoder.codingPath, debugDescription: "style is required")
            )
        }
        guard container.contains(.preference) else {
            throw DecodingError.keyNotFound(
                CodingKeys.preference,
                .init(codingPath: decoder.codingPath, debugDescription: "preference is required")
            )
        }
        style = try container.decodeIfPresent(String.self, forKey: .style)
        preference = try container.decodeIfPresent(String.self, forKey: .preference)
    }
}

private struct GrimodexWireProject: Decodable, Sendable {
    let formatVersion: Int
    let projectID: String
    let projectName: String
    let generatedAt: String
    let entries: [GrimodexWireEntry]
    let profile: String?
    let zenzaiContext: GrimodexWireZenzaiContext?

    enum CodingKeys: String, CodingKey {
        case formatVersion = "format_version"
        case projectID = "project_id"
        case projectName = "project_name"
        case generatedAt = "generated_at"
        case entries
        case profile
        case zenzaiContext = "zenzai_context"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        formatVersion = try container.decode(Int.self, forKey: .formatVersion)
        projectID = try container.decode(String.self, forKey: .projectID)
        projectName = try container.decode(String.self, forKey: .projectName)
        generatedAt = try container.decode(String.self, forKey: .generatedAt)
        entries = try container.decode([GrimodexWireEntry].self, forKey: .entries)
        if container.contains(.profile) {
            profile = try container.decode(String.self, forKey: .profile)
        } else {
            profile = nil
        }
        if container.contains(.zenzaiContext) {
            zenzaiContext = try container.decode(
                GrimodexWireZenzaiContext.self,
                forKey: .zenzaiContext
            )
        } else {
            zenzaiContext = nil
        }
    }
}

private enum GrimodexProtocolValidator {
    static func validate(_ state: GrimodexWireState) -> Bool {
        guard state.formatVersion == 1, validTimestamp(state.updatedAt) else {
            return false
        }
        return state.activeProjectID.map(validProjectID) ?? true
    }

    static func validate(_ project: GrimodexWireProject, expectedProjectID: String) -> Bool {
        guard
            project.formatVersion == 1,
            project.projectID == expectedProjectID,
            validProjectID(project.projectID),
            validText(project.projectName, minimum: 1, maximum: GrimodexProtocolLimits.projectNameScalars),
            validTimestamp(project.generatedAt),
            project.entries.count <= GrimodexProtocolLimits.projectEntries
        else {
            return false
        }
        if let profile = project.profile,
            !validText(profile, minimum: 0, maximum: GrimodexProtocolLimits.profileScalars)
        {
            return false
        }
        if let context = project.zenzaiContext {
            guard validText(
                context.topic,
                minimum: 1,
                maximum: GrimodexProtocolLimits.zenzaiConditionScalars
            ) else {
                return false
            }
            for value in [context.style, context.preference].compactMap({ $0 }) {
                guard validText(
                    value,
                    minimum: 0,
                    maximum: GrimodexProtocolLimits.zenzaiConditionScalars
                ) else {
                    return false
                }
            }
        }
        return project.entries.allSatisfy { entry in
            validText(
                entry.yomi,
                minimum: 1,
                maximum: GrimodexProtocolLimits.entryYomiScalars
            )
                && validText(
                    entry.surface,
                    minimum: 1,
                    maximum: GrimodexProtocolLimits.entrySurfaceScalars
                )
                && (1...3).contains(entry.priority)
                && validEntryID(entry.entryID)
        }
    }

    static func validProjectID(_ value: String) -> Bool {
        validASCIIIdentifier(value, maximum: GrimodexProtocolLimits.projectIDScalars, allowDot: false)
    }

    static func validEntryID(_ value: String) -> Bool {
        validASCIIIdentifier(value, maximum: GrimodexProtocolLimits.entryIDScalars, allowDot: false)
    }

    private static func validASCIIIdentifier(
        _ value: String,
        maximum: Int,
        allowDot: Bool
    ) -> Bool {
        let bytes = Array(value.utf8)
        guard !bytes.isEmpty, bytes.count <= maximum, value.unicodeScalars.count == bytes.count else {
            return false
        }
        return bytes.allSatisfy { byte in
            byte.isASCIIAlphabetic || byte.isASCIIDigit || byte == 0x2D || byte == 0x5F
                || (allowDot && byte == 0x2E)
        }
    }

    private static func validText(_ value: String, minimum: Int, maximum: Int) -> Bool {
        let scalars = value.unicodeScalars
        guard scalars.count >= minimum, scalars.count <= maximum else {
            return false
        }
        return scalars.allSatisfy { scalar in
            let codepoint = scalar.value
            return !(codepoint <= 0x1F || (0x7F...0x9F).contains(codepoint))
        }
    }

    private static func validTimestamp(_ value: String) -> Bool {
        let bytes = Array(value.utf8)
        guard
            value.unicodeScalars.count == bytes.count,
            bytes.count >= 20,
            bytes.count <= GrimodexProtocolLimits.timestampBytes,
            bytes[4] == 0x2D,
            bytes[7] == 0x2D,
            bytes[10] == 0x54,
            bytes[13] == 0x3A,
            bytes[16] == 0x3A,
            digits(bytes, in: 0..<4),
            digits(bytes, in: 5..<7),
            digits(bytes, in: 8..<10),
            digits(bytes, in: 11..<13),
            digits(bytes, in: 14..<16),
            digits(bytes, in: 17..<19)
        else {
            return false
        }

        var zoneIndex = 19
        if bytes[zoneIndex] == 0x2E {
            zoneIndex += 1
            let fractionStart = zoneIndex
            while zoneIndex < bytes.count, bytes[zoneIndex].isASCIIDigit {
                zoneIndex += 1
            }
            guard (1...9).contains(zoneIndex - fractionStart) else {
                return false
            }
        }

        if zoneIndex < bytes.count, bytes[zoneIndex] == 0x5A {
            guard zoneIndex + 1 == bytes.count else { return false }
        } else {
            guard
                zoneIndex + 6 == bytes.count,
                bytes[zoneIndex] == 0x2B || bytes[zoneIndex] == 0x2D,
                bytes[zoneIndex + 3] == 0x3A,
                digits(bytes, in: (zoneIndex + 1)..<(zoneIndex + 3)),
                digits(bytes, in: (zoneIndex + 4)..<(zoneIndex + 6)),
                number(bytes, in: (zoneIndex + 1)..<(zoneIndex + 3))! <= 23,
                number(bytes, in: (zoneIndex + 4)..<(zoneIndex + 6))! <= 59
            else {
                return false
            }
        }

        guard
            let year = number(bytes, in: 0..<4),
            let month = number(bytes, in: 5..<7),
            let day = number(bytes, in: 8..<10),
            let hour = number(bytes, in: 11..<13),
            let minute = number(bytes, in: 14..<16),
            let second = number(bytes, in: 17..<19),
            (1...12).contains(month),
            hour <= 23,
            minute <= 59,
            second <= 59
        else {
            return false
        }
        return (1...daysInMonth(year: year, month: month)).contains(day)
    }

    private static func digits(_ bytes: [UInt8], in range: Range<Int>) -> Bool {
        range.allSatisfy { bytes[$0].isASCIIDigit }
    }

    private static func number(_ bytes: [UInt8], in range: Range<Int>) -> Int? {
        guard digits(bytes, in: range) else { return nil }
        return range.reduce(0) { result, index in result * 10 + Int(bytes[index] - 0x30) }
    }

    private static func daysInMonth(year: Int, month: Int) -> Int {
        switch month {
        case 2:
            let leap = year.isMultiple(of: 4)
                && (!year.isMultiple(of: 100) || year.isMultiple(of: 400))
            return leap ? 29 : 28
        case 4, 6, 9, 11:
            return 30
        default:
            return 31
        }
    }
}

private extension UInt8 {
    var isASCIIDigit: Bool { (0x30...0x39).contains(self) }
    var isASCIIAlphabetic: Bool {
        (0x41...0x5A).contains(self) || (0x61...0x7A).contains(self)
    }
}

private struct GrimodexDictionaryMapper {
    private struct Key: Hashable {
        let ruby: String
        let word: String
        let cid: Int
    }

    private struct Candidate {
        let mapped: GrimodexMappedDictionaryEntry
        let priority: Int
    }

    static func map(_ entries: [GrimodexWireEntry]) -> [GrimodexMappedDictionaryEntry] {
        var unique: [Key: Candidate] = [:]
        for entry in entries {
            let ruby = entry.yomi.precomposedStringWithCompatibilityMapping.toKatakana()
            let word = entry.surface.precomposedStringWithCanonicalMapping
            let cid = cid(for: entry.category)
            let mapped = GrimodexMappedDictionaryEntry(
                ruby: ruby,
                word: word,
                cid: cid,
                mid: 501,
                value: score(for: entry.category, priority: entry.priority),
                entryID: entry.entryID
            )
            let key = Key(ruby: ruby, word: word, cid: cid)
            if let current = unique[key],
                current.priority > entry.priority
                    || (current.priority == entry.priority
                        && !utf8Less(entry.entryID, current.mapped.entryID))
            {
                continue
            }
            unique[key] = Candidate(mapped: mapped, priority: entry.priority)
        }
        return unique.values.sorted { left, right in
            if left.priority != right.priority { return left.priority > right.priority }
            if left.mapped.ruby != right.mapped.ruby {
                return utf8Less(left.mapped.ruby, right.mapped.ruby)
            }
            if left.mapped.word != right.mapped.word {
                return utf8Less(left.mapped.word, right.mapped.word)
            }
            if left.mapped.cid != right.mapped.cid { return left.mapped.cid < right.mapped.cid }
            return utf8Less(left.mapped.entryID, right.mapped.entryID)
        }.map(\.mapped)
    }

    private static func cid(for category: GrimodexWireCategory) -> Int {
        switch category {
        case .person: 1289
        case .place: 1293
        case .noun: 1288
        }
    }

    private static func score(for category: GrimodexWireCategory, priority: Int) -> Float {
        let base: Float = switch priority {
        case 3: -4
        case 2: -5
        default: -8
        }
        return base + (category == .person ? 0 : -1)
    }

    private static func utf8Less(_ left: String, _ right: String) -> Bool {
        left.utf8.lexicographicallyPrecedes(right.utf8)
    }
}

struct GrimodexSnapshotLoader: Sendable {
    let rootURL: URL
    private let fileReader: any GrimodexFileReading

    init(
        rootURL: URL,
        fileReader: any GrimodexFileReading = GrimodexBoundedFileReader()
    ) {
        self.rootURL = rootURL
        self.fileReader = fileReader
    }

    func load() -> GrimodexLoadResult {
        let stateURL = rootURL.appendingPathComponent("state.json")
        let firstState: GrimodexWireState
        do {
            guard let data = try fileReader.read(
                stateURL,
                maxBytes: GrimodexProtocolLimits.stateBytes
            ) else {
                return GrimodexLoadResult(payload: nil, diagnostic: .inactive)
            }
            firstState = try JSONDecoder().decode(GrimodexWireState.self, from: data)
            guard GrimodexProtocolValidator.validate(firstState) else {
                return GrimodexLoadResult(payload: nil, diagnostic: .invalidState)
            }
        } catch {
            return GrimodexLoadResult(payload: nil, diagnostic: .invalidState)
        }

        guard let projectID = firstState.activeProjectID else {
            return GrimodexLoadResult(payload: nil, diagnostic: .inactive)
        }
        let projectURL = rootURL
            .appendingPathComponent("projects", isDirectory: true)
            .appendingPathComponent("\(projectID).json")
        let project: GrimodexWireProject
        do {
            guard let data = try fileReader.read(
                projectURL,
                maxBytes: GrimodexProtocolLimits.projectBytes
            ) else {
                return GrimodexLoadResult(payload: nil, diagnostic: .invalidSnapshot)
            }
            project = try JSONDecoder().decode(GrimodexWireProject.self, from: data)
            guard GrimodexProtocolValidator.validate(project, expectedProjectID: projectID) else {
                return GrimodexLoadResult(payload: nil, diagnostic: .invalidSnapshot)
            }
        } catch {
            return GrimodexLoadResult(payload: nil, diagnostic: .invalidSnapshot)
        }

        do {
            guard let data = try fileReader.read(
                stateURL,
                maxBytes: GrimodexProtocolLimits.stateBytes
            ) else {
                return GrimodexLoadResult(payload: nil, diagnostic: .stateChangedDuringRead)
            }
            let secondState = try JSONDecoder().decode(GrimodexWireState.self, from: data)
            guard
                GrimodexProtocolValidator.validate(secondState),
                secondState.activeProjectID == firstState.activeProjectID
            else {
                return GrimodexLoadResult(payload: nil, diagnostic: .stateChangedDuringRead)
            }
        } catch {
            return GrimodexLoadResult(payload: nil, diagnostic: .stateChangedDuringRead)
        }

        let conditions = project.zenzaiContext.map { context in
            GrimodexProjectConditions(
                topic: converterCondition(context.topic),
                style: context.style.map(converterCondition),
                preference: context.preference.map(converterCondition)
            )
        } ?? .empty
        return GrimodexLoadResult(
            payload: GrimodexIntegrationPayload(
                projectID: project.projectID,
                projectName: project.projectName,
                dictionaryEntries: GrimodexDictionaryMapper.map(project.entries),
                conditions: conditions
            ),
            diagnostic: .loaded
        )
    }

    private func converterCondition(_ value: String) -> String {
        String(value.unicodeScalars.prefix(GrimodexProtocolLimits.converterConditionScalars))
    }
}

final class GrimodexSnapshotManager: @unchecked Sendable {
    private let loader: GrimodexSnapshotLoader
    private let lock = NSLock()
    private var published = GrimodexPublishedSnapshot(
        generation: 0,
        payload: nil,
        diagnostic: .inactive
    )

    init(loader: GrimodexSnapshotLoader) {
        self.loader = loader
    }

    func reload() -> GrimodexPublishedSnapshot {
        let result = loader.load()
        lock.lock()
        defer { lock.unlock() }
        let generation = published.payload == result.payload
            ? published.generation
            : published.generation &+ 1
        published = GrimodexPublishedSnapshot(
            generation: generation,
            payload: result.payload,
            diagnostic: result.diagnostic
        )
        return published
    }

    func latest() -> GrimodexPublishedSnapshot {
        lock.lock()
        defer { lock.unlock() }
        return published
    }
}
