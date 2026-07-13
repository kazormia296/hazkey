#include <cstdlib>
#include <array>
#include <iostream>
#include <string>

#include <fcitx-utils/key.h>
#include <fcitx-utils/keysym.h>

#include "hazkey_action_mapper.h"

namespace {
using fcitx::HazkeySemanticActionKind;
using fcitx::HazkeyInputPhase;
using fcitx::Key;
using fcitx::KeyState;
using fcitx::KeySym;

void expect(bool value, const std::string& message) {
    if (!value) {
        std::cerr << message << '\n';
        std::exit(1);
    }
}

void mapsSpaceWidthByModeAndShift() {
    const auto full = fcitx::mapHazkeyKey(
        Key(FcitxKey_space), HazkeyInputPhase::idle, true);
    const auto half = fcitx::mapHazkeyKey(
        Key(FcitxKey_space, KeyState::Shift), HazkeyInputPhase::idle, true);
    expect(full && full->kind == HazkeySemanticActionKind::insertText && full->fullwidth,
           "normal fullwidth space must be semantic insertion");
    expect(half && half->kind == HazkeySemanticActionKind::insertText && !half->fullwidth,
           "shifted fullwidth space must invert width");

    const auto normalHalf = fcitx::mapHazkeyKey(
        Key(FcitxKey_space), HazkeyInputPhase::idle, false);
    const auto shiftedFull = fcitx::mapHazkeyKey(
        Key(FcitxKey_space, KeyState::Shift), HazkeyInputPhase::idle, false);
    expect(normalHalf && !normalHalf->fullwidth,
           "normal halfwidth space setting must stay halfwidth");
    expect(shiftedFull && shiftedFull->fullwidth,
           "Shift+Space must invert a halfwidth default");

    const auto nextCandidate = fcitx::mapHazkeyKey(
        Key(FcitxKey_space), HazkeyInputPhase::selecting, true);
    const auto previousCandidate = fcitx::mapHazkeyKey(
        Key(FcitxKey_space, KeyState::Shift), HazkeyInputPhase::selecting, true);
    expect(nextCandidate && nextCandidate->value == 1,
           "candidate Space must move forward instead of inserting whitespace");
    expect(previousCandidate && previousCandidate->value == -1,
           "candidate Shift+Space must move backward instead of inserting whitespace");
}

void separatesCandidateNavigationFromSegmentMovement() {
    const auto page = fcitx::mapHazkeyKey(
        Key(FcitxKey_Page_Down), HazkeyInputPhase::selecting, true);
    expect(page && page->kind == HazkeySemanticActionKind::navigateCandidatePage && page->value == 1,
           "PageDown must be candidate page navigation");

    for (const auto phase : {HazkeyInputPhase::previewing,
                             HazkeyInputPhase::selecting,
                             HazkeyInputPhase::reconverting}) {
        const auto left = fcitx::mapHazkeyKey(
            Key(FcitxKey_Left), phase, true);
        const auto right = fcitx::mapHazkeyKey(
            Key(FcitxKey_Right), phase, true);
        const auto shrink = fcitx::mapHazkeyKey(
            Key(FcitxKey_Left, KeyState::Shift), phase, true);
        const auto expand = fcitx::mapHazkeyKey(
            Key(FcitxKey_Right, KeyState::Shift), phase, true);

        expect(left &&
                   left->kind == HazkeySemanticActionKind::moveActiveSegment &&
                   left->value == -1,
               "conversion Left must focus the previous segment");
        expect(right &&
                   right->kind == HazkeySemanticActionKind::moveActiveSegment &&
                   right->value == 1,
               "conversion Right must focus the next segment");
        expect(shrink &&
                   shrink->kind == HazkeySemanticActionKind::resizeSegment &&
                   shrink->value == -1,
               "Shift+Left must shrink the active segment");
        expect(expand &&
                   expand->kind == HazkeySemanticActionKind::resizeSegment &&
                   expand->value == 1,
               "Shift+Right must expand the active segment");
    }
}

void mapsEditorKeysWithoutCandidateFocus() {
    const auto home = fcitx::mapHazkeyKey(
        Key(FcitxKey_Home), HazkeyInputPhase::composing, true);
    const auto backspace = fcitx::mapHazkeyKey(
        Key(FcitxKey_BackSpace), HazkeyInputPhase::composing, true);
    const auto left = fcitx::mapHazkeyKey(
        Key(FcitxKey_Left), HazkeyInputPhase::composing, true);
    expect(home && home->kind == HazkeySemanticActionKind::moveCursorToStart,
           "Home must move to the composition start");
    expect(backspace && backspace->kind == HazkeySemanticActionKind::deleteBackward,
           "Backspace must delete backward");
    expect(left && left->kind == HazkeySemanticActionKind::moveCursor &&
                       left->value == -1,
           "composing Left must keep moving the editing cursor");
}

void mapsJapaneseKeyboardKeys() {
    const auto muhenkan = fcitx::mapHazkeyKey(
        Key(FcitxKey_Muhenkan), HazkeyInputPhase::composing, true);
    const auto hiragana = fcitx::mapHazkeyKey(
        Key(FcitxKey_Hiragana), HazkeyInputPhase::composing, true);
    const auto eisu = fcitx::mapHazkeyKey(
        Key(FcitxKey_Eisu_toggle), HazkeyInputPhase::composing, true);
    expect(muhenkan && muhenkan->kind == HazkeySemanticActionKind::transformHiragana,
           "Muhenkan must be handled semantically");
    expect(hiragana && hiragana->kind == HazkeySemanticActionKind::transformHiragana,
           "Kana/Hiragana must be handled semantically");
    expect(eisu && eisu->kind == HazkeySemanticActionKind::transformAlphabetHalfwidth,
           "Eisu must be handled semantically");
}

void mapsEveryJapaneseModeKeyAcrossPhases() {
    struct ExpectedMapping {
        KeySym symbol;
        HazkeySemanticActionKind kind;
    };
    const std::array<ExpectedMapping, 9> mappings = {{
        {FcitxKey_Muhenkan, HazkeySemanticActionKind::transformHiragana},
        {FcitxKey_Hiragana, HazkeySemanticActionKind::transformHiragana},
        {FcitxKey_Katakana, HazkeySemanticActionKind::transformKatakanaFullwidth},
        {FcitxKey_Hiragana_Katakana,
         HazkeySemanticActionKind::transformKatakanaFullwidth},
        {FcitxKey_Hankaku, HazkeySemanticActionKind::transformKatakanaHalfwidth},
        {FcitxKey_Zenkaku, HazkeySemanticActionKind::transformAlphabetFullwidth},
        {FcitxKey_Zenkaku_Hankaku,
         HazkeySemanticActionKind::transformAlphabetHalfwidth},
        {FcitxKey_Eisu_Shift, HazkeySemanticActionKind::transformAlphabetHalfwidth},
        {FcitxKey_Eisu_toggle, HazkeySemanticActionKind::transformAlphabetHalfwidth},
    }};
    const std::array<HazkeyInputPhase, 4> activePhases = {
        HazkeyInputPhase::composing,
        HazkeyInputPhase::previewing,
        HazkeyInputPhase::selecting,
        HazkeyInputPhase::reconverting,
    };

    for (const auto& mapping : mappings) {
        const auto idle = fcitx::mapHazkeyKey(
            Key(mapping.symbol), HazkeyInputPhase::idle, true);
        expect(idle && idle->kind == HazkeySemanticActionKind::passThrough,
               "idle Japanese mode key must have an explicit fallthrough policy");
        for (const auto phase : activePhases) {
            const auto action = fcitx::mapHazkeyKey(
                Key(mapping.symbol), phase, true);
            expect(action && action->kind == mapping.kind,
                   "Japanese mode key must map in every active phase");
        }
    }

    const auto idleHenkan = fcitx::mapHazkeyKey(
        Key(FcitxKey_Henkan), HazkeyInputPhase::idle, true);
    const auto composingHenkan = fcitx::mapHazkeyKey(
        Key(FcitxKey_Henkan), HazkeyInputPhase::composing, true);
    const auto selectingHenkan = fcitx::mapHazkeyKey(
        Key(FcitxKey_Henkan), HazkeyInputPhase::selecting, true);
    expect(idleHenkan && idleHenkan->kind == HazkeySemanticActionKind::reconvert,
           "idle Henkan must reconvert selected surrounding text");
    expect(composingHenkan &&
               composingHenkan->kind == HazkeySemanticActionKind::startConversion,
           "composing Henkan must start conversion");
    expect(selectingHenkan &&
               selectingHenkan->kind == HazkeySemanticActionKind::navigateCandidate &&
               selectingHenkan->value == 1,
           "selecting Henkan must advance the candidate");
}

void leavesUsAndJisPrintableSymbolsForTextInsertion() {
    for (const auto symbol : {FcitxKey_a, FcitxKey_at, FcitxKey_bracketleft,
                              FcitxKey_backslash, FcitxKey_yen}) {
        const auto idle = fcitx::mapHazkeyKey(
            Key(symbol), HazkeyInputPhase::idle, true);
        const auto composing = fcitx::mapHazkeyKey(
            Key(symbol), HazkeyInputPhase::composing, true);
        expect(!idle && !composing,
               "printable US/JIS symbols must remain text, not control actions");
    }
}

void mapsP1SemanticActions() {
    const auto forget = fcitx::mapHazkeyKey(
        Key(FcitxKey_Delete, KeyState::Ctrl), HazkeyInputPhase::selecting, true);
    expect(forget && forget->kind == HazkeySemanticActionKind::forgetCandidate,
           "Ctrl+Delete must forget the selected candidate");

    const auto reconvert = fcitx::mapHazkeyKey(
        Key(FcitxKey_Henkan), HazkeyInputPhase::idle, true);
    expect(reconvert && reconvert->kind == HazkeySemanticActionKind::reconvert,
           "idle Henkan must request selected-text reconversion");

    const auto beginUnicode = fcitx::mapHazkeyKey(
        Key(FcitxKey_u, KeyState::Ctrl_Shift), HazkeyInputPhase::idle, true);
    expect(beginUnicode &&
               beginUnicode->kind == HazkeySemanticActionKind::beginUnicodeInput,
           "Ctrl+Shift+U must begin Unicode input");
    const auto hexDigit = fcitx::mapHazkeyKey(
        Key(FcitxKey_f), HazkeyInputPhase::unicodeInput, true);
    expect(hexDigit &&
               hexDigit->kind == HazkeySemanticActionKind::appendUnicodeDigit,
           "hexadecimal keys must append during Unicode input");
    const auto finishUnicode = fcitx::mapHazkeyKey(
        Key(FcitxKey_space), HazkeyInputPhase::unicodeInput, true);
    expect(finishUnicode &&
               finishUnicode->kind == HazkeySemanticActionKind::commitUnicodeInput,
           "Space must finish Unicode input");

    const auto emacsStart = fcitx::mapHazkeyKey(
        Key(FcitxKey_a, KeyState::Ctrl), HazkeyInputPhase::composing, true);
    expect(emacsStart &&
               emacsStart->kind == HazkeySemanticActionKind::moveCursorToStart,
           "Ctrl+A must move to the composition start");
}

void followsExplicitSnapshotPhase() {
    const auto down = fcitx::mapHazkeyKey(
        Key(FcitxKey_Down), HazkeyInputPhase::composing, true);
    expect(down && down->kind == HazkeySemanticActionKind::navigateCandidate &&
                    down->value == 0,
           "composing Down must enter candidate selection semantically");

    const auto resize = fcitx::mapHazkeyKey(
        Key(FcitxKey_Left, KeyState::Shift), HazkeyInputPhase::composing, true);
    expect(resize && resize->kind == HazkeySemanticActionKind::resizeSegment &&
                      resize->value == -1,
           "composing Shift+Left must start segment editing");

    const auto previewEnter = fcitx::mapHazkeyKey(
        Key(FcitxKey_Return), HazkeyInputPhase::previewing, true);
    const auto selectionEnter = fcitx::mapHazkeyKey(
        Key(FcitxKey_Return), HazkeyInputPhase::selecting, true);
    const auto selectionTab = fcitx::mapHazkeyKey(
        Key(FcitxKey_Tab), HazkeyInputPhase::selecting, true);
    expect(previewEnter && previewEnter->kind == HazkeySemanticActionKind::commitAll,
           "preview Enter must commit the entire displayed preedit");
    expect(selectionEnter &&
               selectionEnter->kind == HazkeySemanticActionKind::commitAll,
           "selection Enter must commit the entire segmented conversion");
    expect(selectionTab &&
               selectionTab->kind == HazkeySemanticActionKind::commitSelected,
           "selection Tab must preserve partial-commit compatibility");
}

void preservesInputWhenTheFirstDispatchFails() {
    const fcitx::HazkeySemanticAction insert{
        HazkeySemanticActionKind::insertText};
    expect(!fcitx::shouldAcceptHazkeyDispatch(
               insert, HazkeyInputPhase::idle, false),
           "a failed first text action must fall through to the application");
    expect(fcitx::shouldAcceptHazkeyDispatch(
               insert, HazkeyInputPhase::composing, false),
           "a failed composing action must preserve and retain the preedit");
    expect(fcitx::shouldAcceptHazkeyDispatch(
               insert, HazkeyInputPhase::idle, true),
           "a confirmed first text action must be consumed");

    const fcitx::HazkeySemanticAction unicode{
        HazkeySemanticActionKind::beginUnicodeInput};
    expect(!fcitx::shouldAcceptHazkeyDispatch(
               unicode, HazkeyInputPhase::idle, false),
           "a failed Unicode entry shortcut must not be swallowed");
}
}  // namespace

int main() {
    mapsSpaceWidthByModeAndShift();
    separatesCandidateNavigationFromSegmentMovement();
    mapsEditorKeysWithoutCandidateFocus();
    mapsJapaneseKeyboardKeys();
    mapsEveryJapaneseModeKeyAcrossPhases();
    leavesUsAndJisPrintableSymbolsForTextInsertion();
    mapsP1SemanticActions();
    followsExplicitSnapshotPhase();
    preservesInputWhenTheFirstDispatchFails();
    return 0;
}
