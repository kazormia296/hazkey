#ifndef FCITX5_HAZKEY_HAZKEY_ACTION_MAPPER_H_
#define FCITX5_HAZKEY_HAZKEY_ACTION_MAPPER_H_

#include <fcitx-utils/key.h>

#include <optional>

namespace fcitx {

enum class HazkeySemanticActionKind {
    insertText,
    deleteBackward,
    deleteForward,
    moveCursor,
    moveCursorToStart,
    moveCursorToEnd,
    startConversion,
    navigateCandidate,
    navigateCandidatePage,
    resizeSegment,
    commitSelected,
    commitAll,
    cancel,
    selectCandidate,
    transformHiragana,
    transformKatakanaFullwidth,
    transformKatakanaHalfwidth,
    transformAlphabetFullwidth,
    transformAlphabetHalfwidth,
    forgetCandidate,
    reconvert,
    beginUnicodeInput,
    appendUnicodeDigit,
    commitUnicodeInput,
    consume,
    passThrough,
};

enum class HazkeyInputPhase {
    idle,
    composing,
    previewing,
    selecting,
    reconverting,
    unicodeInput,
};

struct HazkeySemanticAction {
    HazkeySemanticActionKind kind;
    int value = 0;
    bool fullwidth = false;
};

/// Maps only platform key meaning. It does not inspect candidate-panel focus
/// and therefore can be unit-tested without an InputContext or server.
std::optional<HazkeySemanticAction> mapHazkeyKey(
    const Key& key, HazkeyInputPhase phase, bool normalSpaceFullwidth);

/// A failed dispatch while already composing must keep the last confirmed
/// preedit and consume the key. A failed first text/Unicode key must fall
/// through so the application does not silently lose user input.
bool shouldAcceptHazkeyDispatch(
    const HazkeySemanticAction& action, HazkeyInputPhase phase,
    bool dispatchSucceeded);

}  // namespace fcitx

#endif  // FCITX5_HAZKEY_HAZKEY_ACTION_MAPPER_H_
