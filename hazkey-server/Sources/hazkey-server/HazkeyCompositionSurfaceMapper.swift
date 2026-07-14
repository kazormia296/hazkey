import Foundation
import KanaKanjiConverterModule

/// Converts reducer-owned input elements into the rendered reading shared by
/// the in-process Hazkey adapter and the optional Mozc process boundary.
/// Indices remain input-element boundaries; the external helper only sees the
/// rendered reading and Unicode-scalar key counts.
struct HazkeyCompositionSurfaceMapper {
    private let mappedInputStyleProvider: () -> InputStyle

    init(
        mappedInputStyleProvider: @escaping () -> InputStyle = { .roman2kana }
    ) {
        self.mappedInputStyleProvider = mappedInputStyleProvider
    }

    func composingText(for composition: CompositionInput) -> ComposingText {
        composingText(
            from: composition.elements[...],
            mappedTableName: composition.mappedTableName
        )
    }

    func composingText(
        from elements: ArraySlice<CompositionElement>,
        mappedTableName: String?
    ) -> ComposingText {
        var composingText = ComposingText()
        for element in elements {
            let inputStyle: InputStyle
            switch element.inputStyle {
            case .direct:
                inputStyle = .direct
            case .mapped:
                if let mappedTableName {
                    inputStyle = .mapped(id: .tableName(mappedTableName))
                } else {
                    inputStyle = mappedInputStyleProvider()
                }
            }
            if element.inputStyle == .mapped,
               let intention = element.mappedIntention?.first,
               let input = (element.mappedInputOverride ?? element.text).first {
                composingText.insertAtCursorPosition([
                    ComposingText.InputElement(
                        piece: .key(
                            intention: intention,
                            input: input,
                            modifiers: []
                        ),
                        inputStyle: inputStyle
                    )
                ])
            } else {
                composingText.insertAtCursorPosition(
                    element.text,
                    inputStyle: inputStyle
                )
            }
        }
        return composingText
    }

    func display(for composition: CompositionInput) -> CompositionDisplay {
        let text = composingText(for: composition)
        let inputCursor = min(max(composition.cursor, 0), text.input.count)
        let indexMap = text.inputIndexToSurfaceIndexMap()
        let mappedCursor = indexMap[inputCursor]
            ?? indexMap
                .filter { $0.key < inputCursor }
                .max(by: { $0.key < $1.key })?
                .value
            ?? 0
        let surfaceCursor = min(max(mappedCursor, 0), text.convertTarget.count)
        return CompositionDisplay(
            text: text.convertTarget,
            caretUtf8ByteOffset: UInt32(
                text.convertTarget.prefix(surfaceCursor).utf8.count
            )
        )
    }

    func inputCursorPosition(
        for composition: CompositionInput,
        movingBy offset: Int
    ) -> Int {
        let text = composingText(for: composition)
        let cursor = min(max(composition.cursor, 0), text.input.count)
        guard offset != 0 else { return cursor }

        var boundarySet = Set(
            text.inputIndexToSurfaceIndexMap().keys.filter {
                (0...text.input.count).contains($0)
            }
        )
        boundarySet.insert(0)
        boundarySet.insert(text.input.count)
        let boundaries = boundarySet.sorted()
        if offset > 0 {
            let following = boundaries.filter { $0 > cursor }
            guard !following.isEmpty else { return cursor }
            return following[min(offset - 1, following.count - 1)]
        }
        let preceding = boundaries.filter { $0 < cursor }
        guard !preceding.isEmpty else { return cursor }
        let additionalSteps = min(-(offset + 1), preceding.count - 1)
        return preceding[preceding.count - 1 - additionalSteps]
    }

    /// Maps a stable reducer input boundary to the key-size unit used by
    /// Mozc's Segment API. Nil means the requested input boundary is inside a
    /// still-dependent Romaji sequence and cannot be resized safely.
    func keySize(
        forInputCount inputCount: Int,
        in composition: CompositionInput
    ) -> Int? {
        let text = composingText(for: composition)
        let count = min(max(inputCount, 0), text.input.count)
        let surfaceCount: Int?
        if count == text.input.count {
            surfaceCount = text.convertTarget.count
        } else {
            surfaceCount = text.inputIndexToSurfaceIndexMap()[count]
        }
        guard let surfaceCount else { return nil }
        return text.convertTarget.prefix(surfaceCount).unicodeScalars.count
    }

    /// Converts Mozc's consumed reading length back to a reducer input-element
    /// boundary. Only exact stable boundaries are accepted so a response can
    /// never split an unresolved Romaji sequence.
    func inputCount(
        forKeySize keySize: Int,
        in composition: CompositionInput
    ) -> Int? {
        let text = composingText(for: composition)
        guard (0...text.convertTarget.unicodeScalars.count).contains(keySize) else {
            return nil
        }
        var boundaries = text.inputIndexToSurfaceIndexMap()
        boundaries[text.input.count] = text.convertTarget.count
        return boundaries
            .filter { inputIndex, surfaceIndex in
                (0...text.input.count).contains(inputIndex)
                    && text.convertTarget
                        .prefix(surfaceIndex)
                        .unicodeScalars
                        .count == keySize
            }
            .map(\.key)
            .max()
    }
}
