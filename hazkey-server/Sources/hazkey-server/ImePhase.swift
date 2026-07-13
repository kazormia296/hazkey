import Foundation

/// The domain phase is owned by the Swift composition session.  UI state such
/// as whether Fcitx currently has a candidate panel focus is deliberately not
/// part of this enum.
enum ImePhase: String, Codable, CaseIterable, Sendable {
    case idle
    case composing
    case previewing
    case selecting
    case reconverting
    case unicodeInput
}

enum ImeInputStyle: String, Codable, Sendable {
    case mapped
    case direct
}

struct CompositionElement: Equatable, Hashable, Codable, Sendable {
    let text: String
    let composingCount: Int
    let inputStyle: ImeInputStyle
    let mappedIntention: String?
    let mappedInputOverride: String?

    init(
        text: String,
        composingCount: Int = 1,
        inputStyle: ImeInputStyle = .mapped,
        mappedIntention: String? = nil,
        mappedInputOverride: String? = nil
    ) {
        self.text = text
        self.composingCount = max(1, composingCount)
        self.inputStyle = inputStyle
        self.mappedIntention = mappedIntention
        self.mappedInputOverride = mappedInputOverride
    }

    private enum CodingKeys: String, CodingKey {
        case text
        case composingCount
        case inputStyle
        case mappedIntention
        case mappedInputOverride
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        text = try container.decode(String.self, forKey: .text)
        composingCount = max(
            1,
            try container.decode(Int.self, forKey: .composingCount)
        )
        inputStyle = try container.decode(ImeInputStyle.self, forKey: .inputStyle)
        mappedIntention = try container.decodeIfPresent(
            String.self,
            forKey: .mappedIntention
        )
        mappedInputOverride = try container.decodeIfPresent(
            String.self,
            forKey: .mappedInputOverride
        )
    }
}

/// A cursor is an index between input elements, not a String.Index.  This is
/// important for romaji tables where the input and displayed lengths differ.
struct CompositionBuffer: Equatable, Codable, Sendable {
    private(set) var elements: [CompositionElement]
    private(set) var cursor: Int

    init(elements: [CompositionElement] = [], cursor: Int? = nil) {
        self.elements = elements
        self.cursor = min(max(cursor ?? elements.count, 0), elements.count)
    }

    private enum CodingKeys: String, CodingKey {
        case elements
        case cursor
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            elements: try container.decode(
                [CompositionElement].self,
                forKey: .elements
            ),
            cursor: try container.decode(Int.self, forKey: .cursor)
        )
    }

    var isEmpty: Bool { elements.isEmpty }
    var text: String { elements.map(\.text).joined() }
    var cursorUtf8ByteOffset: UInt32 {
        UInt32(elements.prefix(cursor).reduce(0) { $0 + $1.text.utf8.count })
    }

    mutating func insert(
        _ text: String,
        inputStyle: ImeInputStyle = .mapped,
        keymap: [String: PinnedKeymapRule] = [:]
    ) {
        let newElements = text.map { character in
            let source = String(character)
            let mapping = inputStyle == .mapped ? keymap[source] : nil
            return CompositionElement(
                text: source,
                inputStyle: inputStyle,
                mappedIntention: mapping?.intention,
                mappedInputOverride: mapping?.inputOverride
            )
        }
        guard !newElements.isEmpty else { return }
        elements.insert(contentsOf: newElements, at: cursor)
        cursor += newElements.count
    }

    mutating func deleteBackward() {
        guard cursor > 0 else { return }
        elements.remove(at: cursor - 1)
        cursor -= 1
    }

    mutating func deleteForward() {
        guard cursor < elements.count else { return }
        elements.remove(at: cursor)
    }

    mutating func moveCursor(by offset: Int) {
        cursor = min(max(cursor + offset, 0), elements.count)
    }

    mutating func moveCursorToStart() { cursor = 0 }
    mutating func moveCursorToEnd() { cursor = elements.count }

    func prefixText(count: Int) -> String {
        elements.prefix(max(0, min(count, elements.count))).map(\.text).joined()
    }

    func suffixText(after count: Int) -> String {
        elements.dropFirst(max(0, min(count, elements.count))).map(\.text).joined()
    }

    @discardableResult
    mutating func removePrefix(count: Int) -> String {
        let amount = max(0, min(count, elements.count))
        let result = prefixText(count: amount)
        elements.removeFirst(amount)
        cursor = max(0, cursor - amount)
        return result
    }
}

struct CompositionInput: Equatable, Codable, Sendable {
    let elements: [CompositionElement]
    let cursor: Int
    let leftContext: String
    let targetCount: Int?
    let mappedTableName: String?

    init(
        elements: [CompositionElement],
        cursor: Int,
        leftContext: String,
        targetCount: Int? = nil,
        mappedTableName: String? = nil
    ) {
        self.elements = elements
        self.cursor = cursor
        self.leftContext = leftContext
        self.targetCount = targetCount
        self.mappedTableName = mappedTableName
    }
}
