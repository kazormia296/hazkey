#include "hazkey_state.h"

#include <fcitx-utils/key.h>
#include <fcitx-utils/keysym.h>
#include <fcitx-utils/log.h>
#include <fcitx-utils/utf8.h>
#include <fcitx/candidatelist.h>

#include <algorithm>
#include <cstdint>
#include <ctime>
#include <limits>
#include <optional>
#include <string>
#include <utility>

#include "commands.pb.h"
#include "hazkey_engine.h"
#include "hazkey_snapshot_renderer.h"
#include "hazkey_text_offset.h"

namespace fcitx {

namespace {

HazkeyClientContext makeClientContext(InputContext* inputContext,
                                      CapabilityFlags flags) {
    const char* frontend = inputContext->frontend();
    return HazkeyClientContext{
        .program = inputContext->program(),
        .frontend = frontend == nullptr ? "" : frontend,
        .secureInput = flags.testAny(CapabilityFlag::PasswordOrSensitive),
    };
}

bool hasTextInputModifiers(const Key& key) {
    return key.states().test(KeyState::Ctrl) ||
           key.states().test(KeyState::Alt) ||
           key.states().test(KeyState::Super) ||
           key.states().test(KeyState::Super2) ||
           key.states().test(KeyState::Meta);
}

bool isPendingLearningCancellationKey(const Key& key) {
    const auto sym = key.sym();
    if (sym == FcitxKey_Escape || sym == FcitxKey_BackSpace ||
        sym == FcitxKey_Delete) {
        return true;
    }
    return key.states().test(KeyState::Ctrl) &&
           (sym == FcitxKey_z || sym == FcitxKey_Z);
}

HazkeyInputPhase inputPhaseFor(hazkey::ImePhase phase) {
    switch (phase) {
        case hazkey::COMPOSING:
            return HazkeyInputPhase::composing;
        case hazkey::PREVIEWING:
            return HazkeyInputPhase::previewing;
        case hazkey::SELECTING:
            return HazkeyInputPhase::selecting;
        case hazkey::RECONVERTING:
            return HazkeyInputPhase::reconverting;
        case hazkey::UNICODE_INPUT:
            return HazkeyInputPhase::unicodeInput;
        case hazkey::IDLE:
        case hazkey::IME_PHASE_UNSPECIFIED:
        default:
            return HazkeyInputPhase::idle;
    }
}

}  // namespace

HazkeyState::HazkeyState(HazkeyEngine* engine, InputContext* ic)
    : engine_(engine),
      ic_(ic),
      // InputContext properties can be created from InputContext's base
      // constructor. Virtual frontend/program access is not valid until that
      // constructor has completed; activate/key events rebind the real client
      // context before semantic input is handled.
      server_(engine->server(), HazkeyClientContext{},
              [this] { discardLocalComposition(); }) {
    protocolAvailable_ = server_.supportsV2();
    snapshot_.set_phase(hazkey::IDLE);
    if (!protocolAvailable_) {
        FCITX_ERROR()
            << "Grimodex server does not support required IME Protocol v2";
    }
}

HazkeyState::~HazkeyState() {
    cancelLiveConversionTimer();
    const bool acceptedPendingLearning =
        snapshot_.phase() == hazkey::IDLE && snapshot_.pending_learning();
    // Destruction has no UI target for effects. Accepted text defaults to
    // commit-learning; active/unconfirmed composition defaults to discard. A
    // previously journaled Backspace decision overrides either default.
    server_.finalizeWithoutUITarget(acceptedPendingLearning);
}

void HazkeyState::capabilityAboutToChange(CapabilityFlags newFlags) {
    auto nextContext = makeClientContext(ic_, newFlags);
    const auto transition =
        evaluateHazkeyClientContextTransition(server_.context(), nextContext);
    const bool retryTransport = !protocolAvailable_;
    if (!transition.contextChanged) {
        if (!protocolAvailable_) {
            // A bounded lifecycle send may have disconnected while retaining
            // the journal's exact old-session binding. Resume it through the
            // normal transport so reconnect -> SESSION_NOT_FOUND ->
            // open/restore/rebind can run before this key is mapped.
            bool recovered = false;
            if (server_.hasPendingV2()) {
                recovered = flushDeferredActions(true, false);
            } else {
                const bool updated =
                    server_.updateClientContext(std::move(nextContext));
                // updateContext may have confirmed restore/fallback effects.
                // Apply that effect-only handoff before protocol input resumes.
                recovered = updated && flushDeferredActions(true);
            }
            protocolAvailable_ = recovered && server_.supportsV2();
        }
        return;
    }
    cancelLiveConversionTimer();

    if (transition.clearPreedit) {
        // Never replay arbitrary old-domain journal entries here. A lost
        // commit could carry COMMIT_TEXT/DELETE effects that must not be
        // applied after focus crosses a secure or program boundary. Instead,
        // hand off discard+close, erase all local old-domain state, and install
        // the new context even if its open must be retried later.
        const bool acceptedPendingLearning =
            snapshot_.phase() == hazkey::IDLE && snapshot_.pending_learning();
        server_.finalizeWithoutUITarget(acceptedPendingLearning);
        discardLocalComposition();
        const bool updated =
            server_.updateClientContext(std::move(nextContext));
        protocolAvailable_ = updated && server_.supportsV2();
        return;
    }

    if (snapshot_.pending_learning()) {
        // A frontend transport replacement preserves the field/domain but
        // closes the old server session. Resolve its accepted text before that
        // explicit close so the new session cannot expose a phantom undo.
        (void)resolvePendingLearning(true);
    }
    if (!flushDeferredActions(retryTransport)) {
        // A frontend-only transition may retain composition, so leave its
        // journal/effects in place and retry on the next capability event.
        protocolAvailable_ = false;
        return;
    }
    if (snapshot_.pending_learning()) {
        // The first flush may itself have recovered a lost commit and exposed
        // newly staged learning. Resolve that transaction before closing the
        // old frontend session; otherwise the new session would retain a
        // phantom local undo for learning already committed by explicit close.
        (void)resolvePendingLearning(true);
        if (!flushDeferredActions(retryTransport)) {
            protocolAvailable_ = false;
            return;
        }
    }
    const bool updated = server_.updateClientContext(std::move(nextContext));
    if (!updated) {
        FCITX_ERROR() << "Failed to replace Grimodex session after context change";
    }
    const bool recoveryEffectsApplied =
        !updated || flushDeferredActions(retryTransport);
    protocolAvailable_ = updated && recoveryEffectsApplied &&
                         server_.supportsV2();
}

void HazkeyState::discardLocalComposition() {
    cancelLiveConversionTimer();
    snapshot_.Clear();
    snapshot_.set_phase(hazkey::IDLE);
    ic_->inputPanel().reset();
    ic_->updatePreedit();
    ic_->updateUserInterface(UserInterfaceComponent::InputPanel, true);
}

bool HazkeyState::isInputableEvent(const KeyEvent& event) const {
    const auto key = event.key();
    if (hasTextInputModifiers(key)) {
        return false;
    }
    return key.check(FcitxKey_space) || key.isSimple() ||
           Key::keySymToUTF8(key.sym()).size() > 1 ||
           (key.sym() >= 0x04a1 && key.sym() <= 0x04df);
}

bool HazkeyState::isAltDigitKeyEvent(const KeyEvent& event) const {
    const auto key = event.key();
    return key.states() == KeyState::Alt && key.sym() >= FcitxKey_1 &&
           key.sym() <= FcitxKey_9;
}

void HazkeyState::commitPreedit() {
    cancelLiveConversionTimer();
    const auto originalPhase = snapshot_.phase();
    std::string visibleText;
    for (const auto& span : snapshot_.preedit()) {
        visibleText.append(span.text());
    }
    if (!protocolAvailable_) {
        if (!visibleText.empty() &&
            server_.isLocalTextFallbackSemanticallySafe()) {
            ic_->commitString(visibleText);
        }
        server_.finalizeWithoutUITarget(
            originalPhase == hazkey::IDLE && snapshot_.pending_learning());
        discardLocalComposition();
        return;
    }
    const bool hadVisibleComposition = snapshot_.phase() != hazkey::IDLE;
    bool commitConfirmed = !hadVisibleComposition;
    if (hadVisibleComposition) {
        commitConfirmed = dispatchV2(
            HazkeySemanticAction{HazkeySemanticActionKind::commitAll});
        if (!commitConfirmed) {
            commitConfirmed = flushDeferredActions(false);
        }
        commitConfirmed = commitConfirmed && snapshot_.phase() == hazkey::IDLE;
    }
    if (!commitConfirmed) {
        // The server effect was not confirmed, so commit the last rendered text
        // locally exactly once and discard all late effects from that session.
        std::string remainingText;
        for (const auto& span : snapshot_.preedit()) {
            remainingText.append(span.text());
        }
        if (!remainingText.empty() &&
            server_.isLocalTextFallbackSemanticallySafe()) {
            ic_->commitString(remainingText);
        }
        server_.finalizeWithoutUITarget(false);
        discardLocalComposition();
        protocolAvailable_ = false;
        return;
    }

    // Only after COMMIT_TEXT has been applied may its learning transaction be
    // committed. This two-phase ordering prevents learning text the app never
    // received when the second lifecycle exchange stalls.
    if (hadVisibleComposition || snapshot_.pending_learning()) {
        (void)resolvePendingLearning(true);
    }
    if (!flushDeferredActions(false)) {
        server_.finalizeWithoutUITarget(true);
        discardLocalComposition();
        protocolAvailable_ = false;
    }
}

void HazkeyState::keyEvent(KeyEvent& event) {
    capabilityAboutToChange(ic_->capabilityFlags());
    if (!protocolAvailable_) {
        // Never let application input overtake an older journaled key/effect or
        // visible composition. Keep recovery fail-closed until it drains or an
        // explicit abandonment boundary clears the retained state atomically.
        if (server_.hasPendingV2() || snapshot_.phase() != hazkey::IDLE ||
            snapshot_.pending_learning()) {
            event.filterAndAccept();
        } else {
            event.filter();
        }
        return;
    }
    keyEventV2(event);
}

void HazkeyState::keyEventV2(KeyEvent& event) {
    if (event.isRelease()) {
        return;
    }

    // Recovery may change the semantic phase (for example, a lost commit can
    // become IDLE+pending while this Backspace is arriving). Apply the bounded
    // replay before mapping the key so it is interpreted from the recovered
    // snapshot rather than stale local composition.
    if (server_.hasPendingV2()) {
        const bool bounded = flushDeferredActions(false, false);
        if (!bounded && !flushDeferredActions(true, false)) {
            protocolAvailable_ = false;
            event.filterAndAccept();
            return;
        }
    }

    const auto phase = inputPhaseFor(snapshot_.phase());
    const bool idlePendingLearning =
        phase == HazkeyInputPhase::idle && snapshot_.pending_learning();
    // A just-committed surface can leave learning staged while the protocol
    // is already idle. These keys are cancellation signals for the staged
    // transaction, but must remain visible to the application (for example,
    // Backspace must not be swallowed merely because learning is pending).
    if (idlePendingLearning &&
        isPendingLearningCancellationKey(event.key())) {
        (void)resolvePendingLearning(false);
        event.filter();
        return;
    }

    const bool composing = phase != HazkeyInputPhase::idle;
    const bool hasCandidates =
        snapshot_.candidate_window().items_size() > 0;
    auto action = mapHazkeyKey(
        event.key(), phase, engine_->config().normalSpaceFullwidth.value());

    int selectionRow = -1;
    if (hasCandidates && isAltDigitKeyEvent(event)) {
        selectionRow = event.key().sym() - FcitxKey_1;
    } else if (hasCandidates) {
        selectionRow = event.key().keyListIndex(defaultSelectionKeys);
    }
    if (selectionRow >= 0) {
        const auto pageSize = std::max(
            1, static_cast<int>(snapshot_.candidate_window().page_size()));
        const auto selected = snapshot_.candidate_window().has_selected_index()
                                  ? static_cast<int>(
                                        snapshot_.candidate_window().selected_index())
                                  : 0;
        action = HazkeySemanticAction{
            HazkeySemanticActionKind::selectCandidate,
            (selected / pageSize) * pageSize + selectionRow,
        };
    }
    if (!action.has_value() && isInputableEvent(event)) {
        action = HazkeySemanticAction{HazkeySemanticActionKind::insertText};
    }
    if (!action.has_value() ||
        action->kind == HazkeySemanticActionKind::passThrough) {
        // A navigation/mode key that goes directly to the application closes
        // the one-key undo window. Otherwise a much later Backspace could
        // discard learning for text that is no longer adjacent to the cursor.
        if (idlePendingLearning) {
            (void)resolvePendingLearning(true);
        }
        event.filter();
        return;
    }
    // Every semantic key supersedes a pending debounce. A successful text edit
    // response will install the replacement timer through its ClientEffect.
    cancelLiveConversionTimer();
    if (action->kind == HazkeySemanticActionKind::consume) {
        event.filterAndAccept();
        return;
    }

    bool dispatchSucceeded = true;
    if (action->kind == HazkeySemanticActionKind::selectCandidate) {
        selectV2Candidate(action->value);
    } else if (action->kind == HazkeySemanticActionKind::forgetCandidate) {
        const int selected = snapshot_.candidate_window().has_selected_index()
                                 ? static_cast<int>(
                                       snapshot_.candidate_window().selected_index())
                                 : 0;
        forgetV2Candidate(selected);
    } else if (action->kind == HazkeySemanticActionKind::reconvert) {
        dispatchSucceeded = reconvertV2Selection();
        if (!dispatchSucceeded) {
            if (idlePendingLearning) {
                (void)resolvePendingLearning(true);
            }
            event.filter();
            return;
        }
    } else if (action->kind == HazkeySemanticActionKind::insertText) {
        if (!composing) {
            updateSurroundingTextV2();
        }
        std::string text;
        if (event.key().sym() == FcitxKey_space) {
            text = action->fullwidth ? "　" : " ";
        } else {
            text = Key::keySymToUTF8(event.key().sym());
        }
        if (text.empty()) {
            if (idlePendingLearning) {
                (void)resolvePendingLearning(true);
            }
            event.filter();
            return;
        }
        dispatchSucceeded = dispatchV2(*action, text);
        if (dispatchSucceeded && !composing &&
            event.key().sym() == FcitxKey_space) {
            // Once insertion is confirmed the key belongs to the IME. A
            // failed follow-up commit leaves the confirmed space visible for
            // retry; falling through here would duplicate it in the client.
            (void)dispatchV2(
                HazkeySemanticAction{HazkeySemanticActionKind::commitAll});
        }
    } else if (action->kind ==
               HazkeySemanticActionKind::appendUnicodeDigit) {
        dispatchSucceeded = dispatchV2(
            *action, Key::keySymToUTF8(event.key().sym()));
    } else {
        dispatchSucceeded = dispatchV2(*action);
    }
    if (!shouldAcceptHazkeyDispatch(*action, phase, dispatchSucceeded)) {
        // The request may have reached a server whose response was lost. Since
        // this key is about to fall through to the application, its journal
        // entry must never be replayed into a later IME session.
        server_.abandonUnconfirmedInput();
        discardLocalComposition();
        event.filter();
        return;
    }
    event.filterAndAccept();
}

void HazkeyState::updateSurroundingTextV2() {
    hazkey::commands::HandleImeAction request;
    auto* context = request.mutable_update_surrounding_context();
    if (!server_.context().secureInput &&
        ic_->capabilityFlags().test(CapabilityFlag::SurroundingText) &&
        ic_->surroundingText().isValid()) {
        const auto& surrounding = ic_->surroundingText();
        const auto cursor = hazkeyAnchorAfterAppend(surrounding.cursor(), "");
        if (cursor.has_value()) {
            context->set_text(surrounding.text());
            context->set_anchor(static_cast<uint32_t>(*cursor));
        }
    }
    applyV2Response(server_.transactV2(std::move(request)));
}

bool HazkeyState::resolvePendingLearning(bool commit) {
    hazkey::commands::HandleImeAction request;
    request.mutable_resolve_pending_learning()->set_commit(commit);
    // This sits directly on Fcitx's key path for Backspace, Ctrl-Z, and
    // pass-through navigation. Journal it durably, but cap the immediate
    // transport attempt so a stalled server cannot freeze application input.
    return applyV2Response(
        server_.transactV2DurableBestEffort(std::move(request)));
}

bool HazkeyState::flushDeferredActions(bool tryConnect,
                                       bool requireSuccess) {
    auto flushed = server_.flushPendingV2(tryConnect);
    bool responseSucceeded = true;
    if (flushed.response.has_value()) {
        responseSucceeded = applyV2Response(flushed.response);
    }
    return flushed.completed && (!requireSuccess || responseSucceeded);
}

bool HazkeyState::dispatchV2(const HazkeySemanticAction& action,
                             const std::string& insertedText) {
    hazkey::commands::HandleImeAction request;
    switch (action.kind) {
        case HazkeySemanticActionKind::insertText:
            request.mutable_insert_text()->set_text(insertedText);
            break;
        case HazkeySemanticActionKind::deleteBackward:
            request.mutable_delete_backward();
            break;
        case HazkeySemanticActionKind::deleteForward:
            request.mutable_delete_forward();
            break;
        case HazkeySemanticActionKind::moveCursor:
            request.mutable_move_cursor_v2()->set_offset(action.value);
            break;
        case HazkeySemanticActionKind::moveCursorToStart:
            request.mutable_move_cursor_to_edge()->set_edge(
                hazkey::commands::MoveCursorToEdge::START);
            break;
        case HazkeySemanticActionKind::moveCursorToEnd:
            request.mutable_move_cursor_to_edge()->set_edge(
                hazkey::commands::MoveCursorToEdge::END);
            break;
        case HazkeySemanticActionKind::startConversion:
            request.mutable_start_conversion();
            break;
        case HazkeySemanticActionKind::navigateCandidate:
            request.mutable_navigate_candidate()->set_delta(action.value);
            break;
        case HazkeySemanticActionKind::navigateCandidatePage:
            request.mutable_navigate_candidate_page()->set_delta(action.value);
            break;
        case HazkeySemanticActionKind::resizeSegment:
            request.mutable_resize_segment()->set_delta(action.value);
            break;
        case HazkeySemanticActionKind::moveActiveSegment:
            request.mutable_move_active_segment()->set_offset(action.value);
            break;
        case HazkeySemanticActionKind::commitSelected:
            request.mutable_commit_selected();
            break;
        case HazkeySemanticActionKind::commitAll:
            request.mutable_commit_all();
            break;
        case HazkeySemanticActionKind::cancel:
            request.mutable_cancel();
            break;
        case HazkeySemanticActionKind::transformHiragana:
            request.mutable_transform_active_segment()->set_transform(
                hazkey::commands::TransformActiveSegment::HIRAGANA);
            break;
        case HazkeySemanticActionKind::transformKatakanaFullwidth:
            request.mutable_transform_active_segment()->set_transform(
                hazkey::commands::TransformActiveSegment::KATAKANA_FULLWIDTH);
            break;
        case HazkeySemanticActionKind::transformKatakanaHalfwidth:
            request.mutable_transform_active_segment()->set_transform(
                hazkey::commands::TransformActiveSegment::KATAKANA_HALFWIDTH);
            break;
        case HazkeySemanticActionKind::transformAlphabetFullwidth:
            request.mutable_transform_active_segment()->set_transform(
                hazkey::commands::TransformActiveSegment::ALPHABET_FULLWIDTH);
            break;
        case HazkeySemanticActionKind::transformAlphabetHalfwidth:
            request.mutable_transform_active_segment()->set_transform(
                hazkey::commands::TransformActiveSegment::ALPHABET_HALFWIDTH);
            break;
        case HazkeySemanticActionKind::beginUnicodeInput:
            request.mutable_begin_unicode_input();
            break;
        case HazkeySemanticActionKind::appendUnicodeDigit:
            request.mutable_append_unicode_digit()->set_digit(insertedText);
            break;
        case HazkeySemanticActionKind::commitUnicodeInput:
            request.mutable_commit_unicode_input();
            break;
        case HazkeySemanticActionKind::selectCandidate:
        case HazkeySemanticActionKind::forgetCandidate:
        case HazkeySemanticActionKind::reconvert:
        case HazkeySemanticActionKind::consume:
        case HazkeySemanticActionKind::passThrough:
            return false;
    }
    return applyV2Response(server_.transactV2(std::move(request)));
}

void HazkeyState::selectV2Candidate(int index) {
    const auto& window = snapshot_.candidate_window();
    if (index < 0 || index >= window.items_size()) {
        return;
    }
    hazkey::commands::HandleImeAction request;
    auto* select = request.mutable_select_candidate();
    select->set_candidate_id(window.items(index).id());
    select->set_generation(window.generation());
    applyV2Response(server_.transactV2(std::move(request)));
}

void HazkeyState::forgetV2Candidate(int index) {
    const auto& window = snapshot_.candidate_window();
    if (index < 0 || index >= window.items_size()) {
        return;
    }
    hazkey::commands::HandleImeAction request;
    auto* forget = request.mutable_forget_candidate();
    forget->set_candidate_id(window.items(index).id());
    forget->set_generation(window.generation());
    applyV2Response(server_.transactV2(std::move(request)));
}

bool HazkeyState::reconvertV2Selection() {
    if (server_.context().secureInput ||
        !ic_->capabilityFlags().test(CapabilityFlag::SurroundingText) ||
        !ic_->surroundingText().isValid()) {
        return false;
    }
    const auto& surrounding = ic_->surroundingText();
    if (surrounding.cursor() == surrounding.anchor()) {
        return false;
    }
    const auto& text = surrounding.text();
    const auto length = utf8::lengthValidated(text);
    const auto start = std::min(surrounding.cursor(), surrounding.anchor());
    const auto end = std::max(surrounding.cursor(), surrounding.anchor());
    if (length == utf8::INVALID_LENGTH || end > length) {
        return false;
    }
    const auto begin = utf8::nextNChar(text.begin(), start);
    const auto finish = utf8::nextNChar(text.begin(), end);

    hazkey::commands::HandleImeAction request;
    auto* reconvert = request.mutable_reconvert();
    reconvert->set_text(std::string(begin, finish));
    reconvert->set_left_context(std::string(text.begin(), begin));
    reconvert->set_right_context(std::string(finish, text.end()));
    if (surrounding.anchor() < surrounding.cursor()) {
        reconvert->set_delete_before(surrounding.cursor() -
                                     surrounding.anchor());
    } else {
        reconvert->set_delete_after(surrounding.anchor() -
                                    surrounding.cursor());
    }
    return applyV2Response(server_.transactV2(std::move(request)));
}

bool HazkeyState::applyV2Response(
    const std::optional<hazkey::ResponseEnvelope>& response) {
    if (!response.has_value()) {
        // Transport failures leave the last confirmed snapshot visible.
        renderV2Snapshot();
        return false;
    }
    if (!response->has_handle_ime_action_result() ||
        !response->handle_ime_action_result().has_snapshot()) {
        return false;
    }

    const bool succeeded = response->status() == hazkey::SUCCESS &&
                           response->handle_ime_action_result().status() ==
                               hazkey::SUCCESS;

    snapshot_ = response->handle_ime_action_result().snapshot();
    std::optional<std::string> notification;
    // HazkeySessionClient strips server-originated effects from failed action
    // snapshots before they reach state. Effects still present on a failure
    // envelope therefore came from independently confirmed journal replay and
    // must be delivered even though this action returns false.
    const auto effects = snapshot_.effects();
    snapshot_.clear_effects();
    for (const auto& effect : effects) {
        if (!server_.shouldApplyEffect(effect.effect_id())) {
            continue;
        }
        switch (effect.type()) {
            case hazkey::ClientEffect::COMMIT_TEXT:
                ic_->commitString(effect.text());
                break;
            case hazkey::ClientEffect::DELETE_SURROUNDING_TEXT: {
                const int64_t before = effect.before();
                const int64_t after = effect.after();
                const int64_t size = before + after;
                if (before >= 0 && after >= 0 &&
                    size <= std::numeric_limits<unsigned int>::max()) {
                    ic_->deleteSurroundingText(
                        -static_cast<int>(before),
                        static_cast<unsigned int>(size));
                }
                break;
            }
            case hazkey::ClientEffect::SWITCH_INPUT_MODE:
                notification = "Input mode: " + effect.mode();
                break;
            case hazkey::ClientEffect::NOTIFY:
                notification = effect.message();
                break;
            case hazkey::ClientEffect::SCHEDULE_LIVE_CONVERSION:
                scheduleLiveConversion(effect.effect_id(), effect.delay_msec(),
                                       effect.scheduled_revision());
                break;
            case hazkey::ClientEffect::TYPE_UNSPECIFIED:
            default:
                break;
        }
    }
    renderV2Snapshot();
    if (notification.has_value()) {
        ic_->inputPanel().setAuxDown(Text(*notification));
    }
    return succeeded;
}

void HazkeyState::renderV2Snapshot() {
    HazkeySnapshotRenderer::render(
        ic_, snapshot_,
        engine_->config().preeditCursorPositionAtBeginning.value());
    if (snapshot_.candidate_window().items_size() == 0) {
        ic_->inputPanel().setCandidateList(nullptr);
        return;
    }

    auto candidateList = std::make_unique<HazkeyCandidateList>(
        snapshot_.candidate_window().items(),
        snapshot_.candidate_window().generation(),
        [this](const std::string& id, uint64_t generation) {
            cancelLiveConversionTimer();
            hazkey::commands::HandleImeAction request;
            auto* select = request.mutable_select_candidate();
            select->set_candidate_id(id);
            select->set_generation(generation);
            applyV2Response(server_.transactV2(std::move(request)));
            // Candidate clicks do not pass through HazkeyEngine::keyEvent(),
            // so flush the newly selected active-segment snapshot here.
            ic_->updatePreedit();
            ic_->updateUserInterface(UserInterfaceComponent::InputPanel);
        });
    candidateList->setPageSize(
        std::max(1, static_cast<int>(
                        snapshot_.candidate_window().page_size())));
    if (snapshot_.candidate_window().has_selected_index()) {
        candidateList->setGlobalCursorIndex(static_cast<int>(
            snapshot_.candidate_window().selected_index()));
    }
    ic_->inputPanel().setCandidateList(std::move(candidateList));
}

void HazkeyState::scheduleLiveConversion(uint64_t effectID, uint32_t delayMs,
                                         uint64_t scheduledRevision) {
    cancelLiveConversionTimer();
    pendingLiveConversionEffectID_ = effectID;
    const auto boundedDelay = std::min<uint32_t>(delayMs, 1000);
    const uint64_t deadline =
        now(CLOCK_MONOTONIC) + static_cast<uint64_t>(boundedDelay) * 1000ULL;
    liveConversionTimer_ = engine_->instance()->eventLoop().addTimeEvent(
        CLOCK_MONOTONIC, deadline, 1000,
        [this, effectID, scheduledRevision](EventSourceTime*, uint64_t) {
            if (effectID != pendingLiveConversionEffectID_) {
                return true;
            }
            pendingLiveConversionEffectID_ = 0;
            applyDelayedLiveConversion(effectID, scheduledRevision);
            return true;
        });
    liveConversionTimer_->setOneShot();
}

void HazkeyState::cancelLiveConversionTimer() {
    pendingLiveConversionEffectID_ = 0;
    liveConversionTimer_.reset();
}

void HazkeyState::applyDelayedLiveConversion(uint64_t effectID,
                                             uint64_t scheduledRevision) {
    // The effect ID participates in the local stale-callback guard above. The
    // scheduled revision travels over the wire because the normal client
    // request revision is refreshed during recovery and retry.
    (void)effectID;
    hazkey::commands::HandleImeAction request;
    request.mutable_apply_live_conversion()->set_scheduled_revision(
        scheduledRevision);
    (void)applyV2Response(server_.transactV2BestEffort(std::move(request)));
    // Timer callbacks do not return through HazkeyEngine::keyEvent(). Flush the
    // new snapshot explicitly.
    ic_->updatePreedit();
    ic_->updateUserInterface(UserInterfaceComponent::InputPanel);
}

void HazkeyState::reset() {
    cancelLiveConversionTimer();
    // reset() is a UI-clearing boundary and need not preserve composition.
    // Avoid normal multi-second cancel retries: hand off the final disposition
    // plus close through the bounded lifecycle FIFO, then drop local effects.
    const bool acceptedPendingLearning =
        snapshot_.phase() == hazkey::IDLE && snapshot_.pending_learning();
    server_.finalizeWithoutUITarget(acceptedPendingLearning);
    protocolAvailable_ = false;
    snapshot_.Clear();
    snapshot_.set_phase(hazkey::IDLE);
    ic_->inputPanel().reset();
}

}  // namespace fcitx
