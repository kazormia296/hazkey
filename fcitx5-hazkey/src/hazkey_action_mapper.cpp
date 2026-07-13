#include "hazkey_action_mapper.h"

#include "fcitx-utils/keysym.h"

namespace fcitx {

std::optional<HazkeySemanticAction> mapHazkeyKey(
    const Key& key, HazkeyInputPhase phase, bool normalSpaceFullwidth) {
    const auto sym = key.sym();
    const bool shifted = key.states().test(KeyState::Shift);
    const bool controlled = key.states().test(KeyState::Ctrl);

    if (phase == HazkeyInputPhase::unicodeInput) {
        if (sym == FcitxKey_Escape) {
            return HazkeySemanticAction{HazkeySemanticActionKind::cancel};
        }
        if (sym == FcitxKey_BackSpace) {
            return HazkeySemanticAction{HazkeySemanticActionKind::deleteBackward};
        }
        if (sym == FcitxKey_Return || sym == FcitxKey_space) {
            return HazkeySemanticAction{HazkeySemanticActionKind::commitUnicodeInput};
        }
        const auto text = Key::keySymToUTF8(sym);
        if (!controlled && text.size() == 1 &&
            ((text[0] >= '0' && text[0] <= '9') ||
             (text[0] >= 'a' && text[0] <= 'f') ||
             (text[0] >= 'A' && text[0] <= 'F'))) {
            return HazkeySemanticAction{HazkeySemanticActionKind::appendUnicodeDigit};
        }
        return HazkeySemanticAction{HazkeySemanticActionKind::consume};
    }

    if ((phase == HazkeyInputPhase::idle ||
         phase == HazkeyInputPhase::composing) &&
        controlled && shifted &&
        (sym == FcitxKey_u || sym == FcitxKey_U)) {
        return HazkeySemanticAction{HazkeySemanticActionKind::beginUnicodeInput};
    }

    if (phase == HazkeyInputPhase::selecting) {
        if (controlled && sym == FcitxKey_Delete) {
            return HazkeySemanticAction{HazkeySemanticActionKind::forgetCandidate};
        }
        switch (sym) {
            case FcitxKey_Up:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidate, -1};
            case FcitxKey_Down:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidate, 1};
            case FcitxKey_Page_Up:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidatePage, -1};
            case FcitxKey_Page_Down:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidatePage, 1};
            case FcitxKey_space:
            case FcitxKey_Henkan:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidate,
                                            sym == FcitxKey_space && shifted ? -1 : 1};
            case FcitxKey_Return:
            case FcitxKey_Tab:
            case FcitxKey_Right:
                if (shifted && sym == FcitxKey_Right) {
                    return HazkeySemanticAction{
                        HazkeySemanticActionKind::resizeSegment, 1};
                }
                return HazkeySemanticAction{HazkeySemanticActionKind::commitSelected};
            case FcitxKey_BackSpace:
                return HazkeySemanticAction{HazkeySemanticActionKind::deleteBackward};
            case FcitxKey_Escape:
                return HazkeySemanticAction{HazkeySemanticActionKind::cancel};
            case FcitxKey_Left:
                if (shifted) {
                    return HazkeySemanticAction{
                        HazkeySemanticActionKind::resizeSegment, -1};
                }
                return HazkeySemanticAction{HazkeySemanticActionKind::consume};
            case FcitxKey_Muhenkan:
            case FcitxKey_F6:
            case FcitxKey_Hiragana:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformHiragana};
            case FcitxKey_F7:
            case FcitxKey_Katakana:
            case FcitxKey_Hiragana_Katakana:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformKatakanaFullwidth};
            case FcitxKey_F8:
            case FcitxKey_Hankaku:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformKatakanaHalfwidth};
            case FcitxKey_F9:
            case FcitxKey_Zenkaku:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformAlphabetFullwidth};
            case FcitxKey_F10:
            case FcitxKey_Eisu_Shift:
            case FcitxKey_Eisu_toggle:
            case FcitxKey_Zenkaku_Hankaku:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformAlphabetHalfwidth};
            default:
                return std::nullopt;
        }
    }

    if (phase == HazkeyInputPhase::previewing ||
        phase == HazkeyInputPhase::reconverting) {
        if (controlled && sym == FcitxKey_Delete) {
            return HazkeySemanticAction{HazkeySemanticActionKind::forgetCandidate};
        }
        switch (sym) {
            case FcitxKey_Up:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidate, -1};
            case FcitxKey_Down:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidate, 1};
            case FcitxKey_Page_Up:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidatePage, -1};
            case FcitxKey_Page_Down:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidatePage, 1};
            case FcitxKey_space:
            case FcitxKey_Henkan:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidate,
                                            sym == FcitxKey_space && shifted ? -1 : 1};
            case FcitxKey_Tab:
                return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidate, 0};
            case FcitxKey_Return:
                return HazkeySemanticAction{HazkeySemanticActionKind::commitAll};
            case FcitxKey_Right:
                if (shifted) {
                    return HazkeySemanticAction{
                        HazkeySemanticActionKind::resizeSegment, 1};
                }
                return HazkeySemanticAction{HazkeySemanticActionKind::commitSelected};
            case FcitxKey_Left:
                if (shifted) {
                    return HazkeySemanticAction{
                        HazkeySemanticActionKind::resizeSegment, -1};
                }
                return HazkeySemanticAction{HazkeySemanticActionKind::consume};
            case FcitxKey_BackSpace:
                return HazkeySemanticAction{HazkeySemanticActionKind::deleteBackward};
            case FcitxKey_Escape:
                return HazkeySemanticAction{HazkeySemanticActionKind::cancel};
            case FcitxKey_Muhenkan:
            case FcitxKey_F6:
            case FcitxKey_Hiragana:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformHiragana};
            case FcitxKey_F7:
            case FcitxKey_Katakana:
            case FcitxKey_Hiragana_Katakana:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformKatakanaFullwidth};
            case FcitxKey_F8:
            case FcitxKey_Hankaku:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformKatakanaHalfwidth};
            case FcitxKey_F9:
            case FcitxKey_Zenkaku:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformAlphabetFullwidth};
            case FcitxKey_F10:
            case FcitxKey_Eisu_Shift:
            case FcitxKey_Eisu_toggle:
            case FcitxKey_Zenkaku_Hankaku:
                return HazkeySemanticAction{HazkeySemanticActionKind::transformAlphabetHalfwidth};
            default:
                return std::nullopt;
        }
    }

    if (phase == HazkeyInputPhase::idle) {
        if (sym == FcitxKey_space) {
            return HazkeySemanticAction{
                HazkeySemanticActionKind::insertText,
                0,
                shifted ? !normalSpaceFullwidth : normalSpaceFullwidth,
            };
        }
        if (sym == FcitxKey_Henkan) {
            return HazkeySemanticAction{HazkeySemanticActionKind::reconvert};
        }
        switch (sym) {
            case FcitxKey_Muhenkan:
            case FcitxKey_Hiragana:
            case FcitxKey_Katakana:
            case FcitxKey_Hiragana_Katakana:
            case FcitxKey_Hankaku:
            case FcitxKey_Zenkaku:
            case FcitxKey_Zenkaku_Hankaku:
            case FcitxKey_Eisu_Shift:
            case FcitxKey_Eisu_toggle:
                return HazkeySemanticAction{HazkeySemanticActionKind::passThrough};
            default:
                break;
        }
        return std::nullopt;
    }

    if (controlled) {
        switch (sym) {
            case FcitxKey_a:
            case FcitxKey_A:
                return HazkeySemanticAction{HazkeySemanticActionKind::moveCursorToStart};
            case FcitxKey_e:
            case FcitxKey_E:
                return HazkeySemanticAction{HazkeySemanticActionKind::moveCursorToEnd};
            case FcitxKey_b:
            case FcitxKey_B:
                return HazkeySemanticAction{HazkeySemanticActionKind::moveCursor, -1};
            case FcitxKey_f:
            case FcitxKey_F:
                return HazkeySemanticAction{HazkeySemanticActionKind::moveCursor, 1};
            case FcitxKey_h:
            case FcitxKey_H:
                return HazkeySemanticAction{HazkeySemanticActionKind::deleteBackward};
            case FcitxKey_d:
            case FcitxKey_D:
                return HazkeySemanticAction{HazkeySemanticActionKind::deleteForward};
            default:
                break;
        }
    }

    switch (sym) {
        case FcitxKey_Left:
            if (shifted) {
                return HazkeySemanticAction{
                    HazkeySemanticActionKind::resizeSegment, -1};
            }
            return HazkeySemanticAction{HazkeySemanticActionKind::moveCursor, -1};
        case FcitxKey_Right:
            if (shifted) {
                return HazkeySemanticAction{
                    HazkeySemanticActionKind::resizeSegment, 1};
            }
            return HazkeySemanticAction{HazkeySemanticActionKind::moveCursor, 1};
        case FcitxKey_Home:
            return HazkeySemanticAction{HazkeySemanticActionKind::moveCursorToStart};
        case FcitxKey_End:
            return HazkeySemanticAction{HazkeySemanticActionKind::moveCursorToEnd};
        case FcitxKey_BackSpace:
            return HazkeySemanticAction{HazkeySemanticActionKind::deleteBackward};
        case FcitxKey_Delete:
            return HazkeySemanticAction{HazkeySemanticActionKind::deleteForward};
        case FcitxKey_space:
        case FcitxKey_Henkan:
            return HazkeySemanticAction{HazkeySemanticActionKind::startConversion};
        case FcitxKey_Down:
        case FcitxKey_Tab:
            return HazkeySemanticAction{HazkeySemanticActionKind::navigateCandidate, 0};
        case FcitxKey_Return:
            return HazkeySemanticAction{HazkeySemanticActionKind::commitAll};
        case FcitxKey_Escape:
            return HazkeySemanticAction{HazkeySemanticActionKind::cancel};
        case FcitxKey_Muhenkan:
        case FcitxKey_F6:
        case FcitxKey_Hiragana:
            return HazkeySemanticAction{HazkeySemanticActionKind::transformHiragana};
        case FcitxKey_F7:
        case FcitxKey_Katakana:
        case FcitxKey_Hiragana_Katakana:
            return HazkeySemanticAction{HazkeySemanticActionKind::transformKatakanaFullwidth};
        case FcitxKey_F8:
        case FcitxKey_Hankaku:
            return HazkeySemanticAction{HazkeySemanticActionKind::transformKatakanaHalfwidth};
        case FcitxKey_F9:
        case FcitxKey_Zenkaku:
            return HazkeySemanticAction{HazkeySemanticActionKind::transformAlphabetFullwidth};
        case FcitxKey_F10:
        case FcitxKey_Eisu_Shift:
        case FcitxKey_Eisu_toggle:
        case FcitxKey_Zenkaku_Hankaku:
            return HazkeySemanticAction{HazkeySemanticActionKind::transformAlphabetHalfwidth};
        default:
            return std::nullopt;
    }
}

bool shouldAcceptHazkeyDispatch(
    const HazkeySemanticAction& action, HazkeyInputPhase phase,
    bool dispatchSucceeded) {
    if (dispatchSucceeded || phase != HazkeyInputPhase::idle) {
        return true;
    }
    return action.kind != HazkeySemanticActionKind::insertText &&
           action.kind != HazkeySemanticActionKind::beginUnicodeInput;
}

}  // namespace fcitx
